#!/usr/bin/env python3
"""Mid-run project switching — the ``list_projects`` / ``switch_project`` pair.

The contract under test is not "the tool returned ok" but "everything derived
from the working directory moved together". A partial switch is the failure mode
worth guarding: the agent believes it moved, half the machinery did not, and the
results look plausible right up until they are applied to the wrong tree. So the
assertions here are made from the CALLER's side of each derived seam —

- the ``root`` the loop injects into the next registry tool call,
- the containment root that jails filesystem access when enforcement is on,
- the workspace the spawn tool hands to the next child it builds,
- the project instructions baked into the system prompt,
- the ``context`` event the API's session-cwd resolver reads back.

Only two I/O boundaries are stubbed: ``build_chat_model`` (never a real model)
and ``get_or_build_registry`` (a filesystem scan that can start MCP servers).
The stub for the latter doubles as the assertion that the registry really is
re-resolved for the new directory. Everything else — the catalog, the tools, the
loop's dispatch path, the error-envelope reclassification — is the real code.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.agents.hypervisor import AgentHypervisor
from mewbo_core.config import ProjectConfig, reset_config, set_config_override
from mewbo_core.hooks import HookManager
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.tooling.tool_registry import ToolRegistry, ToolSpec
from mewbo_core.workspaces.project_catalog import ProjectCatalog
from mewbo_core.workspaces.project_switch import (
    LIST_PROJECTS_TOOL_ID,
    SWITCH_PROJECT_TOOL_ID,
    ListProjectsTool,
    SwitchProjectTool,
)

# A marker only project B's instructions carry, so "were the new project's
# instructions picked up" never depends on whether this host happens to have a
# user-level ~/.claude/CLAUDE.md (it usually does, and it would answer yes for
# every directory).
B_MARKER = "beacon-project-instructions-marker"


# ---------------------------------------------------------------------------
# Fixtures — two real directories, one catalog over them
# ---------------------------------------------------------------------------


@pytest.fixture
def projects(tmp_path):
    """Two configured project directories, B carrying its own instructions."""
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    beacon = tmp_path / "beacon"
    beacon.mkdir()
    (beacon / "CLAUDE.md").write_text(f"# Beacon\n\n{B_MARKER}\n", encoding="utf-8")
    ghost = tmp_path / "ghost"  # deliberately NOT created
    return {"alpha": str(alpha), "beacon": str(beacon), "ghost": str(ghost)}


@pytest.fixture
def catalog(projects) -> ProjectCatalog:
    """A catalog over the two real directories plus one that does not exist."""
    return ProjectCatalog(
        configured={
            "alpha": ProjectConfig(path=projects["alpha"], description="First project"),
            "beacon": ProjectConfig(path=projects["beacon"], description="Second project"),
            "ghost": ProjectConfig(path=projects["ghost"], description="Never created"),
        }
    )


def _spec(tool_id: str) -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=tool_id,
        factory=lambda: MagicMock(),
        enabled=True,
        kind="local",
        metadata={"schema": {"type": "object", "properties": {}}},
    )


def _registry(*tool_ids: str) -> ToolRegistry:
    registry = ToolRegistry()
    for tool_id in tool_ids:
        registry.register(_spec(tool_id))
    return registry


def _allow_all_policy() -> PermissionPolicy:
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    return policy


def _hook_manager() -> HookManager:
    hm = MagicMock(spec=HookManager)
    hm.run_pre_tool_use.side_effect = lambda step: step
    hm.run_post_tool_use.side_effect = lambda step, result: result
    hm.run_permission_request.side_effect = lambda step, decision: decision
    return hm


def _build_loop(
    catalog: ProjectCatalog,
    cwd: str,
    *,
    events: list | None = None,
    workspace_mode: str = "full_access",
    project_autoselect: bool = True,
    can_spawn: bool = False,
) -> ToolUseLoop:
    """A depth-0 loop shaped the way ``Orchestrator`` builds one."""
    ctx = AgentContext.root(
        model_name="test-model",
        max_depth=5 if can_spawn else 0,
        registry=AgentHypervisor(max_concurrent=10),
        event_logger=(events.append if events is not None else None),
        workspace_mode=workspace_mode,
    )
    with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=_registry("read_file", "aider_shell_tool"),
            permission_policy=_allow_all_policy(),
            hook_manager=_hook_manager(),
            cwd=cwd,
            session_id="switch-sess",
            project_autoselect=project_autoselect,
            project_catalog=catalog,
        )
    # ``run()`` stamps this from the specs it is handed; a switch narrows
    # against it, so a loop driven without a run has to stand it up itself.
    loop._tool_specs_full = list(loop._tool_registry.list_specs())
    return loop


def _switch(loop: ToolUseLoop, key: str, *, registries: dict | None = None):
    """Drive the real ``switch_project`` handler, stubbing only the two I/O legs.

    ``registries`` records every cwd ``get_or_build_registry`` is asked for, so a
    caller can assert the registry was re-resolved for the directory switched to.
    """
    seen: dict = registries if registries is not None else {}

    def _fake_registry(*, cwd=None, extra_mcp_servers=None):
        seen[cwd] = seen.get(cwd, 0) + 1
        return _registry("read_file", "aider_shell_tool")

    tool = loop._session_tool(SWITCH_PROJECT_TOOL_ID)
    assert tool is not None, "switch_project was not bound"
    step = MagicMock()
    step.tool_input = {"project": key}
    with (
        patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build,
        patch("mewbo_core.loop.tool_use_loop.get_or_build_registry", _fake_registry),
    ):
        mock_build.return_value = MagicMock()
        return asyncio.run(tool.handle(step))


def _switch_ok(loop: ToolUseLoop, key: str, *, registries: dict | None = None):
    """A switch that must SUCCEED — refusing here would fail the wrong assertion.

    ``rebind_workspace`` mutates the loop step by step, so a switch that dies
    halfway leaves the cwd already moved while later seams have not. Without
    this guard a test asserting on one of the early seams passes on a workspace
    that is half moved, which is precisely the failure the whole module exists
    to catch.
    """
    result = _switch(loop, key, registries=registries)
    assert not result.content.startswith("{'error'"), result.content
    return result


# ---------------------------------------------------------------------------
# Binding: the pair is opt-in and root-only
# ---------------------------------------------------------------------------


class TestBinding:
    def test_autoselect_off_binds_neither_tool(self, catalog, projects):
        loop = _build_loop(catalog, projects["alpha"], project_autoselect=False)
        bound = {t.tool_id for t in loop._session_tools}
        assert LIST_PROJECTS_TOOL_ID not in bound
        assert SWITCH_PROJECT_TOOL_ID not in bound

    def test_autoselect_on_binds_both_tools(self, catalog, projects):
        loop = _build_loop(catalog, projects["alpha"])
        bound = {t.tool_id for t in loop._session_tools}
        assert {LIST_PROJECTS_TOOL_ID, SWITCH_PROJECT_TOOL_ID} <= bound

    def test_missing_catalog_binds_neither_tool(self, projects):
        """The flag alone is not enough — there must be something to resolve."""
        ctx = AgentContext.root(
            model_name="test-model",
            max_depth=0,
            registry=AgentHypervisor(max_concurrent=10),
        )
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=_registry("read_file"),
            permission_policy=_allow_all_policy(),
            hook_manager=_hook_manager(),
            cwd=projects["alpha"],
            session_id="s",
            project_autoselect=True,
            project_catalog=None,
        )
        bound = {t.tool_id for t in loop._session_tools}
        assert LIST_PROJECTS_TOOL_ID not in bound
        assert SWITCH_PROJECT_TOOL_ID not in bound

    def test_strict_scope_that_omits_the_pair_withholds_it(self, catalog, projects):
        """An authoritative ``tools:`` list is the ceiling for a loop-injected tool."""
        ctx = AgentContext.root(
            model_name="test-model",
            max_depth=0,
            registry=AgentHypervisor(max_concurrent=10),
        )
        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=_registry("read_file"),
            permission_policy=_allow_all_policy(),
            hook_manager=_hook_manager(),
            cwd=projects["alpha"],
            session_id="s",
            allowed_tools=["read_file"],
            strict_tool_scope=True,
            project_autoselect=True,
            project_catalog=catalog,
        )
        bound = {t.tool_id for t in loop._session_tools}
        assert LIST_PROJECTS_TOOL_ID not in bound
        assert SWITCH_PROJECT_TOOL_ID not in bound

    def test_both_tools_bind_in_plan_mode_too(self, catalog, projects):
        """Choosing where to work is a planning concern as much as an acting one."""
        loop = _build_loop(catalog, projects["alpha"])
        for tool_id in (LIST_PROJECTS_TOOL_ID, SWITCH_PROJECT_TOOL_ID):
            tool = loop._session_tool(tool_id)
            assert tool is not None
            assert {"plan", "act"} <= tool.modes


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------


class TestListProjects:
    def test_lists_every_entry_with_the_documented_fields(self, catalog, projects):
        import json

        tool = ListProjectsTool(catalog=catalog)
        step = MagicMock()
        step.tool_input = {}
        payload = json.loads(asyncio.run(tool.handle(step)).content)
        assert payload["count"] == 3
        by_key = {row["key"]: row for row in payload["projects"]}
        assert set(by_key) == {"alpha", "beacon", "ghost"}
        assert by_key["beacon"]["path"] == projects["beacon"]
        assert by_key["beacon"]["kind"] == "configured"
        assert by_key["beacon"]["available"] is True
        # The one entry with no directory reports so rather than being hidden —
        # a model that cannot see it cannot be told why it was refused.
        assert by_key["ghost"]["available"] is False
        assert set(by_key["alpha"]) == {
            "key",
            "name",
            "kind",
            "path",
            "description",
            "available",
            "repo",
            "branch",
        }

    def test_declares_its_own_result_cap(self, catalog):
        """A SessionTool has no ToolSpec, so an undeclared cap inherits 2000."""
        tool = ListProjectsTool(catalog=catalog)
        assert tool.max_result_chars > 2000
        assert tool.should_terminate_run() is False
        assert tool.terminal_reason() == "awaiting_approval"


# ---------------------------------------------------------------------------
# A switch re-points every derived seam
# ---------------------------------------------------------------------------


class TestSwitchRepointsTheWorkspace:
    def test_next_tool_call_gets_the_new_root(self, catalog, projects):
        loop = _build_loop(catalog, projects["alpha"])
        before = loop._tool_call_to_action_step({"name": "read_file", "args": {}})
        assert before.tool_input["root"] == projects["alpha"]

        _switch_ok(loop, "beacon")

        after = loop._tool_call_to_action_step({"name": "read_file", "args": {}})
        assert after.tool_input["root"] == projects["beacon"]

    def test_containment_root_moves_with_the_switch(self, catalog, projects):
        """A stale containment root is a jail around the wrong directory."""
        set_config_override({"agent": {"workspace_enforcement": True}})
        try:
            loop = _build_loop(
                catalog, projects["alpha"], workspace_mode="workspace_write"
            )
            assert loop._containment is not None
            assert loop._containment.root == projects["alpha"]

            _switch_ok(loop, "beacon")

            assert loop._containment is not None
            assert loop._containment.root == projects["beacon"]
            assert loop._containment.mode == "workspace_write"
        finally:
            reset_config()

    def test_enforcement_off_leaves_containment_inert_after_a_switch(
        self, catalog, projects
    ):
        """The staged-off flag must stay a no-op on the new path too."""
        loop = _build_loop(catalog, projects["alpha"])
        assert loop._containment is None
        _switch_ok(loop, "beacon")
        assert loop._containment is None

    def test_project_instructions_are_re_resolved(self, catalog, projects):
        loop = _build_loop(catalog, projects["alpha"])
        assert B_MARKER not in (loop._project_instructions or "")

        _switch_ok(loop, "beacon")

        assert B_MARKER in (loop._project_instructions or "")

    def test_registry_is_re_resolved_for_the_new_directory(self, catalog, projects):
        seen: dict = {}
        loop = _build_loop(catalog, projects["alpha"])
        _switch_ok(loop, "beacon", registries=seen)
        assert projects["beacon"] in seen

    def test_spec_ceiling_narrows_and_never_widens(self, catalog, projects):
        """A switch must not hand the agent tools the caller never granted."""
        loop = _build_loop(catalog, projects["alpha"])
        loop._tool_specs_full = [_spec("read_file")]

        _switch_ok(loop, "beacon")

        # The stub registry offers read_file AND aider_shell_tool; only the id
        # this run already held survives.
        assert {s.tool_id for s in loop._tool_specs_full} == {"read_file"}

    def test_the_new_binding_is_applied_at_the_next_turn_boundary(
        self, catalog, projects
    ):
        """The tool mutates state now and leaves the transcript rewrite for later."""
        loop = _build_loop(catalog, projects["alpha"])
        assert loop._pending_workspace_bind is None
        _switch_ok(loop, "beacon")
        assert loop._pending_workspace_bind is not None

    def test_result_is_json_with_every_key_always_present(self, catalog, projects):
        """A transcript card branches on VALUES, never on key existence."""
        import json

        loop = _build_loop(catalog, projects["alpha"])
        result = json.loads(_switch_ok(loop, "beacon").content)
        assert set(result) == {
            "project",
            "name",
            "kind",
            "cwd",
            "repo",
            "branch",
            "description",
            "previous_project",
            "previous_cwd",
            "project_instructions_found",
            "bound_tools",
            "skills",
            "summary",
        }
        assert result["project"] == "beacon"
        assert result["kind"] == "configured"
        assert result["cwd"] == projects["beacon"]
        assert result["description"] == "Second project"
        # Unknown reads as null, not as an absent key.
        assert result["repo"] is None
        assert result["branch"] is None
        assert result["project_instructions_found"] is True
        assert isinstance(result["bound_tools"], int)

    def test_result_summary_names_where_the_agent_now_is(self, catalog, projects):
        import json

        loop = _build_loop(catalog, projects["alpha"])
        summary = json.loads(_switch_ok(loop, "beacon").content)["summary"]
        assert "beacon" in summary
        assert projects["beacon"] in summary
        assert "found and loaded" in summary
        assert "sub-agent" in summary

    def test_result_is_honest_when_no_instructions_exist(self, catalog, projects):
        import json

        loop = _build_loop(catalog, projects["beacon"])
        result = json.loads(_switch_ok(loop, "alpha").content)
        assert result["project_instructions_found"] is False
        assert "none found" in result["summary"]

    def test_previous_project_is_null_on_the_first_switch_then_named(
        self, catalog, projects
    ):
        """The loop is handed a directory at construction, never a catalog key."""
        import json

        loop = _build_loop(catalog, projects["alpha"])

        first = json.loads(_switch_ok(loop, "beacon").content)
        assert first["previous_project"] is None
        assert first["previous_cwd"] == projects["alpha"]

        second = json.loads(_switch_ok(loop, "alpha").content)
        assert second["previous_project"] == "beacon"
        assert second["previous_cwd"] == projects["beacon"]


# ---------------------------------------------------------------------------
# The spawn seam — the trap
# ---------------------------------------------------------------------------


class TestSwitchRepointsFutureChildren:
    def test_a_child_spawned_after_the_switch_starts_in_the_new_project(
        self, catalog, projects
    ):
        """SpawnAgentTool holds its OWN copy of cwd and forwards it to every child."""
        loop = _build_loop(catalog, projects["alpha"], can_spawn=True)
        spawn_tool = loop._spawn_agent_tool
        assert spawn_tool is not None, "the spawn tool must exist for this seam to matter"
        assert spawn_tool._cwd == projects["alpha"]

        _switch_ok(loop, "beacon")

        assert spawn_tool._cwd == projects["beacon"]
        assert B_MARKER in (spawn_tool._project_instructions or "")


# ---------------------------------------------------------------------------
# Repeated switches
# ---------------------------------------------------------------------------


class TestRepeatedSwitches:
    def test_a_to_b_to_a_lands_back_where_it_started(self, catalog, projects):
        seen: dict = {}
        loop = _build_loop(catalog, projects["alpha"], can_spawn=True)

        _switch_ok(loop, "beacon", registries=seen)
        assert loop._cwd == projects["beacon"]
        assert B_MARKER in (loop._project_instructions or "")

        _switch_ok(loop, "alpha", registries=seen)
        assert loop._cwd == projects["alpha"]
        assert B_MARKER not in (loop._project_instructions or "")
        assert loop._spawn_agent_tool._cwd == projects["alpha"]
        step = loop._tool_call_to_action_step({"name": "read_file", "args": {}})
        assert step.tool_input["root"] == projects["alpha"]


# ---------------------------------------------------------------------------
# The context event — the whole reason re-engagement follows the switch
# ---------------------------------------------------------------------------


class TestContextEvent:
    def test_appends_a_context_event_with_exactly_project_and_cwd(
        self, catalog, projects
    ):
        events: list = []
        loop = _build_loop(catalog, projects["alpha"], events=events)

        _switch_ok(loop, "beacon")

        contexts = [e for e in events if e["type"] == "context"]
        assert len(contexts) == 1
        assert contexts[0]["payload"] == {
            "project": "beacon",
            "cwd": projects["beacon"],
        }

    def test_carries_the_previous_context_forward(self, catalog, projects):
        """A two-key patch would BLANK every verbatim reader of the last payload.

        ``_load_last_context`` (the API's ``/message`` re-engage and ``/recover``)
        and the console's ``getLastContext`` both take the newest payload as-is,
        so the switch event has to be the previous effective context PLUS the
        two keys the switch actually changed.
        """
        events: list = []
        loop = _build_loop(catalog, projects["alpha"], events=events)
        loop._session_context_reader = lambda: {
            "model": "openai/pinned-model",
            "mcp_tools": ["a", "b"],
            "mode": "act",
            "project": "alpha",
            "cwd": projects["alpha"],
        }

        _switch_ok(loop, "beacon")

        payload = [e for e in events if e["type"] == "context"][0]["payload"]
        # The switch owns these two and overrides whatever was carried.
        assert payload["project"] == "beacon"
        assert payload["cwd"] == projects["beacon"]
        # Everything else survives verbatim.
        assert payload["model"] == "openai/pinned-model"
        assert payload["mcp_tools"] == ["a", "b"]
        assert payload["mode"] == "act"

    def test_a_reader_that_raises_still_writes_the_two_switch_keys(
        self, catalog, projects
    ):
        """Carry-forward is best-effort — it must never be what fails a switch."""
        events: list = []
        loop = _build_loop(catalog, projects["alpha"], events=events)

        def _boom():
            raise RuntimeError("store down")

        loop._session_context_reader = _boom

        _switch_ok(loop, "beacon")

        payload = [e for e in events if e["type"] == "context"][0]["payload"]
        assert payload == {"project": "beacon", "cwd": projects["beacon"]}

    def test_the_carried_payload_still_resolves_as_a_session_cwd(
        self, catalog, projects
    ):
        """The API resolver walks back for these keys; a superset must still hit."""
        events: list = []
        loop = _build_loop(catalog, projects["alpha"], events=events)
        loop._session_context_reader = lambda: {"model": "m", "skill": "s"}

        _switch_ok(loop, "beacon")

        payload = [e for e in events if e["type"] == "context"][0]["payload"]
        assert payload["cwd"] == projects["beacon"]
        assert payload["project"] == "beacon"

    def test_a_refused_switch_writes_no_context_event(self, catalog, projects):
        events: list = []
        loop = _build_loop(catalog, projects["alpha"], events=events)

        _switch(loop, "nope")

        assert [e for e in events if e["type"] == "context"] == []


# ---------------------------------------------------------------------------
# Refusals — the envelope AND the failed-step reclassification
# ---------------------------------------------------------------------------


def _dispatch(loop: ToolUseLoop, key: str):
    """Drive the LOOP's dispatch path so the failed-step reclassification runs."""

    def _fake_registry(*, cwd=None, extra_mcp_servers=None):
        return _registry("read_file")

    with (
        patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build,
        patch("mewbo_core.loop.tool_use_loop.get_or_build_registry", _fake_registry),
    ):
        mock_build.return_value = MagicMock()
        return asyncio.run(
            loop._execute_tool_call(
                {
                    "id": "call-1",
                    "name": SWITCH_PROJECT_TOOL_ID,
                    "args": {"project": key},
                },
                [],
            )
        )


class TestRefusals:
    @pytest.mark.parametrize(
        ("key", "code"),
        [
            ("nope", "not_found"),
            ("ghost", "unavailable"),
            ("auto", "auto_sentinel"),
            ("", "empty"),
        ],
    )
    def test_refusal_returns_the_shared_envelope(self, catalog, projects, key, code):
        loop = _build_loop(catalog, projects["alpha"])
        content = _switch(loop, key).content
        # The Python-repr form is the contract: ``_SessionToolError.parse`` uses
        # ``ast.literal_eval``, so a ``json.dumps`` envelope is silently declined
        # and the failure would record as a success.
        parsed = eval(content)  # noqa: S307 - asserting the exact repr shape
        assert parsed["error"]["code"] == code
        assert parsed["error"]["message"]

    def test_a_refusal_leaves_the_workspace_untouched(self, catalog, projects):
        loop = _build_loop(catalog, projects["alpha"], can_spawn=True)
        _switch(loop, "ghost")
        assert loop._cwd == projects["alpha"]
        assert loop._spawn_agent_tool._cwd == projects["alpha"]
        assert loop._pending_workspace_bind is None

    @pytest.mark.parametrize("key", ["nope", "ghost", "auto"])
    def test_refusal_records_a_failed_step(self, catalog, projects, key):
        loop = _build_loop(catalog, projects["alpha"])
        result = _dispatch(loop, key)
        assert result.success is False
        assert result.tool_id == SWITCH_PROJECT_TOOL_ID

    def test_a_successful_switch_records_a_successful_step(self, catalog, projects):
        loop = _build_loop(catalog, projects["alpha"])
        result = _dispatch(loop, "beacon")
        assert result.success is True
        assert loop._cwd == projects["beacon"]

    def test_a_non_string_project_argument_is_a_validation_refusal(
        self, catalog, projects
    ):
        loop = _build_loop(catalog, projects["alpha"])
        tool = loop._session_tool(SWITCH_PROJECT_TOOL_ID)
        step = MagicMock()
        step.tool_input = {"project": 7}
        parsed = eval(asyncio.run(tool.handle(step)).content)  # noqa: S307
        assert parsed["error"]["code"] == "validation"

    def test_a_failing_rebind_reports_it_and_does_not_raise(self, catalog):
        """A switch that blows up must not take the run with it."""

        def _boom(entry):
            raise OSError("disk gone")

        tool = SwitchProjectTool(catalog=catalog, rebind=_boom)
        step = MagicMock()
        step.tool_input = {"project": "beacon"}
        parsed = eval(asyncio.run(tool.handle(step)).content)  # noqa: S307
        assert parsed["error"]["code"] == "rebind_failed"
        assert "still in the previous project" in parsed["error"]["message"]
