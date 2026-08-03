#!/usr/bin/env python3
"""Integration tests: the safety plane wired through the real ToolUseLoop.

These drive the ACTUAL tool-execution chokepoint (``_execute_tool_call``),
never a mock of it — a fake gate would prove nothing about whether a root
agent can reach past the real one. Model calls are stubbed (the I/O boundary);
everything from the model's tool call through the gate to the emitted event is
real production code.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.safety.plane import SafetyPlane
from test_tool_use_loop import (
    _allow_all_policy,
    _make_agent_context,
    _make_context,
    _make_hook_manager,
    _make_registry,
    _make_spec,
    _text_response,
    _tool_call_response,
)


def _write_policy(root: Path, name: str, body: str) -> None:
    policy_dir = root / ".mewbo" / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / f"{name}.md").write_text(body, encoding="utf-8")


def _run_loop(
    *,
    safety_plane,
    tool_call_response,
    follow_up_response=None,
    tool_id: str = "file_edit_tool",
    capability: str | None = "write",
):
    """Run one real ToolUseLoop turn against a stubbed model, real gate."""
    base_spec = _make_spec(tool_id, capability=capability)
    # Wrap the factory in a trackable mock (a plain lambda has no call
    # assertions) so a test can prove the tool was never INSTANTIATED, not
    # merely that its result reads as a failure. ToolSpec is frozen, so a
    # tracked replacement is built via ``dataclasses.replace``.
    spec = dataclasses.replace(base_spec, factory=MagicMock(side_effect=base_spec.factory))
    registry = _make_registry(spec)

    responses = [tool_call_response]
    if follow_up_response is not None:
        responses.append(follow_up_response)
    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(side_effect=responses)
    bound = MagicMock()
    bound.ainvoke = fake_model.ainvoke

    events: list[dict] = []

    with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = bound

        loop = ToolUseLoop(
            agent_context=_make_agent_context(event_logger=events.append),
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            safety_plane=safety_plane,
        )
        tq, state = asyncio.run(
            loop.run("do the thing", tool_specs=[spec], context=_make_context())
        )
    return tq, state, events, spec


class TestHeadlineBypass:
    """A root agent cannot disable, edit, bypass or reconfigure its own policy."""

    def test_write_to_own_policy_file_is_blocked_and_never_executes(self, tmp_path: Path):
        _write_policy(
            tmp_path,
            "house-rules",
            """---
name: house-rules
description: test
rules: []
---
body
""",
        )
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None

        target = str(tmp_path / ".mewbo" / "policy" / "house-rules.md")
        _, state, events, spec = _run_loop(
            safety_plane=plane,
            tool_call_response=_tool_call_response(
                "file_edit_tool", {"file_path": target, "new": "enabled: false"}
            ),
            follow_up_response=_text_response("I could not edit that file."),
        )

        # The mocked tool's factory (spec.factory) must NEVER have been
        # invoked to build a live tool instance — the call was refused before
        # any side effect, not merely reported as failed after one.
        spec.factory.assert_not_called()

        # A deny event was emitted naming the self-protection rule.
        deny_events = [
            e
            for e in events
            if e.get("type") == "safety_plane" and e.get("payload", {}).get("phase") == "deny"
        ]
        assert len(deny_events) == 1
        assert deny_events[0]["payload"]["rule"] == "safety-plane-integrity"

    def test_shell_deletion_of_policy_dir_is_blocked(self, tmp_path: Path):
        _write_policy(tmp_path, "house-rules", "---\nname: house-rules\ndescription: t\n---\nb\n")
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None

        _, state, events, spec = _run_loop(
            safety_plane=plane,
            tool_call_response=_tool_call_response(
                "file_edit_tool", {"command": "rm -rf .mewbo/policy"}
            ),
            follow_up_response=_text_response("Refused."),
            tool_id="aider_shell_tool",
            capability="execute",
        )
        spec.factory.assert_not_called()

    def test_the_plane_is_not_a_bound_tool(self, tmp_path: Path):
        """No tool schema exposes the safety plane to the model at all."""
        _write_policy(tmp_path, "house-rules", "---\nname: house-rules\ndescription: t\n---\nb\n")
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None

        spec = _make_spec("read_file", capability="read")
        registry = _make_registry(spec)
        fake_model = MagicMock()
        fake_model.ainvoke = AsyncMock(return_value=_text_response("done"))
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke

        with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound
            loop = ToolUseLoop(
                agent_context=_make_agent_context(),
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
                safety_plane=plane,
            )
            asyncio.run(loop.run("hi", tool_specs=[spec], context=_make_context()))

        bind_call_args = mock_build.return_value.bind_tools.call_args
        bound_schemas = bind_call_args[0][0] if bind_call_args and bind_call_args[0] else []
        bound_names = {
            (s.get("name") if isinstance(s, dict) else getattr(s, "name", None))
            for s in bound_schemas
        }
        assert not any(
            name and "safety" in str(name).lower() for name in bound_names if name
        )

    def test_editing_the_document_mid_session_does_not_change_the_running_plane(
        self, tmp_path: Path
    ):
        """The plane is loaded ONCE and held by value — a file edit after load
        (however it happened) cannot reach the object already governing this
        session."""
        _write_policy(
            tmp_path,
            "house-rules",
            """---
name: house-rules
description: test
rules:
  - kind: tool.match
    name: no-payments
    decision: deny
    tools: ["mcp__payments__.*"]
---
body
""",
        )
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None
        original_rule_count = len(plane.rules)

        # Simulate the document being weakened on disk AFTER the plane loaded.
        _write_policy(
            tmp_path, "house-rules", "---\nname: house-rules\ndescription: t\nrules: []\n---\nb\n"
        )

        # The in-memory plane is unaffected — no re-read happens.
        assert len(plane.rules) == original_rule_count
        from mewbo_core.safety.spec import ToolCallObservation

        verdict = plane.evaluate_tool_call(
            ToolCallObservation(tool_id="mcp__payments__refund", capability="execute")
        )
        assert verdict is not None
        assert verdict.decision == "deny"


class TestInertnessAtTheLoop:
    def test_no_plane_means_no_disclosure_event_and_no_gate_cost(self):
        _, state, events, spec = _run_loop(
            safety_plane=None,
            tool_call_response=_text_response("plain response, no tool call"),
        )
        assert not [e for e in events if e.get("type") == "safety_plane"]

    def test_the_plane_never_reaches_the_model_context(self, tmp_path: Path):
        """The disclosure/deny events go to the transcript, never ``messages``.

        Every safety-plane emission is a ``self._emit_event`` call — the
        transcript/UI channel — never an append to the message list the model
        sees. Proven here rather than asserted: build an active plane with
        SEVERAL rules and a long prose body, then diff the exact messages sent
        to the model against a run with no plane at all. Byte-identical is
        what "zero token cost" means.
        """
        _write_policy(
            tmp_path,
            "house-rules",
            "---\nname: house-rules\ndescription: test\nrules:\n"
            + "\n".join(
                f'  - kind: tool.match\n    name: rule-{i}\n    decision: deny\n'
                f'    tools: ["mcp__blocked_{i}__.*"]'
                for i in range(10)
            )
            + "\n---\n"
            + "Long rationale body that would cost real tokens if it ever reached a prompt. " * 50,
        )
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None
        assert len(plane.rules) == 11  # 10 document rules + the built-in guard

        def captured_messages(active_plane):
            spec = _make_spec("read_file", capability="read")
            registry = _make_registry(spec)
            fake_model = MagicMock()
            fake_model.ainvoke = AsyncMock(return_value=_text_response("hi"))
            bound = MagicMock()
            bound.ainvoke = fake_model.ainvoke
            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                loop = ToolUseLoop(
                    agent_context=_make_agent_context(),
                    tool_registry=registry,
                    permission_policy=_allow_all_policy(),
                    hook_manager=_make_hook_manager(),
                    safety_plane=active_plane,
                )
                asyncio.run(loop.run("hi", tool_specs=[spec], context=_make_context()))
            sent = fake_model.ainvoke.call_args[0][0]
            return [(type(m).__name__, m.content) for m in sent]

        assert captured_messages(None) == captured_messages(plane)

    def test_active_plane_discloses_before_any_tool_call(self, tmp_path: Path):
        _write_policy(
            tmp_path,
            "house-rules",
            """---
name: house-rules
description: test
rules:
  - kind: tool.match
    name: no-payments
    tools: ["mcp__payments__.*"]
---
body
""",
        )
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None
        _, state, events, spec = _run_loop(
            safety_plane=plane,
            tool_call_response=_text_response("no tool call needed"),
        )
        disclosed = [
            e
            for e in events
            if e.get("type") == "safety_plane"
            and e.get("payload", {}).get("phase") == "disclosed"
        ]
        assert len(disclosed) == 1
        rule_names = [r["name"] for r in disclosed[0]["payload"]["rules"]]
        assert "no-payments" in rule_names
        assert "safety-plane-integrity" in rule_names


class TestSessionBudgetStopsTheRun:
    def test_turn_ceiling_stops_the_run_honestly(self, tmp_path: Path):
        _write_policy(
            tmp_path,
            "budget",
            """---
name: budget
description: test
rules:
  - kind: session.budget
    name: turn-ceiling
    decision: deny
    max_steps: 1
---
body
""",
        )
        plane = SafetyPlane.load(str(tmp_path), enabled=True)
        assert plane is not None
        spec = _make_spec("read_file", capability="read")
        registry = _make_registry(spec)
        fake_model = MagicMock()
        # The turn-boundary check runs BEFORE each model call, so with
        # ``max_steps: 1`` one tool-call turn is allowed to complete before
        # the ceiling stops the SECOND turn from ever reaching the model.
        fake_model.ainvoke = AsyncMock(
            return_value=_tool_call_response("read_file", {"input": "x"})
        )
        bound = MagicMock()
        bound.ainvoke = fake_model.ainvoke
        events: list[dict] = []

        with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound
            loop = ToolUseLoop(
                agent_context=_make_agent_context(event_logger=events.append),
                tool_registry=registry,
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
                safety_plane=plane,
            )
            tq, state = asyncio.run(
                loop.run("hi", tool_specs=[spec], context=_make_context())
            )

        assert state.done_reason == "safety_blocked"
        deny_events = [
            e
            for e in events
            if e.get("type") == "safety_plane" and e.get("payload", {}).get("phase") == "deny"
        ]
        assert len(deny_events) == 1
        assert deny_events[0]["payload"]["rule"] == "turn-ceiling"
