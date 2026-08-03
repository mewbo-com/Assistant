#!/usr/bin/env python3
"""Tests for the required-terminal gate at the tool-use-loop completion seam.

Drives the natural-completion branch with stub ``SessionTool``s that declare
``required_terminal`` / ``terminal_satisfied()`` — never a real plugin tool —
so the gate is exercised in isolation from the wiki-QA domain it was built for.
No real LLM anywhere; ``model.ainvoke`` is always an ``AsyncMock``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from mewbo_core.common import MockSpeaker
from mewbo_core.loop.tool_use_loop import _REQUIRED_TERMINAL_MAX_NUDGES, ToolUseLoop

# Sibling-helper reuse (tests/ is on sys.path under pytest).
from test_tool_use_loop import (
    _allow_all_policy,
    _make_agent_context,
    _make_context,
    _make_hook_manager,
    _make_registry,
    _make_spec,
    _text_response,
)


class _RequiredTerminalStub:
    """Standalone SessionTool (no shared base) declaring a required obligation.

    Deliberately NOT a subclass of anything in ``session_tools.py`` — the
    whole point of the gate is that it must work for an implementer that
    inherits no defaults from the structural ``SessionTool`` Protocol.
    """

    tool_id = "stub_required_terminal"
    schema: dict[str, object] = {
        "type": "function",
        "function": {
            "name": "stub_required_terminal",
            "description": "stub",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    modes = frozenset({"act"})
    required_terminal = True

    def __init__(self, *, satisfied: bool) -> None:
        self._satisfied = satisfied

    async def handle(self, action_step):
        return MockSpeaker(content="ok")

    def should_terminate_run(self) -> bool:
        return False

    def terminal_reason(self) -> str:
        return "completed"

    def terminal_satisfied(self) -> bool:
        return self._satisfied


class _BareSessionTool:
    """Standalone SessionTool declaring NEITHER knob — the undeclared shape."""

    tool_id = "bare_tool"
    schema: dict[str, object] = {
        "type": "function",
        "function": {
            "name": "bare_tool",
            "description": "stub",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    modes = frozenset({"act"})

    async def handle(self, action_step):
        return MockSpeaker(content="ok")

    def should_terminate_run(self) -> bool:
        return False

    def terminal_reason(self) -> str:
        return "completed"


def _run_with_stub(*, stub, model_responses):
    """Build + run a root loop carrying *stub* as an extra session tool."""
    spec = _make_spec("test_tool", "A test tool")
    registry = _make_registry(spec)

    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(side_effect=list(model_responses))
    bound = MagicMock()
    bound.ainvoke = fake_model.ainvoke

    ctx = _make_agent_context()

    with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = bound

        loop = ToolUseLoop(
            agent_context=ctx,
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id="sess-required-terminal",
            extra_session_tools=[stub],
        )
        tq, state = asyncio.run(
            loop.run("answer the question", tool_specs=[spec], context=_make_context())
        )
    return tq, state, fake_model


class TestRequiredTerminalGate:
    def test_unmet_obligation_nudges_then_exhausts_to_unmet_goal(self):
        """Never-satisfied obligation → bounded nudges, then honest unmet_goal.

        Never ``completed`` — the reply text was never actually delivered
        through the required tool, so accepting it as a clean completion
        would repeat the exact defect this gate exists to close.
        """
        turns = _REQUIRED_TERMINAL_MAX_NUDGES + 1
        responses = [_text_response(f"draft {i}") for i in range(turns)]
        stub = _RequiredTerminalStub(satisfied=False)

        tq, state, fake_model = _run_with_stub(stub=stub, model_responses=responses)

        assert state.done_reason == "unmet_goal"
        assert fake_model.ainvoke.await_count == turns
        # The text is still returned honestly, never swallowed — mirrors the
        # verification_failed arm's "keep the text honest" contract.
        assert tq.task_result == f"draft {turns - 1}"

    def test_satisfied_obligation_completes_with_zero_injections(self):
        """A satisfied required terminal → unchanged completed, ZERO injections.

        The happy path must never pay for this gate.
        """
        stub = _RequiredTerminalStub(satisfied=True)

        tq, state, fake_model = _run_with_stub(
            stub=stub, model_responses=[_text_response("the whole answer")]
        )

        assert state.done_reason == "completed"
        assert fake_model.ainvoke.await_count == 1
        assert tq.task_result == "the whole answer"

    def test_standalone_tool_declaring_neither_knob_is_inert(self):
        """A SessionTool declaring neither knob ⇒ no AttributeError, gate inert.

        Pins the structural-Protocol trap the declaration law names: a
        standalone implementer inherits no defaults, so silence must resolve
        to "no obligation" via ``getattr``, never a crash.
        """
        stub = _BareSessionTool()

        tq, state, fake_model = _run_with_stub(
            stub=stub, model_responses=[_text_response("done, no tool needed")]
        )

        assert state.done_reason == "completed"
        assert fake_model.ainvoke.await_count == 1
        assert tq.task_result == "done, no tool needed"

    def test_no_extra_session_tools_gate_is_inert(self):
        """A run with no ``required_terminal`` tool bound at all behaves as before."""
        tq, state, fake_model = _run_with_stub(
            stub=_BareSessionTool(),  # present but declares nothing; see above
            model_responses=[_text_response("plain completion")],
        )
        assert state.done_reason == "completed"


class TestUnmetRequiredTerminalsHelper:
    """Direct unit coverage of ``_unmet_required_terminals`` — no model call."""

    def _build_loop(self, extra_session_tools):
        with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = MagicMock()
            return ToolUseLoop(
                agent_context=_make_agent_context(),
                tool_registry=_make_registry(),
                permission_policy=_allow_all_policy(),
                hook_manager=_make_hook_manager(),
                session_id="sess-helper",
                extra_session_tools=extra_session_tools,
            )

    def test_missing_or_raising_check_counts_as_unmet(self):
        """A declared obligation with no callable check, or one that raises,
        is fail-closed to UNMET rather than silently skipped."""

        class _NoCheck:
            tool_id = "no_check"
            required_terminal = True

        class _RaisingCheck:
            tool_id = "raising_check"
            required_terminal = True

            def terminal_satisfied(self) -> bool:
                raise RuntimeError("boom")

        loop = self._build_loop([_NoCheck(), _RaisingCheck()])
        assert set(loop._unmet_required_terminals()) == {"no_check", "raising_check"}

    def test_undeclared_and_falsy_and_satisfied_report_nothing(self):
        """Undeclared, explicitly falsy, and satisfied tools all contribute nothing."""

        class _Undeclared:
            tool_id = "undeclared"

        class _FalsyDeclaration:
            tool_id = "falsy"
            required_terminal = False

        loop = self._build_loop(
            [_Undeclared(), _FalsyDeclaration(), _RequiredTerminalStub(satisfied=True)]
        )
        assert loop._unmet_required_terminals() == []
