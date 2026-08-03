"""The bundled ``generative-ui`` plugin — manifest, gating, and the tool itself.

Deliberately integration-shaped against the REAL plugin tree, like
``test_widget_builder_plugin.py``: the discovery pipeline (manifest parse →
``load_entry`` → capability gate → instantiation) is most of what can break
here, and none of it is exercised by constructing the tool class directly.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
from pathlib import Path

import pytest
from mewbo_core.builtin_plugins.generative_ui.nodes import GenerativeUISpec
from mewbo_core.builtin_plugins.generative_ui.present_ui import (
    GENERATIVE_UI_CAPABILITY,
    GENERATIVE_UI_EVENT,
    PRESENT_UI_TOOL_ID,
    GenerativeUIPayload,
    PresentUiArgs,
    PresentUiTool,
)
from mewbo_core.classes import ActionStep
from mewbo_core.tooling.session_tools import (
    DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS,
    SessionToolFactory,
    SessionToolRegistry,
)
from pydantic import ValidationError

CAPS = (GENERATIVE_UI_CAPABILITY,)

SIMPLE_TREE = {
    "root": [
        {"component": "Heading", "value": "Status"},
        {"component": "Badge", "label": "green", "status": "success"},
    ]
}


def _plugin_root() -> Path:
    traversable = (
        importlib.resources.files("mewbo_core") / "builtin_plugins" / "generative_ui"
    )
    return Path(str(traversable))


def _manifest_entry() -> dict:
    raw = json.loads(
        (_plugin_root() / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    return raw["session_tools"][0]


def _registry() -> SessionToolRegistry:
    """A registry loaded exactly the way the orchestrator loads this plugin."""
    reg = SessionToolRegistry()
    reg.load_entry(_manifest_entry(), requires_capabilities=CAPS)
    return reg


def _step(**tool_input) -> ActionStep:
    return ActionStep(
        tool_id=PRESENT_UI_TOOL_ID, operation="run", tool_input=tool_input
    )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_parses_with_the_generative_ui_capability(self):
        from mewbo_core.tooling.plugins import parse_plugin_manifest

        manifest = parse_plugin_manifest(_plugin_root())
        assert manifest is not None
        assert manifest.name == "generative-ui"
        assert manifest.requires_capabilities == (GENERATIVE_UI_CAPABILITY,)

    def test_manifest_declares_present_ui_as_a_default_on_session_tool(self):
        entry = _manifest_entry()
        assert entry["tool_id"] == PRESENT_UI_TOOL_ID
        assert entry["module"].endswith("generative_ui.present_ui")
        assert entry["class"] == "PresentUiTool"
        # Load-bearing: without it the capability gate is structurally
        # unreachable for a console session (any ``mcp_tools`` list, ``[]``
        # included, caps that gate) and the tool never reaches a root agent.
        assert entry["unconditional"] is True

    def test_the_plugin_contributes_no_agentdef_or_skill(self):
        """Capability gating has two enforcement surfaces; this plugin only has
        one to gate. The catalog surface (``filter_by_capabilities`` over
        AgentDefs and skills) is vacuous here BECAUSE the bundle ships neither —
        assert that rather than leave the reader to assume it."""
        assert not (_plugin_root() / "agents").exists()
        assert not (_plugin_root() / "skills").exists()

    def test_the_plugin_is_discovered_from_the_shipped_builtin_root(self):
        from mewbo_core.tooling.plugins import discover_builtin_plugins

        root = Path(str(importlib.resources.files("mewbo_core") / "builtin_plugins"))
        names = [
            pc.manifest.name for pc in discover_builtin_plugins(root) if pc.manifest
        ]
        assert "generative-ui" in names


# ---------------------------------------------------------------------------
# Capability gating — the session-tool build surface
# ---------------------------------------------------------------------------


class TestCapabilityGating:
    """Six scenarios covering every route a session can take to this tool.

    The pair that matters most sits at the top: a console/Aura root ALWAYS
    sends some ``context.mcp_tools`` (``useSessions`` creates with ``[]``), so
    it is a permissive session with an explicit allowlist — and the capability
    gate alone can never fire for it.
    """

    def test_permissive_session_with_the_capability_gets_the_tool(self):
        ids = _registry().ids_for(
            [], session_capabilities=CAPS, strict_tool_scope=False
        )
        assert ids == [PRESENT_UI_TOOL_ID]

    def test_permissive_session_without_the_capability_does_not(self):
        assert _registry().ids_for([], session_capabilities=(), strict_tool_scope=False) == []

    def test_unscoped_session_without_the_capability_does_not(self):
        """The headless drives (wiki indexing, triggers, channels) reach here.

        They can render nothing, so binding eleven component schemas on every
        one of their LLM calls would be pure cost. This is what the capability
        buys once ``unconditional`` has relaxed the allowlist ceiling.
        """
        assert _registry().ids_for(None, session_capabilities=()) == []

    def test_unscoped_session_with_the_capability_gets_the_tool(self):
        assert _registry().ids_for(None, session_capabilities=CAPS) == [PRESENT_UI_TOOL_ID]

    def test_strict_scope_that_omits_the_tool_does_not_get_it(self):
        """An authoritative AgentDef ``tools:`` is the last word, capability or not."""
        ids = _registry().ids_for(
            ["shell"], session_capabilities=CAPS, strict_tool_scope=True
        )
        assert ids == []

    def test_strict_scope_that_names_the_tool_gets_it(self):
        ids = _registry().ids_for(
            [PRESENT_UI_TOOL_ID], session_capabilities=CAPS, strict_tool_scope=True
        )
        assert ids == [PRESENT_UI_TOOL_ID]

    def test_read_only_delegation_still_drops_it(self):
        """A session tool is an action; ``read_only`` admits none by default."""
        ids = _registry().ids_for(
            None, session_capabilities=CAPS, capability_mode="read_only"
        )
        assert ids == []

    def test_build_for_agrees_with_ids_for(self):
        """The catalog an operator reads and the tools an agent holds resolve
        through one algorithm — a drift between them is a lie in the operator's
        variable reference."""
        reg = _registry()
        for allowed, caps, strict in [
            ([], CAPS, False),
            ([], (), False),
            (None, CAPS, False),
            (None, (), False),
            ([PRESENT_UI_TOOL_ID], CAPS, True),
            (["shell"], CAPS, True),
        ]:
            built = reg.build_for(
                allowed,
                session_id="s1",
                event_logger=None,
                session_capabilities=caps,
                strict_tool_scope=strict,
            )
            named = reg.ids_for(
                allowed, session_capabilities=caps, strict_tool_scope=strict
            )
            assert [t.tool_id for t in built] == named

    def test_naming_the_tool_unlocks_its_capability(self):
        """``capabilities_for`` is the read-side mirror: selecting the tool by id
        grants the capability that gates it for that request."""
        assert _registry().capabilities_for([PRESENT_UI_TOOL_ID]) == CAPS

    def test_a_capability_free_unconditional_factory_is_unaffected(self):
        """``schedule_trigger``'s shape — unconditional with NO capabilities —
        must stay byte-identical now that the two flags compose."""
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="schedule_trigger", build=lambda *_: object(), unconditional=True
            )
        )
        assert reg.ids_for(None, session_capabilities=()) == ["schedule_trigger"]
        assert reg.ids_for([], session_capabilities=(), strict_tool_scope=False) == [
            "schedule_trigger"
        ]
        assert reg.ids_for(["x"], session_capabilities=(), strict_tool_scope=True) == []


# ---------------------------------------------------------------------------
# The tool contract
# ---------------------------------------------------------------------------


class TestToolContract:
    def test_load_entry_builds_the_real_tool_class(self):
        tools = _registry().build_for(
            [PRESENT_UI_TOOL_ID], session_id="s1", event_logger=None
        )
        assert len(tools) == 1
        tool = tools[0]
        assert isinstance(tool, PresentUiTool)
        assert tool.tool_id == PRESENT_UI_TOOL_ID
        assert tool.schema["function"]["name"] == PRESENT_UI_TOOL_ID
        assert tool.modes == frozenset({"act"})

    def test_presenting_never_terminates_the_run(self):
        """Verbatim replica of the loop's terminal poll.

        ``SessionTool`` is a structural Protocol, so a tool that omits
        ``terminal_reason`` crashes this poll with an ``AttributeError`` AFTER
        its side effect has already landed — how ``submit_widget`` broke every
        call it served. The panel renders off the event, so this tool never
        terminates and still defines the method.
        """
        tool = PresentUiTool(session_id="s1", event_logger=None)
        terminating = [t for t in [tool] if t.should_terminate_run()]
        done_reason = terminating[0].terminal_reason() if terminating else "completed"
        assert terminating == []
        assert done_reason == "completed"
        assert tool.terminal_reason() == "awaiting_approval"

    def test_result_cap_is_declared_not_inherited(self):
        """A session tool has no ``ToolSpec``, so an undeclared cap silently
        falls through to the registry's 2000 — which would truncate the
        validation error the model needs in order to fix its tree."""
        tool = PresentUiTool(session_id="s1", event_logger=None)
        declared = getattr(tool, "max_result_chars", None)
        assert isinstance(declared, int) and not isinstance(declared, bool)
        assert declared == 8_000
        assert declared != DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS


# ---------------------------------------------------------------------------
# handle() — the event it emits
# ---------------------------------------------------------------------------


class TestHandle:
    FROZEN_KEYS = {"ui_id", "session_id", "spec", "alt_text", "summary"}

    def test_a_successful_call_emits_one_frozen_generative_ui_event(self):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        result = asyncio.run(
            tool.handle(_step(spec=SIMPLE_TREE, summary="build status"))
        )

        assert len(events) == 1
        assert events[0]["type"] == GENERATIVE_UI_EVENT
        payload = events[0]["payload"]
        assert set(payload) == self.FROZEN_KEYS
        assert payload["session_id"] == "s1"
        assert payload["summary"] == "build status"
        assert payload["spec"] == GenerativeUISpec.model_validate(SIMPLE_TREE).to_wire()
        assert payload["ui_id"] in result.content

    def test_alt_text_is_computed_server_side_from_the_tree(self):
        """Every non-visual client renders this, so it must be derived from the
        same tree the console gets — never a second thing the model authored."""
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        asyncio.run(tool.handle(_step(spec=SIMPLE_TREE, summary="s")))
        assert events[0]["payload"]["alt_text"] == (
            GenerativeUISpec.model_validate(SIMPLE_TREE).to_text()
        )
        # Default heading level is 2, which renders one rank below the page title.
        assert events[0]["payload"]["alt_text"] == "### Status\n[green]"

    def test_the_result_is_a_receipt_not_an_echo(self):
        """The model just authored the tree; handing it back would spend context
        on what it already knows."""
        tool = PresentUiTool(session_id="s1", event_logger=None)
        result = asyncio.run(tool.handle(_step(spec=SIMPLE_TREE, summary="s")))
        assert "Status" not in result.content
        assert "component" not in result.content
        assert len(result.content) < 200
        assert "2 nodes" in result.content

    def test_a_minted_ui_id_matches_the_frozen_pattern(self):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        asyncio.run(tool.handle(_step(spec=SIMPLE_TREE, summary="s")))
        ui_id = events[0]["payload"]["ui_id"]
        assert len(ui_id) == 12
        assert ui_id.startswith("gui-")
        assert all(c in "0123456789abcdef" for c in ui_id[4:])

    def test_two_calls_mint_distinct_ids(self):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        asyncio.run(tool.handle(_step(spec=SIMPLE_TREE, summary="s")))
        asyncio.run(tool.handle(_step(spec=SIMPLE_TREE, summary="s")))
        assert events[0]["payload"]["ui_id"] != events[1]["payload"]["ui_id"]

    def test_a_supplied_ui_id_is_reused_so_the_panel_upserts(self):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        asyncio.run(
            tool.handle(_step(spec=SIMPLE_TREE, summary="s", ui_id="gui-0123abcd"))
        )
        assert events[0]["payload"]["ui_id"] == "gui-0123abcd"

    @pytest.mark.parametrize(
        "ui_id", ["nope", "gui-XYZ", "gui-0123abc", "gui-0123abcde", "0123abcd"]
    )
    def test_a_malformed_ui_id_is_refused(self, ui_id):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        result = asyncio.run(
            tool.handle(_step(spec=SIMPLE_TREE, summary="s", ui_id=ui_id))
        )
        assert result.content.startswith("ERROR: invalid present_ui args")
        assert events == []

    @pytest.mark.parametrize(
        "bad_input",
        [
            {},
            {"summary": "s"},
            {"spec": SIMPLE_TREE},
            {"spec": SIMPLE_TREE, "summary": ""},
            {"spec": {"root": []}, "summary": "s"},
            {"spec": SIMPLE_TREE, "summary": "s", "theme": "dark"},
            {"spec": {"root": [{"component": "Nope"}]}, "summary": "s"},
        ],
    )
    def test_invalid_args_return_a_correctable_error_and_emit_nothing(self, bad_input):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        result = asyncio.run(tool.handle(_step(**bad_input)))
        assert result.content.startswith("ERROR: invalid present_ui args")
        assert events == []

    def test_a_pathologically_deep_tree_is_refused_not_crashed(self):
        """The tree is recursive, so ``handle`` catching only ``ValidationError``
        rests on pydantic-core's own recursion guard reporting a stack overflow
        AS a validation error. Pin that rather than assume it — if it ever
        changes, this arm stops covering the input it was reasoned about."""
        node: dict[str, object] = {"component": "Text", "value": "leaf"}
        for _ in range(3_000):
            node = {"component": "Stack", "children": [node]}

        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        result = asyncio.run(tool.handle(_step(spec={"root": [node]}, summary="deep")))
        assert result.content.startswith("ERROR: invalid present_ui args")
        assert events == []

    def test_a_non_dict_tool_input_is_handled(self):
        tool = PresentUiTool(session_id="s1", event_logger=None)
        step = ActionStep(
            tool_id=PRESENT_UI_TOOL_ID, operation="run", tool_input="not a dict"
        )
        result = asyncio.run(tool.handle(step))
        assert result.content.startswith("ERROR: invalid present_ui args")

    def test_a_failing_event_logger_never_breaks_the_call(self):
        def boom(_event):
            raise RuntimeError("store is down")

        tool = PresentUiTool(session_id="s1", event_logger=boom)
        result = asyncio.run(tool.handle(_step(spec=SIMPLE_TREE, summary="s")))
        assert result.content.startswith("Presented UI gui-")

    def test_no_event_logger_degrades_silently(self):
        tool = PresentUiTool(session_id="s1", event_logger=None)
        result = asyncio.run(tool.handle(_step(spec=SIMPLE_TREE, summary="s")))
        assert result.content.startswith("Presented UI gui-")


# ---------------------------------------------------------------------------
# The payload's own contract
# ---------------------------------------------------------------------------


class TestPayload:
    def _valid(self, **overrides):
        base = dict(
            ui_id="gui-0123abcd",
            session_id="s1",
            spec={"root": []},
            alt_text="x",
            summary="y",
        )
        base.update(overrides)
        return base

    def test_round_trip_matches_the_frozen_wire_shape(self):
        dumped = GenerativeUIPayload(**self._valid()).model_dump()
        assert set(dumped) == {"ui_id", "session_id", "spec", "alt_text", "summary"}

    def test_rejects_an_extra_key(self):
        with pytest.raises(ValidationError):
            GenerativeUIPayload(**self._valid(), rendered=True)

    @pytest.mark.parametrize(
        "spec",
        [{}, {"nodes": []}, {"root": {}}, {"root": [], "theme": "dark"}],
    )
    def test_rejects_a_spec_that_is_not_the_frozen_shape(self, spec):
        with pytest.raises(ValidationError):
            GenerativeUIPayload(**self._valid(spec=spec))

    def test_rejects_a_malformed_ui_id(self):
        with pytest.raises(ValidationError):
            GenerativeUIPayload(**self._valid(ui_id="widget-1"))


class TestArgs:
    def test_summary_is_required_and_capped(self):
        with pytest.raises(ValidationError):
            PresentUiArgs(spec=SIMPLE_TREE, summary="x" * 201)
        assert PresentUiArgs(spec=SIMPLE_TREE, summary=" x " * 1).summary == "x"
