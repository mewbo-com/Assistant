"""Re-engaging a capability-gated session must rebind its own tools.

Asserted on the RESOLVED TOOL SET — the ids ``SessionToolRegistry.build_for``
actually instantiates — never on the context payload. A payload assertion passes
while binding stays broken, which is precisely how the defect survived: the
durable ``session_spec.capabilities`` read ``["wiki"]`` on every re-engage event
the whole time, and the run still bound zero ``wiki_*`` tools.

**The two halves are independent, and BOTH are required.** ``ids_for`` admits an
allowlisted id by NAME ALONE — the capability gate never runs for it — so the
capability and the persisted allowlist gate disjoint halves of the wiki surface:

* the six ``unconditional`` read tools (``wiki_read_page`` and friends) surface
  only via the capability gate, so they need ``wiki`` in the session caps;
* the capability-gated write tools (``wiki_submit_page``, ``wiki_finalize``)
  reach a strictly-scoped run only by being NAMED, so they need the indexer's
  allowlist to have been persisted.

Fixing either alone leaves half the surface unbound, which is why the tests below
cover both and why the end-to-end case drives them together.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mewbo_api.session_spec import SessionSpec
from mewbo_api.wiki.jobs import INDEXER_TOOLS, _start_indexer_session
from mewbo_core.tooling.plugins import discover_builtin_plugins
from mewbo_core.tooling.session_tools import SessionToolRegistry
from mewbo_graph import plugins_root
from mewbo_graph.wiki.store import JsonWikiStore

# What the console stamps on EVERY request (``realClient.ts`` builds this as a
# fixed constant), which is what buried the session's own capability: the newest
# context event always carries these four and nothing else.
CONSOLE_CAPABILITIES = ["stlite", "apps", "ask_user", "generative_ui"]

# A permissive re-engage's ``context.mcp_tools`` — an MCP-tool ceiling that never
# names a built-in session tool. Non-empty is the load-bearing part: ANY explicit
# allowlist caps the capability gate, so this is what a real console re-engage
# sends and it must not be simplified to ``None``.
CONSOLE_MCP_TOOLS = ["some_mcp_server__search"]


@pytest.fixture(scope="module")
def registry() -> SessionToolRegistry:
    """A real registry fed by every built-in plugin manifest.

    Mirrors how a live session populates it, so the capability stamps under test
    are the ones the wiki/scg/widget/generative-ui plugins actually ship rather
    than hand-written fixtures that could agree with a wrong implementation.
    """
    reg = SessionToolRegistry()
    for pc in discover_builtin_plugins(plugins_root()):
        if pc.manifest is None:
            continue
        for entry in pc.session_tool_entries:
            reg.load_entry(entry, requires_capabilities=pc.manifest.requires_capabilities)
    return reg


def _built_ids(
    registry: SessionToolRegistry,
    allowed: list[str] | None,
    caps,
    *,
    strict: bool = False,
) -> set[str]:
    return {
        tool.tool_id
        for tool in registry.build_for(
            allowed,
            session_id="s1",
            event_logger=None,
            session_capabilities=tuple(caps or ()),
            strict_tool_scope=strict,
        )
    }


class TestCapabilityUnionOnReengage:
    def test_reengaging_a_wiki_session_still_binds_its_wiki_read_tools(self, registry):
        """The reported failure, at the seam that actually decides.

        Without the union the resolved caps are the console's four, none of which
        is ``wiki``, so every ``unconditional`` wiki factory is skipped by its own
        capability check and the agent falls back to generic file browsing.
        """
        spec = SessionSpec(capabilities=["wiki"])

        resolved = spec.run_capabilities(CONSOLE_CAPABILITIES)
        assert "wiki" in resolved

        bound = _built_ids(registry, CONSOLE_MCP_TOOLS, resolved)
        assert {"wiki_read_page", "wiki_list_pages", "wiki_search_pages"} <= bound

    def test_the_bare_advertisement_binds_no_wiki_tool_at_all(self, registry):
        """The control that makes the test above non-vacuous.

        This is the advertisement alone — and it must
        bind nothing, or the assertion above would pass for the wrong reason.
        """
        bound = _built_ids(registry, CONSOLE_MCP_TOOLS, CONSOLE_CAPABILITIES)

        assert not [tid for tid in bound if tid.startswith("wiki_")]

    def test_a_surface_without_a_rendering_capability_does_not_inherit_it(self, registry):
        """The other direction: only the SESSION-OWNED half travels.

        A spec that carries rendering capabilities (every console-created session
        does) must not hand them to a surface that cannot render them — otherwise
        the fix for the drop becomes a widening.
        """
        spec = SessionSpec(capabilities=["wiki", "stlite", "generative_ui"])

        resolved = spec.run_capabilities(["ask_user"])

        assert "wiki" in resolved
        assert "stlite" not in resolved
        assert "generative_ui" not in resolved

        bound = _built_ids(registry, None, resolved)
        assert "submit_widget" not in bound  # stlite-gated
        assert "present_ui" not in bound  # generative_ui-gated

    def test_apps_is_not_treated_as_session_owned(self):
        """``apps`` gates a plugin suite yet is deliberately request-scoped.

        The console advertises it on every request regardless of what the session
        is for, so unioning it from the spec would grant it to sessions that
        merely happened to be created from a browser.
        """
        spec = SessionSpec(capabilities=["apps"])

        assert spec.run_capabilities(["ask_user"]) == ("ask_user",)

    def test_an_already_advertised_capability_is_not_duplicated(self):
        spec = SessionSpec(capabilities=["wiki"])

        assert spec.run_capabilities(["wiki", "ask_user"]) == ("wiki", "ask_user")

    def test_no_advertisement_still_falls_back_to_the_purpose(self):
        """An unattended fire sends no header — the purpose is all there is."""
        spec = SessionSpec(capabilities=["wiki"])

        assert spec.run_capabilities(None) == ("wiki",)
        assert spec.run_capabilities([]) == ("wiki",)

    def test_unattended_stripping_still_applies_over_the_union(self):
        """A scheduled fire must not gain ``ask_user`` from an interactive turn.

        The union is turn-scoped and never written back onto the spec, so the
        unattended derivation is unchanged — pinned here because the union is
        exactly the kind of change that could have leaked into it.
        """
        spec = SessionSpec(capabilities=["wiki", "ask_user"])

        assert spec.run_capabilities(CONSOLE_CAPABILITIES) == (
            "wiki",
            "stlite",
            "apps",
            "ask_user",
            "generative_ui",
        )
        assert spec.unattended_capabilities() == ("wiki",)


class TestIndexerSessionPersistsItsScope:
    @pytest.fixture()
    def started(self, tmp_path: Path, monkeypatch) -> dict:
        """Drive the real seam and return the context payload it persisted."""
        monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
        store = JsonWikiStore(root_dir=tmp_path / "wiki")
        runtime = MagicMock()
        runtime.resolve_session.return_value = "sess-indexer"

        _start_indexer_session(
            store=store,
            runtime=runtime,
            job_id="job-1",
            model="anthropic/claude-sonnet-4-6",
            user_query="index it",
        )

        return runtime.append_context_event.call_args.args[1]

    def test_it_persists_every_key_the_run_was_scoped_with(self, started):
        """``WikiQaSession.start`` already did this; the indexer never did.

        A re-engage can only re-apply what the start path wrote down, so these
        four being ``start_async`` kwargs alone is what cost a continued indexing
        session its tool ceiling, its playbook and its checkout.
        """
        assert started["client_capabilities"] == ["wiki"]
        assert started["mcp_tools"] == INDEXER_TOOLS
        assert started["strict_tool_scope"] is True
        assert started["skill_instructions"]
        assert started["cwd"]

    def test_the_persisted_payload_rebinds_the_write_tools_on_reengage(
        self, started, registry
    ):
        """End to end, through the generic re-engage path's own reconstruction.

        This is the case neither half fixes alone: ``wiki_submit_page`` is
        capability-gated AND absent from a console allowlist, so it returns only
        when the persisted ceiling is read back AND the capability survives.
        """
        spec = SessionSpec.from_context(started)
        assert spec.strict_tool_scope is True

        resolved = spec.run_capabilities(CONSOLE_CAPABILITIES)
        bound = _built_ids(
            registry, list(spec.allowed_tools or ()), resolved, strict=True
        )

        assert {"wiki_submit_page", "wiki_finalize", "wiki_commit_plan"} <= bound
