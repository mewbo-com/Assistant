"""tests/wiki/test_tool_allowlist_resolution.py

Every tool name a wiki allowlist grants — the two Python constants that gate
the indexer/QA root sessions (``INDEXER_TOOLS`` / ``QA_TOOLS``,
``mewbo_api.wiki.jobs``) and each wiki AgentDef's ``tools:`` frontmatter — must
resolve to something a session can actually bind. None of the three surfaces
a name is checked against complains about an unknown id: ``filter_specs``
(core ``ToolRegistry``) and ``SessionToolRegistry.ids_for`` both silently drop
anything absent from their own factories, and the loop-injected spawn family
(``spawn_agent`` / ``check_agents``) lives outside both registries entirely.
A typo'd or renamed tool id therefore never surfaces as an error anywhere —
it just quietly becomes unreachable. This module pins resolution so a name
that stops resolving fails a test instead of a live indexing run.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_api.wiki.jobs import INDEXER_TOOLS, QA_TOOLS
from mewbo_core.agents.agent_registry import parse_agent_def
from mewbo_core.tooling.plugins import discover_builtin_plugins
from mewbo_core.tooling.session_tools import SessionToolRegistry
from mewbo_core.tooling.tool_registry import ToolRegistry, _default_registry
from mewbo_graph import plugins_root

WIKI_AGENTS_DIR = Path("packages/mewbo_graph/src/mewbo_graph/plugins/wiki/agents")

# The spawn family is bound directly by ``ToolUseLoop`` (``_spawn_agent_tool``
# / ``_directly_bound_tool_schemas``, gated by ``_loop_injected_admitted``),
# outside BOTH the core ``ToolRegistry`` and ``SessionToolRegistry``. A wiki
# allowlist naming these relies on that loop-injection seam, not on either
# registry — so they need an explicit carve-out here rather than a spec.
LOOP_INJECTED_SPAWN_FAMILY = frozenset({"spawn_agent", "check_agents"})

# jobs.py's own comment on ``INDEXER_TOOLS``: wiki_build_graph/wiki_query_graph
# are Phase-3 tools "tolerated when absent at runtime" — the registry silently
# skips an unknown name rather than failing the session, by design, so a
# deployment missing them must not fail an index. They DO resolve today (the
# plugin manifest registers both); this allowance documents that absence is
# tolerated, not that they are currently dead.
PHASE3_TOLERATED_ABSENT = frozenset({"wiki_build_graph", "wiki_query_graph"})


@pytest.fixture(scope="module")
def core_registry() -> ToolRegistry:
    """The core built-in tool specs (read_file, aider_*, ...)."""
    return _default_registry()


@pytest.fixture(scope="module")
def session_registry() -> SessionToolRegistry:
    """A real ``SessionToolRegistry`` fed by every built-in plugin manifest.

    Mirrors how a live session populates it (``load_all_plugin_components`` →
    ``SessionToolRegistry.load_entry`` per ``session_tools`` manifest entry) —
    built directly from the wiki (and sibling) plugin manifests rather than
    hand-listing ids, so this test can't drift from what a plugin actually
    ships.
    """
    registry = SessionToolRegistry()
    for pc in discover_builtin_plugins(plugins_root()):
        if pc.manifest is None:
            continue
        for entry in pc.session_tool_entries:
            registry.load_entry(
                entry, requires_capabilities=pc.manifest.requires_capabilities
            )
    return registry


def _resolves(
    tool_id: str, *, core_registry: ToolRegistry, session_registry: SessionToolRegistry
) -> bool:
    """Whether *tool_id* is bound by any surface a wiki session draws from."""
    if tool_id in LOOP_INJECTED_SPAWN_FAMILY:
        return True
    if core_registry.get_spec(tool_id) is not None:
        return True
    # A singleton allowlist under ``strict_tool_scope=True`` exercises the
    # SAME ``tid in self._factories`` membership check ``ids_for`` applies in
    # production (jobs.py passes ``strict_tool_scope=True`` for both the
    # indexer and QA runs) — pure existence, not gated by which capability tag
    # a factory happens to carry (e.g. the entity tools, cross-declared by
    # both the wiki and scg plugin manifests).
    return bool(session_registry.ids_for([tool_id], strict_tool_scope=True))


def _assert_all_resolve(
    names: list[str],
    *,
    core_registry: ToolRegistry,
    session_registry: SessionToolRegistry,
    context: str,
) -> None:
    unresolved = [
        name
        for name in names
        if name not in PHASE3_TOLERATED_ABSENT
        and not _resolves(name, core_registry=core_registry, session_registry=session_registry)
    ]
    assert not unresolved, f"{context}: no registered tool resolves {unresolved!r}"


def test_indexer_tools_all_resolve(core_registry, session_registry):
    _assert_all_resolve(
        INDEXER_TOOLS,
        core_registry=core_registry,
        session_registry=session_registry,
        context="INDEXER_TOOLS",
    )


def test_qa_tools_all_resolve(core_registry, session_registry):
    _assert_all_resolve(
        QA_TOOLS,
        core_registry=core_registry,
        session_registry=session_registry,
        context="QA_TOOLS",
    )


@pytest.mark.parametrize(
    "agent_file",
    [
        "wiki-indexer.md",
        "wiki-enricher.md",
        "wiki-page-writer.md",
        "wiki-qa.md",
        "wiki-qa-probe.md",
    ],
)
def test_wiki_agent_def_tools_all_resolve(agent_file, core_registry, session_registry):
    agent_def = parse_agent_def(WIKI_AGENTS_DIR / agent_file, source="plugin:wiki")
    assert agent_def is not None
    _assert_all_resolve(
        agent_def.allowed_tools or [],
        core_registry=core_registry,
        session_registry=session_registry,
        context=f"{agent_file} tools: frontmatter",
    )
