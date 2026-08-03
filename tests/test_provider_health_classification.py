#!/usr/bin/env python3
"""Provider-health classification, the split call bound, and the budget law.

Covers the seam that decides whether a failed model call is worth retrying:
``ProviderFault`` (structured provider codes), the classifier ordering that lets
a code outrank an exception type, the small rate-limit budget, the circuit
breaker's provider-declared trip, ``CallDeadline`` (time-to-first-token vs
stream-idle) and the config-load budget-law checks.

No live model and no network: exceptions are built by hand with the envelope
shapes providers actually return, and every clock is injected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import litellm.exceptions as lx
import pytest
from langchain_core.messages import AIMessage
from mewbo_core.config import AgentConfig, AppConfig
from mewbo_core.llm.llm import build_chat_model
from mewbo_core.llm.llm_resilience import (
    DEFAULT_QUOTA_COOLDOWN,
    CallDeadline,
    CircuitBreaker,
    LlmResilienceExhausted,
    ProviderFault,
    QuotaFault,
    RateLimitFault,
    RetryAction,
    RetryBudget,
    RetryStrategy,
    UnavailableFault,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# The live gateway envelope this work exists to classify: an ordinary 429 whose
# structured type names an exhausted allocation pool, and whose PROSE matches
# none of the substring hints.
QUOTA_BODY = {
    "error": {
        "message": "Your token-plan 1-week quota has been exhausted.",
        "type": "Throttling.AllocationQuota",
        "code": "429",
    }
}


def _rate_limit(body: dict[str, Any] | None = None, **attrs: Any) -> lx.RateLimitError:
    """A litellm 429 carrying *body* the way a proxy embeds it — in the message."""
    exc = lx.RateLimitError(
        message="litellm.RateLimitError: OpenAIException - " + json.dumps(body or QUOTA_BODY),
        llm_provider="openai",
        model="claude-sonnet-5",
    )
    for key, value in attrs.items():
        setattr(exc, key, value)
    return exc


class _Response:
    """Minimal stand-in for the httpx response hung off a provider exception."""

    def __init__(self, headers: dict[str, str], status_code: int = 429) -> None:
        self.headers = headers
        self.status_code = status_code


async def _never_compact() -> bool:
    return False


def _drive(strategy: RetryStrategy, models: list[str], invoke, events: list[dict]):
    """Run one logical call through *strategy*, collecting emitted events."""
    return asyncio.run(
        strategy.run(
            models=models,
            invoke=invoke,
            emit=events.append,
            compact=_never_compact,
            agent_id="a1",
            depth=0,
            step=1,
        )
    )


class TestProviderFaultParsing:
    def test_reads_the_code_out_of_a_body_embedded_in_the_message(self):
        fault = ProviderFault.from_exception(_rate_limit())
        assert isinstance(fault, QuotaFault)
        # The provider's code is kept verbatim so an operator can match it
        # against the gateway log they are staring at.
        assert fault.code == "Throttling.AllocationQuota"

    def test_reads_the_code_off_a_body_attribute(self):
        exc = _rate_limit(body={"error": {"code": "insufficient_quota"}})
        # The attribute wins over the message: same exception, same verdict.
        assert isinstance(ProviderFault.from_exception(exc), QuotaFault)

    def test_quota_outranks_the_throttling_token_it_contains(self):
        """``Throttling.AllocationQuota`` folds to a string containing ``throttling``.

        Resolving rate-limit-first would read a spent weekly pool as a momentary
        throttle and retry a model that cannot succeed — the exact live failure.
        """
        assert isinstance(ProviderFault.from_exception(_rate_limit()), QuotaFault)

    def test_plain_throttling_stays_a_rate_limit(self):
        exc = _rate_limit(body={"error": {"code": "rate_limit_exceeded"}})
        assert isinstance(ProviderFault.from_exception(exc), RateLimitFault)

    def test_no_deployment_code_is_unavailable(self):
        exc = _rate_limit(body={"error": {"code": "no_healthy_deployment"}})
        assert isinstance(ProviderFault.from_exception(exc), UnavailableFault)

    def test_unknown_code_declines_so_the_legacy_ladder_still_decides(self):
        exc = _rate_limit(body={"error": {"code": "teapot"}})
        assert ProviderFault.from_exception(exc) is None

    def test_no_envelope_at_all_declines(self):
        assert ProviderFault.from_exception(ValueError("plain")) is None
        assert ProviderFault.from_exception(asyncio.TimeoutError()) is None

    def test_the_base_is_not_constructible(self):
        """Every value this seam can produce IS one of the kinds.

        A constructible base could leak out of the parse seam carrying no
        ``kind``, leaving a caller with a fault it cannot act on — the union
        would then be neither discriminated nor closed.
        """
        with pytest.raises(TypeError):
            ProviderFault(code="anything")  # type: ignore[abstract]

    def test_declining_is_not_a_fault_of_unknown_kind(self):
        """There is deliberately no residual member.

        An unknown-kind fault would have to invent an action for a code nobody
        has read, and that guess would outrank the evidenced exception-type
        arms. ``None`` hands the decision back to them instead.
        """
        exc = _rate_limit(body={"error": {"code": "teapot"}})
        assert ProviderFault.from_exception(exc) is None
        # ...and the type ladder, not the code, is what answers.
        assert RetryStrategy.classify(exc, now=NOW).reason == "rate_limit"

    def test_html_error_page_does_not_parse_as_an_envelope(self):
        exc = lx.RateLimitError(
            message="<html><head><title>502</title></head><body>rate limit</body></html>",
            llm_provider="openai",
            model="m",
        )
        assert ProviderFault.from_exception(exc) is None

    def test_retry_after_header_is_read(self):
        exc = _rate_limit(
            body={"error": {"code": "rate_limit_exceeded"}},
            response=_Response({"retry-after": "9"}),
        )
        fault = ProviderFault.from_exception(exc)
        assert fault is not None
        assert fault.retry_after == 9.0
        assert fault.http_status == 429

    def test_absolute_epoch_reset_becomes_resets_at(self):
        epoch = NOW.timestamp() + 3 * 24 * 3600
        exc = _rate_limit(response=_Response({"x-ratelimit-reset": str(int(epoch))}))
        fault = ProviderFault.from_exception(exc)
        assert fault is not None
        assert fault.resets_at is not None
        assert fault.cooldown_seconds(NOW) == pytest.approx(3 * 24 * 3600, abs=1.0)

    def test_iso_reset_becomes_resets_at(self):
        exc = _rate_limit(response=_Response({"x-ratelimit-reset": "2026-01-04T12:00:00Z"}))
        fault = ProviderFault.from_exception(exc)
        assert fault is not None
        assert fault.cooldown_seconds(NOW) == pytest.approx(3 * 24 * 3600)

    def test_small_reset_number_is_relative_and_lands_in_retry_after(self):
        """The same header carries both spellings; magnitude decides which."""
        exc = _rate_limit(
            body={"error": {"code": "rate_limit_exceeded"}},
            response=_Response({"x-ratelimit-reset": "30"}),
        )
        fault = ProviderFault.from_exception(exc)
        assert fault is not None
        assert fault.resets_at is None
        assert fault.retry_after == 30.0

    def test_a_naive_reset_stamp_is_forced_to_utc_at_definition(self):
        fault = QuotaFault(code="x", resets_at=datetime(2026, 1, 1, 13, 0, 0))
        assert fault.resets_at is not None
        assert fault.resets_at.tzinfo is not None
        assert fault.cooldown_seconds(NOW) == pytest.approx(3600)

    def test_extra_fields_are_refused(self):
        with pytest.raises(Exception, match="extra"):
            QuotaFault(code="x", smuggled="value")

    def test_a_reset_already_past_never_goes_negative(self):
        fault = QuotaFault(code="x", resets_at=NOW - timedelta(hours=1))
        assert fault.cooldown_seconds(NOW) == 0.0


class TestProviderFaultDecisions:
    def test_quota_leaves_the_model_and_benches_it(self):
        decision = QuotaFault(code="x").decide("RateLimitError", NOW)
        assert decision.action is RetryAction.SWITCH_MODEL
        assert decision.reason == "quota_exhausted"
        assert decision.cooldown == DEFAULT_QUOTA_COOLDOWN

    def test_quota_bench_follows_the_declared_reset_not_the_default(self):
        fault = QuotaFault(code="x", resets_at=NOW + timedelta(days=3))
        decision = fault.decide("RateLimitError", NOW)
        assert decision.cooldown == pytest.approx(3 * 24 * 3600)

    def test_rate_limit_retries_the_same_model(self):
        decision = RateLimitFault(code="x", retry_after=5).decide("RateLimitError", NOW)
        assert decision.action is RetryAction.RETRY_SAME
        assert decision.reason == "rate_limit"
        assert decision.retry_after == 5
        # An inferred-health cooldown would bench a model whose pool is intact.
        assert decision.cooldown is None

    def test_unavailable_leaves_the_model(self):
        decision = UnavailableFault(code="x").decide("RateLimitError", NOW)
        assert decision.action is RetryAction.SWITCH_MODEL
        assert decision.reason == "no_deployments"
        assert decision.cooldown


class TestClassifierOrdering:
    def test_the_live_quota_envelope_no_longer_reads_as_a_plain_rate_limit(self):
        decision = RetryStrategy.classify(_rate_limit(), now=NOW)
        assert decision.action is RetryAction.SWITCH_MODEL
        assert decision.reason == "quota_exhausted"

    def test_a_structured_code_outranks_the_timeout_arm(self):
        """A timeout-typed error carrying a provider code is decided on the code.

        The timeout arm used to sit above every provider arm, so any error whose
        TYPE was timeout-flavoured was answered before its envelope was read.
        """

        class _TimeoutWithEnvelope(TimeoutError):
            body = {"error": {"code": "insufficient_quota"}}

        decision = RetryStrategy.classify(_TimeoutWithEnvelope(), now=NOW)
        assert decision.reason == "quota_exhausted"

    def test_cancellation_still_wins_over_everything(self):
        """Load-bearing for TaskGroup propagation — must stay the first arm."""

        class _CancelledWithEnvelope(asyncio.CancelledError):
            body = {"error": {"code": "insufficient_quota"}}

        decision = RetryStrategy.classify(_CancelledWithEnvelope(), now=NOW)
        assert decision.action is RetryAction.FATAL
        assert decision.reason == "cancelled"

    def test_a_bare_timeout_is_unchanged(self):
        decision = RetryStrategy.classify(asyncio.TimeoutError())
        assert decision.action is RetryAction.RETRY_SAME
        assert decision.reason == "timeout"

    def test_the_legacy_substring_arm_still_answers_a_codeless_error(self):
        decision = RetryStrategy.classify(
            lx.RateLimitError(
                message="No deployments available for selected model",
                llm_provider="openai",
                model="m",
            ),
            now=NOW,
        )
        assert decision.action is RetryAction.SWITCH_MODEL
        assert decision.reason == "no_deployments"


class TestCircuitBreakerTrip:
    def test_trip_opens_without_a_failure_streak(self):
        clock = lambda: 100.0  # noqa: E731
        breaker = CircuitBreaker(threshold=3, cooldown=30.0, clock=clock)
        breaker.trip("m", 3600.0)
        assert breaker.is_open("m") is True

    def test_a_declared_reset_outlasts_the_configured_cooldown(self):
        now = [0.0]
        breaker = CircuitBreaker(threshold=3, cooldown=30.0, clock=lambda: now[0])
        breaker.trip("m", 3 * 24 * 3600)
        now[0] = 600.0  # far past the configured cooldown
        assert breaker.is_open("m") is True

    def test_trip_ignores_the_streak_threshold_knob(self):
        """0 disables the streak HEURISTIC, not a provider's own declaration."""
        breaker = CircuitBreaker(threshold=0, cooldown=30.0, clock=lambda: 0.0)
        breaker.record_failure("m")
        assert breaker.is_open("m") is False
        breaker.trip("m", 60.0)
        assert breaker.is_open("m") is True

    def test_a_non_positive_cooldown_is_a_no_op(self):
        breaker = CircuitBreaker(clock=lambda: 0.0)
        breaker.trip("m", 0.0)
        assert breaker.is_open("m") is False


class TestQuotaSwitchingInTheChain:
    def test_a_quota_fault_switches_and_benches_the_model_for_the_run(self):
        calls: list[str] = []

        async def invoke(model: str, _is_fallback: bool) -> AIMessage:
            calls.append(model)
            if model == "primary":
                raise _rate_limit()
            return AIMessage(content="ok")

        strategy = RetryStrategy(
            timeout=5.0, primary_retries=3, turn_deadline=0, wall_clock=lambda: NOW
        )
        events: list[dict] = []
        _response, model = _drive(strategy, ["primary", "rescue"], invoke, events)

        # One attempt on the dead model, then away — no retrying a spent pool.
        assert calls == ["primary", "rescue"]
        assert model == "rescue"
        assert strategy.breaker.is_open("primary") is True
        assert [e["payload"]["reason"] for e in events if e["type"] == "llm_fallback"] == [
            "quota_exhausted"
        ]

    def test_an_inferred_switch_never_benches_the_model(self):
        """A context-window overflow says nothing about the model's health."""

        async def invoke(model: str, _is_fallback: bool) -> AIMessage:
            if model == "primary":
                raise lx.ContextWindowExceededError(
                    message="too long", model="m", llm_provider="openai"
                )
            return AIMessage(content="ok")

        strategy = RetryStrategy(timeout=5.0, primary_retries=2, turn_deadline=0)
        _drive(strategy, ["primary", "rescue"], invoke, [])
        assert strategy.breaker.is_open("primary") is False


class TestRateLimitBudget:
    @staticmethod
    def _strategy(**kwargs: Any) -> RetryStrategy:
        return RetryStrategy(
            timeout=5.0,
            primary_retries=9,
            fallback_retries=1,
            turn_deadline=0,
            backoff_base=0.0,
            backoff_cap=0.0,
            retry_after_cap=0.0,
            rng=lambda: 0.0,
            wall_clock=lambda: NOW,
            **kwargs,
        )

    def test_throttling_drains_the_small_bucket_then_moves_to_the_next_model(self):
        """A spent throttling budget is not a spent run — the next model has its
        own pool, so the chain advances instead of failing."""
        attempts: list[str] = []

        async def invoke(model: str, _is_fallback: bool) -> AIMessage:
            attempts.append(model)
            if model == "primary":
                raise _rate_limit(body={"error": {"code": "rate_limit_exceeded"}})
            return AIMessage(content="ok")

        strategy = self._strategy(rate_limit_budget=RetryBudget(capacity=4.0))
        _response, model = _drive(strategy, ["primary", "rescue"], invoke, [])

        # Capacity 4 refuses at half, so two retries then away — far short of the
        # nine attempts the transient cap would have allowed.
        assert attempts.count("primary") == 3
        assert model == "rescue"

    def test_the_transient_bucket_is_untouched_by_throttling(self):
        async def invoke(model: str, _is_fallback: bool) -> AIMessage:
            if model == "primary":
                raise _rate_limit(body={"error": {"code": "rate_limit_exceeded"}})
            return AIMessage(content="ok")

        strategy = self._strategy(rate_limit_budget=RetryBudget(capacity=4.0))
        before = strategy.budget.tokens
        _drive(strategy, ["primary", "rescue"], invoke, [])
        assert strategy.budget.tokens >= before

    def test_a_spent_transient_budget_still_stops_the_whole_run(self):
        """The storm guard is a statement about the run, not about one model."""

        async def invoke(_model: str, _is_fallback: bool) -> AIMessage:
            raise lx.InternalServerError(message="500", model="m", llm_provider="openai")

        strategy = self._strategy(budget=RetryBudget(capacity=4.0))
        with pytest.raises(LlmResilienceExhausted) as caught:
            _drive(strategy, ["primary", "rescue"], invoke, [])
        assert caught.value.reason == "budget_exhausted"
        assert caught.value.models_tried == ["primary"]


class TestRetryEventErrorText:
    def test_a_timed_out_retry_names_the_exception_type(self):
        """``str(TimeoutError())`` is the empty string, and the timeout verdict is
        the most common one here — so this field used to be blank on exactly the
        retries an operator needs to read back."""
        state = {"first": True}

        async def invoke(_model: str, _is_fallback: bool) -> AIMessage:
            if state["first"]:
                state["first"] = False
                raise asyncio.TimeoutError()
            return AIMessage(content="ok")

        events: list[dict] = []
        strategy = RetryStrategy(
            timeout=5.0,
            primary_retries=2,
            turn_deadline=0,
            backoff_base=0.0,
            backoff_cap=0.0,
            rng=lambda: 0.0,
        )
        _drive(strategy, ["primary"], invoke, events)

        retries = [e for e in events if e["type"] == "llm_retry"]
        assert retries, "a retry event should have been emitted"
        assert retries[0]["payload"]["error"].startswith("TimeoutError")


class TestCallDeadline:
    @staticmethod
    async def _stall(after_chunks: int, chunk_gap: float = 0.02) -> str:
        for _ in range(after_chunks):
            await asyncio.sleep(chunk_gap)
            CallDeadline.note_progress()
        await asyncio.sleep(30)
        return "never"

    def test_a_stream_that_goes_silent_dies_on_the_idle_bound(self):
        async def scenario() -> None:
            deadline = CallDeadline(total=30.0, first_token=0.0, idle=0.15)
            started = asyncio.get_running_loop().time()
            with pytest.raises(TimeoutError) as caught:
                await deadline.run(self._stall(1))
            elapsed = asyncio.get_running_loop().time() - started
            # Names the bound it hit: a bare TimeoutError stringifies to "".
            assert "stream-idle" in str(caught.value)
            # The RAISE alone proves nothing — the total ceiling would have
            # raised too, just 30 seconds later, which is the whole defect. So
            # assert WHEN, not only that.
            assert elapsed < 1.0, f"idle bound did not bind promptly ({elapsed:.2f}s)"

        asyncio.run(scenario())

    def test_a_stream_that_keeps_producing_outlives_the_idle_window(self):
        async def produce() -> str:
            for _ in range(8):
                await asyncio.sleep(0.02)
                CallDeadline.note_progress()
            return "ok"

        async def scenario() -> None:
            deadline = CallDeadline(total=30.0, first_token=0.0, idle=0.15)
            assert await deadline.run(produce()) == "ok"

        asyncio.run(scenario())

    def test_a_silent_call_dies_on_the_first_token_bound(self):
        async def scenario() -> None:
            deadline = CallDeadline(total=30.0, first_token=0.1, idle=0.0)
            with pytest.raises(TimeoutError) as caught:
                await deadline.run(self._stall(0))
            assert "time-to-first-token" in str(caught.value)

        asyncio.run(scenario())

    def test_a_zero_first_token_bound_defers_to_the_total_ceiling(self):
        """The single-bound behaviour, which is the shipped default."""

        async def scenario() -> None:
            deadline = CallDeadline(total=0.12, first_token=0.0, idle=0.0)
            with pytest.raises(TimeoutError) as caught:
                await deadline.run(self._stall(0))
            assert "total" in str(caught.value)

        asyncio.run(scenario())

    def test_the_total_ceiling_still_binds_a_producing_stream(self):
        async def scenario() -> None:
            deadline = CallDeadline(total=0.15, first_token=0.0, idle=10.0)
            with pytest.raises(TimeoutError) as caught:
                await deadline.run(self._stall(2, chunk_gap=0.02))
            assert "total" in str(caught.value)

        asyncio.run(scenario())

    def test_the_inner_call_is_cancelled_when_a_bound_trips(self):
        finished = {"cancelled": False}

        async def victim() -> str:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                finished["cancelled"] = True
                raise
            return "never"

        async def scenario() -> None:
            with pytest.raises(TimeoutError):
                await CallDeadline(total=0.1, first_token=0.0, idle=0.0).run(victim())

        asyncio.run(scenario())
        assert finished["cancelled"] is True

    def test_caller_cancellation_propagates_and_cancels_the_inner_call(self):
        async def scenario() -> None:
            task = asyncio.ensure_future(
                CallDeadline(total=30.0, first_token=0.0, idle=0.0).run(asyncio.sleep(30))
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

    def test_progress_outside_a_call_is_a_no_op(self):
        # A direct client use or a test has no deadline armed; the producer must
        # not have to know that.
        CallDeadline.note_progress()
        assert CallDeadline.active() is None

    def test_the_deadline_is_visible_to_the_awaited_call(self):
        """The contextvar has to be set BEFORE the task is created — a task
        copies the context at creation, so a later set never reaches it."""

        seen: list[bool] = []

        async def probe() -> str:
            seen.append(CallDeadline.active() is not None)
            return "ok"

        async def scenario() -> None:
            assert await CallDeadline(total=5.0).run(probe()) == "ok"

        asyncio.run(scenario())
        assert seen == [True]


class TestStreamProgressIsReportedByTheClient:
    def test_the_litellm_wrapper_pings_the_deadline_per_chunk(self):
        """The wrapper is the one place every streamed chunk physically passes,
        so it is where 'still producing' can honestly be observed."""
        from mewbo_core.llm.llm import _UsageNormalizingLiteLLM

        class _Inner:
            async def acompletion(self, **_kwargs: Any):
                async def _stream():
                    for _ in range(3):
                        yield types.SimpleNamespace(choices=[], usage=None)

                return _stream()

        async def scenario() -> int:
            deadline = CallDeadline(total=5.0)

            async def consume() -> int:
                client = _UsageNormalizingLiteLLM(_Inner())
                stream = await client.acompletion(stream=True)
                return len([chunk async for chunk in stream])

            count = await deadline.run(consume())
            assert deadline.produced is True
            return count

        assert asyncio.run(scenario()) == 3


class TestHttpRetriesAreNotMultiplied:
    def test_max_retries_is_forwarded_into_litellm(self, monkeypatch):
        """``ChatLiteLLM.max_retries`` reaches only the LangChain-level decorator.

        Left unset in ``model_kwargs``, litellm builds the provider client with
        its own default of 2, so the HTTP timeout an operator reads as one idle
        bound is really three attempts' worth before anything surfaces.
        """
        captured: dict[str, Any] = {}

        class DummyChatLiteLLM:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        module = types.ModuleType("langchain_litellm")
        module.ChatLiteLLM = DummyChatLiteLLM  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "langchain_litellm", module)

        build_chat_model(model_name="gpt-4o", openai_api_base=None)
        assert (captured.get("model_kwargs") or {}).get("max_retries") == 0


class TestBudgetLaw:
    @staticmethod
    def _agent(**kwargs: Any) -> AgentConfig:
        return AgentConfig.model_validate(kwargs)

    def test_ladder_budget_counts_every_configured_rung(self):
        agent = self._agent(
            llm_call_timeout=180.0, llm_call_retries=2, retry={"fallback_retries": 1}
        )
        assert agent.ladder_budget_seconds(0) == 360.0
        assert agent.ladder_budget_seconds(1) == 540.0
        assert agent.ladder_budget_seconds(2) == 720.0

    def test_the_deployed_shape_fits_one_fallback_but_not_two(self):
        """The gap the floor check alone cannot see: sized for rung 2, the third
        rung is dead configuration and nothing says so."""
        agent = self._agent(
            llm_call_timeout=180.0,
            llm_call_retries=2,
            retry={"turn_deadline": 600.0, "fallback_retries": 1},
        )
        assert agent.unreachable_rung(1) is None
        assert agent.unreachable_rung(2) is not None

    def test_a_disabled_terminator_bounds_nothing(self):
        agent = self._agent(retry={"turn_deadline": 0})
        assert agent.unreachable_rung(5) is None

    def test_the_floor_breach_is_reported_at_config_load(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core.config"):
            self._agent(
                llm_call_timeout=180.0, llm_call_retries=2, retry={"turn_deadline": 240.0}
            )
        assert "cannot reach a fallback model" in caplog.text

    def test_the_full_ladder_breach_is_reported_at_the_app_level(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core.config"):
            AppConfig.model_validate(
                {
                    "llm": {
                        "fallback": {
                            "enabled": True,
                            "models": ["rescue-a", "rescue-b", "rescue-c"],
                        }
                    },
                    "agent": {
                        "llm_call_timeout": 180.0,
                        "llm_call_retries": 2,
                        "retry": {"turn_deadline": 600.0, "fallback_retries": 1},
                    },
                }
            )
        assert "cannot reach the whole model ladder" in caplog.text

    def test_a_ladder_that_fits_reports_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core.config"):
            AppConfig.model_validate(
                {
                    "llm": {"fallback": {"enabled": True, "models": ["rescue-a", "rescue-b"]}},
                    "agent": {
                        "llm_call_timeout": 60.0,
                        "llm_call_retries": 2,
                        "retry": {"turn_deadline": 600.0, "fallback_retries": 1},
                    },
                }
            )
        assert "turn_deadline" not in caplog.text

    def test_one_misconfiguration_is_never_reported_twice(self, caplog):
        """A budget too small even for rung 2 is the floor case; the app-level
        check stays quiet rather than restating it."""
        with caplog.at_level(logging.WARNING, logger="core.config"):
            AppConfig.model_validate(
                {
                    "llm": {"fallback": {"enabled": True, "models": ["a", "b", "c"]}},
                    "agent": {
                        "llm_call_timeout": 180.0,
                        "llm_call_retries": 2,
                        "retry": {"turn_deadline": 240.0},
                    },
                }
            )
        assert "cannot reach the whole model ladder" not in caplog.text
        assert "cannot reach a fallback model" in caplog.text
