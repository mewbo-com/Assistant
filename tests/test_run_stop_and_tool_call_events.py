#!/usr/bin/env python3
"""Two defects that were the same defect seen from two sides.

Both are about the loop's observability and controllability WHILE a step is in
flight, as opposed to between steps.

1. **Tool events were retroactive.** The engine emitted only ``tool_result``, at
   the tail of a tool call, so a call taking a minute was indistinguishable from
   no call at all for that minute. A ``tool_call`` event now precedes the
   dispatch, paired to its outcome by ``tool_call_id``.

2. **A stop waited out the step it landed in.** ``should_cancel`` was polled at
   exactly ONE place — the top of the turn loop — so a stop requested during a
   model generation or a tool execution was invisible until that whole turn
   settled. The same predicate now also guards both in-flight awaits.

The model boundary is the only thing stubbed. In particular the cancellation
predicate is a real callable over real loop state and the tool awaits are real
``asyncio`` waits, because the defect lived in WHEN the predicate is read — a
test that stubbed the read would prove nothing.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.agents.hypervisor import AgentHypervisor
from mewbo_core.hooks import HookManager
from mewbo_core.loop.cancellation import CancellationSignal, RunCancelled
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.session.context import ContextSnapshot
from mewbo_core.session.token_budget import TokenBudget
from mewbo_core.tooling.tool_registry import ToolRegistry, ToolSpec

# A guard poll interval far below any assertion's patience, so a test measures
# "did the stop land promptly" rather than "did it land within the default".
FAST_POLL = 0.01

# ---------------------------------------------------------------------------
# Helpers (mirroring the conventions in test_tool_use_loop_integration.py)
# ---------------------------------------------------------------------------


def _make_context() -> ContextSnapshot:
    return ContextSnapshot(
        summary=None,
        recent_events=[],
        selected_events=None,
        events=[],
        budget=TokenBudget(
            total_tokens=0,
            summary_tokens=0,
            event_tokens=0,
            context_window=128000,
            remaining_tokens=128000,
            utilization=0.0,
            threshold=0.8,
        ),
    )


def _make_spec(
    tool_id: str, *, concurrency_safe: bool = True, timeout: float = 120.0
) -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"tool {tool_id}",
        factory=lambda: MagicMock(),
        enabled=True,
        kind="local",
        concurrency_safe=concurrency_safe,
        timeout=timeout,
        metadata={
            "schema": {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            }
        },
    )


def _allow_all_policy() -> PermissionPolicy:
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    return policy


def _make_hook_manager() -> HookManager:
    hm = MagicMock(spec=HookManager)
    hm.run_pre_tool_use.side_effect = lambda step: step
    hm.run_post_tool_use.side_effect = lambda step, result: result
    hm.run_permission_request.side_effect = lambda step, decision: decision
    return hm


def _tool_call_response(tool_id: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_id, "args": {"input": "x"}, "id": call_id}],
    )


def _build_loop(
    registry: ToolRegistry,
    *,
    should_cancel=None,
    event_logger=None,
) -> ToolUseLoop:
    ctx = AgentContext.root(
        model_name="test-model",
        max_depth=5,
        should_cancel=should_cancel,
        registry=AgentHypervisor(max_concurrent=100),
        event_logger=event_logger,
    )
    loop = ToolUseLoop(
        agent_context=ctx,
        tool_registry=registry,
        permission_policy=_allow_all_policy(),
        hook_manager=_make_hook_manager(),
    )
    # Tighten the guard's poll so an assertion measures promptness rather than
    # waiting out the production interval. Injected, not monkeypatched: the
    # signal takes its interval as a constructor argument for exactly this.
    loop._cancellation = CancellationSignal(should_cancel, poll_interval=FAST_POLL)
    return loop


def _events_of(events: list[dict], etype: str) -> list[dict]:
    return [e for e in events if e.get("type") == etype]


# ---------------------------------------------------------------------------
# CancellationSignal — the primitive, with no loop around it
# ---------------------------------------------------------------------------


class TestCancellationSignal:
    """The stop predicate as a barrier an in-flight await can lose to."""

    def test_no_predicate_is_permanently_inert(self):
        """Every caller without a run registry must be unaffected.

        The CLI's direct ``Orchestrator`` use and most tests pass no predicate.
        An inert signal is what lets the guarded call sites carry no None-check
        of their own.
        """
        signal = CancellationSignal(None)
        assert signal.inert is True
        assert signal.requested is False

        async def _work() -> str:
            return "value"

        assert asyncio.run(signal.guard(_work())) == "value"

    def test_raising_predicate_reads_as_not_requested(self):
        """A fault READING the flag must not itself terminate the run.

        The flag is a best-effort steering signal. Letting an exception escape
        here would turn a broken predicate into a spurious cancellation, which
        is strictly worse than missing a stop the operator can re-click.
        """

        def _boom() -> bool:
            raise RuntimeError("registry went away")

        assert CancellationSignal(_boom).requested is False

    def test_guard_aborts_an_in_flight_await_promptly(self):
        """The whole point: the wrapped work does NOT get to finish.

        A poll-only design would have returned "finished" here after the full
        sleep, which is exactly the observed defect — Stop clicked, run carries
        on to the end of what it was doing.
        """
        stop = False
        finished: list[str] = []

        def _predicate() -> bool:
            return stop

        async def _long() -> str:
            await asyncio.sleep(5)
            finished.append("finished")
            return "finished"

        async def _drive():
            nonlocal stop
            signal = CancellationSignal(_predicate, poll_interval=FAST_POLL)
            task = asyncio.ensure_future(signal.guard(_long()))
            await asyncio.sleep(FAST_POLL)
            stop = True
            started = time.monotonic()
            with pytest.raises(RunCancelled):
                await task
            return time.monotonic() - started

        elapsed = asyncio.run(_drive())
        assert finished == [], "the guarded work was allowed to run to completion"
        assert elapsed < 1.0, f"stop took {elapsed:.2f}s, i.e. it waited on the work"

    def test_guard_refuses_before_starting_when_already_requested(self):
        """A stop that arrived a moment ago must not buy one more call.

        Without the pre-check, a flag set while the PREVIOUS await was unwinding
        would be missed and the next unbounded call would start anyway.
        """
        started: list[str] = []

        async def _work() -> str:
            started.append("started")
            return "value"

        signal = CancellationSignal(lambda: True, poll_interval=FAST_POLL)
        with pytest.raises(RunCancelled):
            asyncio.run(signal.guard(_work()))
        assert started == [], "guarded work started despite a pending stop"

    def test_guard_propagates_the_wrapped_exception_unchanged(self):
        """Every existing handler around a guarded call must keep working.

        The model call is wrapped in ``except LlmResilienceExhausted``; if the
        guard swallowed or re-wrapped exceptions, that handler would stop
        firing and a real model failure would read as something else.
        """

        class Sentinel(Exception):
            pass

        async def _work() -> str:
            raise Sentinel("from the wrapped awaitable")

        signal = CancellationSignal(lambda: False, poll_interval=FAST_POLL)
        with pytest.raises(Sentinel):
            asyncio.run(signal.guard(_work()))


# ---------------------------------------------------------------------------
# Stop latency inside a real loop turn
# ---------------------------------------------------------------------------


class TestStopLandsMidStep:
    """A stop must not wait out the model call or the tool it landed in."""

    def test_stop_during_a_tool_call_does_not_wait_for_the_tool(self):
        """The reported symptom, reproduced and closed.

        The model returns one tool call; the tool sleeps far longer than the
        test's patience. The stop is requested once the tool is demonstrably
        running (its ``tool_call`` event has been emitted), and the run must
        terminate as ``canceled`` without the tool ever completing.
        """
        spec = _make_spec("slow_tool")
        registry = ToolRegistry()
        registry.register(spec)

        stop = False
        tool_completed: list[str] = []
        events: list[dict] = []

        def _predicate() -> bool:
            return stop

        async def _slow(_step):
            await asyncio.sleep(5)
            tool_completed.append("completed")
            return MagicMock(content="never reached")

        mock_tool = MagicMock()
        mock_tool.arun = _slow

        bound = MagicMock()
        bound.ainvoke = AsyncMock(return_value=_tool_call_response("slow_tool", "tc1"))

        async def _drive():
            nonlocal stop
            with (
                patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build,
                patch.object(registry, "get", return_value=mock_tool),
            ):
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                loop = _build_loop(
                    registry, should_cancel=_predicate, event_logger=events.append
                )
                run = asyncio.ensure_future(
                    loop.run("do stuff", tool_specs=[spec], context=_make_context())
                )
                # Wait for proof the tool is actually in flight before stopping,
                # so this asserts mid-tool cancellation rather than racing the
                # between-turns check that already worked.
                for _ in range(200):
                    if _events_of(events, "tool_call"):
                        break
                    await asyncio.sleep(FAST_POLL)
                assert _events_of(events, "tool_call"), "tool never started"
                stop = True
                started = time.monotonic()
                _tq, state = await run
                return state, time.monotonic() - started

        state, elapsed = asyncio.run(_drive())

        assert state.done is True
        assert state.done_reason == "canceled"
        assert tool_completed == [], "the tool ran to completion after a stop"
        assert elapsed < 2.0, f"stop took {elapsed:.2f}s — it waited on the tool"

    def test_stop_during_the_model_call_does_not_wait_for_the_model(self):
        """A generation can span the whole resilience ladder.

        A stop requested inside that window used to be invisible until the
        ladder settled, which on a retrying model is minutes.
        """
        spec = _make_spec("test_tool")
        registry = ToolRegistry()
        registry.register(spec)

        stop = False
        model_returned: list[str] = []

        def _predicate() -> bool:
            return stop

        async def _slow_invoke(*_args, **_kwargs):
            await asyncio.sleep(5)
            model_returned.append("returned")
            return AIMessage(content="never reached")

        bound = MagicMock()
        bound.ainvoke = _slow_invoke

        async def _drive():
            nonlocal stop
            with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
                mock_build.return_value = MagicMock()
                mock_build.return_value.bind_tools.return_value = bound
                loop = _build_loop(registry, should_cancel=_predicate)
                run = asyncio.ensure_future(
                    loop.run("do stuff", tool_specs=[spec], context=_make_context())
                )
                await asyncio.sleep(FAST_POLL * 5)
                stop = True
                started = time.monotonic()
                _tq, state = await run
                return state, time.monotonic() - started

        state, elapsed = asyncio.run(_drive())

        assert state.done is True
        assert state.done_reason == "canceled"
        assert model_returned == [], "the model call was allowed to finish"
        assert elapsed < 2.0, f"stop took {elapsed:.2f}s — it waited on the model"

    def test_a_run_with_no_stop_is_unaffected(self):
        """The guard must be transparent on the ordinary path.

        A guard that also changed the non-cancelled path would trade one defect
        for a subtler one, so the happy path is pinned explicitly.
        """
        spec = _make_spec("test_tool")
        registry = ToolRegistry()
        registry.register(spec)

        bound = MagicMock()
        bound.ainvoke = AsyncMock(return_value=AIMessage(content="all done"))

        with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound
            loop = _build_loop(registry, should_cancel=lambda: False)
            _tq, state = asyncio.run(
                loop.run("do stuff", tool_specs=[spec], context=_make_context())
            )

        assert state.done is True
        assert state.done_reason != "canceled"


# ---------------------------------------------------------------------------
# The initiation event
# ---------------------------------------------------------------------------


class TestToolCallEvent:
    """A tool call must be visible while it runs, not only once it finished."""

    def _drive_one_tool_call(self, *, call_id: str = "tc1") -> list[dict]:
        """Run one turn that executes one tool; return the emitted events."""
        spec = _make_spec("test_tool")
        registry = ToolRegistry()
        registry.register(spec)
        events: list[dict] = []

        mock_tool = MagicMock()
        mock_tool.arun = AsyncMock(return_value=MagicMock(content="tool output"))

        bound = MagicMock()
        bound.ainvoke = AsyncMock(
            side_effect=[
                _tool_call_response("test_tool", call_id),
                AIMessage(content="done"),
            ]
        )

        with (
            patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound
            loop = _build_loop(registry, event_logger=events.append)
            asyncio.run(loop.run("do stuff", tool_specs=[spec], context=_make_context()))
        return events

    def test_tool_call_precedes_its_result(self):
        """Ordering IS the feature — an initiation event after the fact is nothing."""
        events = self._drive_one_tool_call()
        types = [e.get("type") for e in events]
        assert "tool_call" in types, "no initiation event was emitted"
        assert "tool_result" in types
        assert types.index("tool_call") < types.index("tool_result")

    def test_tool_call_is_emitted_before_the_tool_returns(self):
        """Stronger than ordering: the event must precede the AWAIT, not follow it.

        Emitting both at the tail would satisfy the ordering assertion above
        while leaving the defect fully in place, so this observes the event from
        INSIDE the running tool.
        """
        spec = _make_spec("test_tool")
        registry = ToolRegistry()
        registry.register(spec)
        events: list[dict] = []
        seen_while_running: list[str] = []

        async def _observe(_action_step):
            seen_while_running.extend(
                str(e["payload"].get("tool_id")) for e in _events_of(events, "tool_call")
            )
            return MagicMock(content="tool output")

        mock_tool = MagicMock()
        mock_tool.arun = _observe

        bound = MagicMock()
        bound.ainvoke = AsyncMock(
            side_effect=[
                _tool_call_response("test_tool", "tc1"),
                AIMessage(content="done"),
            ]
        )

        with (
            patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build,
            patch.object(registry, "get", return_value=mock_tool),
        ):
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = bound
            loop = _build_loop(registry, event_logger=events.append)
            asyncio.run(loop.run("do stuff", tool_specs=[spec], context=_make_context()))

        assert seen_while_running == ["test_tool"], (
            "the tool could not see its own tool_call event, so the event is "
            "still being emitted after the call rather than before it"
        )

    def test_the_pair_shares_one_correlation_key(self):
        """Without a shared key a client cannot settle the pending row in place.

        It would render the call twice — once pending, once settled — which is
        worse than the retroactive single row it replaced.
        """
        events = self._drive_one_tool_call(call_id="tc-abc")
        call = _events_of(events, "tool_call")[0]["payload"]
        result = _events_of(events, "tool_result")[0]["payload"]
        assert call["tool_call_id"] == "tc-abc"
        assert result["tool_call_id"] == "tc-abc"
        assert call["tool_id"] == result["tool_id"] == "test_tool"

    def test_a_call_that_never_returns_still_left_a_record(self):
        """The initiation event is the only trace a killed call leaves.

        A tool that times out emits its ``tool_result`` from the wrapper, but a
        process that dies mid-call emits nothing at all — before this event
        existed the transcript showed the step had never happened.
        """
        # A timeout short enough to fire, exercising the wrapper's own emit path.
        spec = _make_spec("slow_tool", timeout=0.05)
        registry = ToolRegistry()
        registry.register(spec)
        events: list[dict] = []

        async def _slow(_step):
            await asyncio.sleep(10)

        mock_tool = MagicMock()
        mock_tool.arun = _slow

        async def _drive():
            with patch.object(registry, "get", return_value=mock_tool):
                loop = _build_loop(registry, event_logger=events.append)
                return await loop._safe_execute(
                    {"name": "slow_tool", "args": {}, "id": "tc-timeout"}, [spec]
                )

        result = asyncio.run(_drive())

        assert result.success is False
        call = _events_of(events, "tool_call")
        assert len(call) == 1, "a timed-out call left no initiation event"
        assert call[0]["payload"]["tool_call_id"] == "tc-timeout"
        # The wrapper's failure emit must carry the same key, or the pending row
        # is never settled and the UI shows a call running forever.
        timeout_result = _events_of(events, "tool_result")[0]["payload"]
        assert timeout_result["tool_call_id"] == "tc-timeout"
        assert timeout_result["success"] is False
