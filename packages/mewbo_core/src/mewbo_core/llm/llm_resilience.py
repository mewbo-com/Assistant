#!/usr/bin/env python3
"""LLM retry / fallback resilience as a small set of atomic objects.

``RetryStrategy`` holds the policy knobs plus the live retry-budget and
circuit-breaker state; its methods describe the behaviour over that state. The
tool-use loop builds one per run (``from_config``) and drives it with injected
I/O (the model call, event emission, reactive compaction), so the state machine
is testable without a live model or the loop. ``DoomLoopGuard`` is the matching
object for no-progress detection.

See the design notes for the failure taxonomy and the rationale behind the
defaults.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time as _time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The retry/fallback defaults are DECLARED in ``contracts/defaults.py``, with
# their calibration notes, rather than here — that is what lets ``config.py``
# read them for its ``agent.retry`` field defaults without importing this
# module. They are re-imported (not re-exported through an ``__all__``, which
# this module does not have) so
# ``from mewbo_core.llm.llm_resilience import DEFAULT_TIMEOUT`` still resolves and
# every consumer keeps reading a retry default from the module that implements
# retries. ``DEFAULT_REQUEST_TIMEOUT`` and ``DEFAULT_LLM_CALL_LIVENESS_S`` are
# read only through this module (by ``llm.py`` and ``tool_use_loop.py``), never
# in its own body — hence the F401 exemption.
from mewbo_core.contracts.defaults import (  # noqa: F401
    DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_CAP,
    DEFAULT_BUDGET_CAPACITY,
    DEFAULT_CB_COOLDOWN,
    DEFAULT_CB_THRESHOLD,
    DEFAULT_DOOM_LOOP_THRESHOLD,
    DEFAULT_FALLBACK_RETRIES,
    DEFAULT_FIRST_TOKEN_TIMEOUT,
    DEFAULT_LLM_CALL_LIVENESS_S,
    DEFAULT_PRIMARY_RETRIES,
    DEFAULT_RATE_LIMIT_BUDGET_CAPACITY,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_RETRY_AFTER_CAP,
    DEFAULT_STREAM_IDLE_TIMEOUT,
    DEFAULT_TIMEOUT,
    DEFAULT_TURN_DEADLINE,
    DEFAULT_WRITE_PROGRESS_EVENT_INTERVAL,
    DEFAULT_WRITE_PROGRESS_MAX_EVENTS,
    DEFAULT_WRITE_PROGRESS_THRESHOLD,
)
from mewbo_core.contracts.run_error import render_exception

if TYPE_CHECKING:
    from langchain_core.messages import AIMessage

    from mewbo_core.contracts.types import LlmFallbackPayload, LlmRetryPayload
    from mewbo_core.tooling.tool_registry import ToolSpec

# These three stay declared here: ``config.py`` does not read them, so they
# never crossed the boundary that moved the block above.
DEFAULT_BUDGET_RETRY_COST = 1.0
DEFAULT_BUDGET_SUCCESS_CREDIT = 0.3
# Cooldown for a model a provider declared exhausted without naming a reset. The
# breaker instance lives and dies with one run, so any value beyond a plausible
# run length benches the model for exactly as long as it matters — without
# pretending to know a window the provider did not state.
DEFAULT_QUOTA_COOLDOWN = 3600.0

# Synchronization / wait primitives are NOT progress-making work. Polling them
# (e.g. ``check_agents`` while spawned children finish one by one) is the
# intended epoll pattern — repeated identical calls there are healthy waiting,
# never a doom loop. They are dropped from the doom signature entirely.
#
# This is only the BUILT-IN seed of the poll class, never the whole of it: any
# tool whose documented contract is "call me again until my run settles"
# belongs here, and a hardcoded id cannot know about one. ``DoomLoopGuard``
# resolves its effective rules from this seed PLUS whatever a tool DECLARES
# (``ToolSpec.poll`` / ``poll_when_args``, or a session tool's ``poll_class`` /
# ``poll_when_args``) and whatever the operator lists in
# ``agent.retry.poll_tools`` — see :meth:`DoomLoopGuard.from_config`. A
# self-polling nested run whose first two probes both answer "processing" is
# honest waiting; halting it there was a 100%-reproducible false positive.
DOOM_LOOP_EXEMPT_TOOLS: frozenset[str] = frozenset({"check_agents"})

# PROSE FALLBACK ARM — consulted only after :class:`ProviderFault` declines.
# Provider/proxy substrings marking a condition hopeless on the *current* model
# but recoverable on a *different* one. Kept because older providers and proxies
# return prose with no structured code at all, so this is still the only signal
# available for them. It is NOT the seam to extend: a provider rewords a message
# whenever it likes and each rewording silently un-matches every substring here
# (a live gateway's "1-week quota has been exhausted" matched none of the six,
# and the run retried a model that could not succeed until the turn deadline).
# Teach ``ProviderFault`` a CODE instead.
_SWITCH_HINTS: tuple[str, ...] = (
    "no deployments available",  # proxy/router exhausted this model
    "out of extra usage",  # provider billing/quota exhaustion
    "insufficient_quota",
    "insufficient credits",
    "exceeded your current quota",
    "billing",
)

# An unknown/retired model id surfaces as a 400 too (e.g. the proxy dropped a
# model a persisted submission still names) — hopeless on THIS model but
# recoverable on a fallback, so it is ``switch_model``, not a fatal malformed
# request. Paired with a ``"model"`` token check to avoid matching unrelated
# 400s ("file does not exist" etc.).
_INVALID_MODEL_HINTS: tuple[str, ...] = (
    "invalid model",
    "model not found",
    "does not exist",
    "no such model",
    "unknown model",
)

# Deterministic local errors never benefit from a retry.
_DETERMINISTIC: tuple[type[BaseException], ...] = (
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    IndexError,
    ImportError,
)

_INTERRUPTED_TOOL_RESULT = "[Tool execution was interrupted]"

# Injected I/O contracts for ``RetryStrategy.run``.
InvokeFn = Callable[[str, bool], Awaitable["AIMessage"]]  # (model, is_fallback) -> response
EmitFn = Callable[[dict[str, Any]], None]
CompactFn = Callable[[], Awaitable[bool]]  # compact in place; True if it happened


class RetryAction(str, Enum):
    """What to do with a failed LLM call."""

    RETRY_SAME = "retry_same"  # transient — back off and retry the same model
    SWITCH_MODEL = "switch_model"  # hopeless here, maybe fine elsewhere
    FATAL = "fatal"  # terminal — never retry, never switch


@dataclass(frozen=True)
class ErrorDecision:
    """Outcome of classifying an LLM exception."""

    action: RetryAction
    error_type: str
    reason: str = ""
    retry_after: float | None = None
    #: Seconds this model should be benched for, when the PROVIDER declared it
    #: unusable rather than merely failing. ``None`` for every inference the
    #: classifier makes on its own — a context-window overflow or a bad key is a
    #: fact about the request, not about the model's health, and benching a
    #: healthy model over one would strand the chain on its fallbacks.
    cooldown: float | None = None

    @property
    def retryable(self) -> bool:
        """True when the same model should be retried (transient failure)."""
        return self.action is RetryAction.RETRY_SAME


class ProviderFault(BaseModel, ABC):
    """A fault a provider declared with a STRUCTURED code, not with prose.

    The parse seam (:meth:`from_exception`) reads the error ENVELOPE — the
    provider's own code, the HTTP status, the ``Retry-After`` and rate-limit
    reset headers — and resolves it to one of the kinds below, each of which
    owns its own :meth:`decide`. Prose is never read: a provider rewrites a
    message freely and cannot rewrite the code it contracts on, which is exactly
    why the ``_SWITCH_HINTS`` substring table below misses a live gateway's
    "1-week quota has been exhausted".

    Members carry NO I/O: the exception arrives as an argument to the parse
    seam, and the current time arrives as an argument to :meth:`decide` /
    :meth:`cooldown_seconds`, so every path is drivable from a test with a fixed
    clock and a hand-built envelope.

    The base is ABSTRACT, and structurally so rather than by convention: it
    declares no ``kind`` and an abstract :meth:`decide`, so every CONSTRUCTIBLE
    value is one of the members of :data:`AnyProviderFault` and a base instance
    cannot leak out of the parse seam into a caller that would then have nothing
    to dispatch on.

    There is deliberately no residual "unknown" member. A code this seam does
    not recognise resolves to ``None``, which means *this classifier declines*
    and hands the exception back to the type ladder — not *this is a fault of
    unknown kind*. The difference is the whole safety argument: an unknown-kind
    member would have to invent a retry action for a code nobody has read, and
    that guess would OVERRIDE the well-evidenced exception-type arms rather than
    defer to them. Returning ``None`` for "no answer" is the same shape
    ``CommandVerification.from_value`` and ``RepositoryRef.coerce`` already use.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Code fragments this kind owns, folded to lowercase alphanumerics so a
    #: provider's punctuation and casing (``Throttling.AllocationQuota``,
    #: ``insufficient_quota``) collapse into one comparison. Declared on the
    #: class that owns the ACTION, so teaching the classifier a new code is one
    #: entry here — never a branch at a call site.
    codes: ClassVar[tuple[str, ...]] = ()

    #: The provider's code verbatim, kept for telemetry and for the operator who
    #: has to recognise it in a gateway log.
    code: str
    http_status: int | None = None
    retry_after: float | None = Field(None, ge=0)
    #: When the exhausted pool refills, as an ABSOLUTE moment. A relative hint
    #: is normalized into ``retry_after`` at parse time instead, so the two
    #: fields never encode the same thing in two units.
    resets_at: datetime | None = None

    @field_validator("resets_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        """Force a naive reset stamp to UTC so arithmetic against ``now`` works."""
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @classmethod
    def owns(cls, folded: str) -> bool:
        """True when *folded* (a code run through :meth:`fold`) is this kind."""
        return any(fragment in folded for fragment in cls.codes)

    @abstractmethod
    def decide(self, error_type: str, now: datetime) -> ErrorDecision:
        """Map this fault onto a retry action.

        Abstract with no default on purpose: an unimplemented kind must fail at
        class definition, never fall through to a neutral action that reads as a
        deliberate verdict.
        """

    def cooldown_seconds(self, now: datetime) -> float | None:
        """Seconds until the declared reset, or ``None`` when none was stated."""
        if self.resets_at is None:
            return None
        return max(0.0, (self.resets_at - now).total_seconds())

    # -- parsing ----------------------------------------------------------
    #: Headers a provider states a rate-limit reset in. Read in order; the first
    #: that parses wins.
    RESET_HEADERS: ClassVar[tuple[str, ...]] = (
        "x-ratelimit-reset",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
        "anthropic-ratelimit-unified-reset",
        "ratelimit-reset",
    )
    #: An exception message can carry an entire upstream error page. Only the
    #: head is scanned for an embedded JSON envelope — the same bounding rule
    #: ``run_error`` applies, for the same reason.
    MESSAGE_SCAN_LIMIT: ClassVar[int] = 4000
    #: Below this, a numeric reset value is a RELATIVE hint in seconds rather
    #: than an absolute epoch stamp; providers send both spellings under the
    #: same header name.
    EPOCH_FLOOR: ClassVar[float] = 1_000_000_000.0

    @staticmethod
    def fold(value: str) -> str:
        """Fold a code to lowercase alphanumerics for comparison."""
        return "".join(ch for ch in value.lower() if ch.isalnum())

    @classmethod
    def from_exception(cls, exc: BaseException) -> AnyProviderFault | None:
        """Resolve *exc* to a structured fault, or ``None`` when it carries none.

        ``None`` is the ordinary answer — most exceptions have no provider code
        at all — and it is what hands the decision back to the exception-type
        ladder and the substring arm, so this seam can only ever make the
        classifier MORE specific, never less.
        """
        envelope = cls._envelope(exc)
        if envelope is None:
            return None
        candidates: list[str] = envelope.pop("candidates")
        for kind in _FAULT_KINDS:
            for candidate in candidates:
                if kind.owns(cls.fold(candidate)):
                    return kind.model_validate({**envelope, "code": candidate})
        return None

    @classmethod
    def _envelope(cls, exc: BaseException) -> dict[str, Any] | None:
        """Pull the code candidates, status and timing hints off *exc*.

        ``None`` means *this exception carries no provider envelope* — the
        single guard below is where that is decided, so every read after it
        works on a proven mapping instead of defending itself. Naming the case
        matters beyond tidiness: an unparseable body is exactly the situation
        that degrades to a bare timeout with the cause discarded, and a silent
        empty dict would make it indistinguishable from an envelope that parsed
        and simply said nothing.
        """
        body = cls._body(exc)
        direct_code = getattr(exc, "code", None)
        if body is None and not isinstance(direct_code, (str, int)):
            return None
        body = body or {}
        raw_error = body.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}

        candidates = [
            str(value)
            for value in (
                error.get("code"),
                error.get("type"),
                body.get("code"),
                body.get("type"),
                direct_code,
            )
            if isinstance(value, (str, int)) and str(value).strip()
        ]
        if not candidates:
            return None

        response = getattr(exc, "response", None)
        headers = cls._mapping(getattr(response, "headers", None))
        retry_after = cls._as_float(headers.get("retry-after")) or cls._as_float(
            error.get("retry_after") or body.get("retry_after")
        )
        absolute, relative = cls._reset_hint(headers, error)
        if relative is not None:
            retry_after = max(retry_after or 0.0, relative)

        return {
            "candidates": candidates,
            "http_status": cls._as_int(
                getattr(exc, "status_code", None) or getattr(response, "status_code", None)
            ),
            "retry_after": retry_after,
            "resets_at": absolute,
        }

    @classmethod
    def _reset_hint(
        cls, headers: dict[str, Any], error: dict[str, Any]
    ) -> tuple[datetime | None, float | None]:
        """First parseable reset hint, as ``(absolute, relative_seconds)``.

        Headers first, then the body — a provider that sends both states the
        same thing twice, and the header is the one it contracts on.
        """
        sources = (
            *(headers.get(name) for name in cls.RESET_HEADERS),
            error.get("resets_at"),
            error.get("reset_at"),
        )
        for raw in sources:
            absolute, relative = cls._as_moment(raw)
            if absolute is not None or relative is not None:
                return absolute, relative
        return None, None

    @classmethod
    def _body(cls, exc: BaseException) -> dict[str, Any] | None:
        """The error body as a mapping, or ``None`` when there is none to read.

        A proxy commonly embeds the upstream JSON envelope inside the exception
        MESSAGE rather than exposing it as a field, so the message is scanned
        for a JSON object. That is still reading structure, not prose: only the
        parsed ``code``/``type`` keys are ever consulted.
        """
        direct = getattr(exc, "body", None)
        if isinstance(direct, dict):
            return direct
        try:
            text = str(getattr(exc, "message", "") or str(exc))[: cls.MESSAGE_SCAN_LIMIT]
        except Exception:  # noqa: BLE001 — defensive: some exc __str__ raise
            return None
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _mapping(headers: Any) -> dict[str, Any]:
        """Lowercase-keyed view of a header mapping, or an empty dict."""
        try:
            return {str(k).lower(): v for k, v in dict(headers).items()}
        except (TypeError, ValueError, AttributeError):
            return {}

    @staticmethod
    def _as_float(raw: Any) -> float | None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _as_int(raw: Any) -> int | None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _as_moment(cls, raw: Any) -> tuple[datetime | None, float | None]:
        """Split a reset hint into ``(absolute, relative_seconds)``.

        The same header carries both spellings across providers — an epoch
        stamp, an ISO timestamp, or a plain number of seconds from now — so the
        magnitude decides which it is rather than a per-provider table.
        """
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return None, None
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")), None
            except ValueError:
                raw = text
        numeric = cls._as_float(raw)
        if numeric is None:
            return None, None
        if numeric >= cls.EPOCH_FLOOR:
            try:
                return datetime.fromtimestamp(numeric, tz=timezone.utc), None
            except (OverflowError, OSError, ValueError):
                return None, None
        return None, numeric


class QuotaFault(ProviderFault):
    """An allocation pool this model draws on is spent.

    Hopeless on THIS model until the pool refills, and a refill is measured in
    hours or days rather than in the seconds a backoff can absorb. So the action
    is to leave the model AND to bench it for the rest of the run: re-probing it
    every turn spends the whole turn deadline on a call that cannot succeed,
    which is precisely the failure this kind exists to end.
    """

    kind: Literal["quota"] = "quota"
    codes: ClassVar[tuple[str, ...]] = (
        "allocationquota",
        "insufficientquota",
        "quotaexceeded",
        "quotaexhausted",
        "outofquota",
        "insufficientcredits",
        "insufficientbalance",
        "billinghardlimitreached",
    )

    def decide(self, error_type: str, now: datetime) -> ErrorDecision:
        """Leave this model and bench it until the declared reset."""
        return ErrorDecision(
            RetryAction.SWITCH_MODEL,
            error_type,
            "quota_exhausted",
            cooldown=self.cooldown_seconds(now) or DEFAULT_QUOTA_COOLDOWN,
        )


class RateLimitFault(ProviderFault):
    """Ordinary throttling — the pool is intact, the window is momentarily full.

    The same exception class and the same HTTP status as :class:`QuotaFault`,
    and the OPPOSITE right action, which is the whole reason this seam reads a
    code instead of a status: a throttle clears on its own, so the model is
    retried, bounded by the small rate-limit budget rather than the transient one.
    """

    kind: Literal["rate_limit"] = "rate_limit"
    codes: ClassVar[tuple[str, ...]] = (
        "ratelimit",
        "toomanyrequests",
        "throttling",
        "slowdown",
    )

    def decide(self, error_type: str, now: datetime) -> ErrorDecision:
        """Retry the same model, honouring whatever wait the provider stated."""
        wait = self.retry_after or self.cooldown_seconds(now)
        return ErrorDecision(RetryAction.RETRY_SAME, error_type, "rate_limit", retry_after=wait)


class UnavailableFault(ProviderFault):
    """The proxy has no healthy route for this model.

    Distinct from a 5xx, which says the route exists and misbehaved: nothing the
    caller waits for changes a routing table mid-run, so this leaves the model
    rather than retrying it.
    """

    kind: Literal["unavailable"] = "unavailable"
    codes: ClassVar[tuple[str, ...]] = (
        "nodeployments",
        "nodeploymentsavailable",
        "nohealthydeployment",
        "modelnotavailable",
        "modelnotfound",
    )

    def decide(self, error_type: str, now: datetime) -> ErrorDecision:
        """Leave this model; bench it so the chain does not circle back."""
        return ErrorDecision(
            RetryAction.SWITCH_MODEL,
            error_type,
            "no_deployments",
            cooldown=self.cooldown_seconds(now) or DEFAULT_CB_COOLDOWN,
        )


#: The parse seam. A stored/wire-shaped fault validates through the discriminator;
#: :meth:`ProviderFault.from_exception` derives the same ``kind`` from a code and
#: constructs the member directly, so there is exactly one place the member set
#: is declared and no ``if kind ==`` anywhere.
AnyProviderFault = Annotated[
    QuotaFault | RateLimitFault | UnavailableFault, Field(discriminator="kind")
]

#: Resolution ORDER, most specific first — this is load-bearing, not cosmetic. A
#: quota code routinely also carries the generic throttling token
#: (``Throttling.AllocationQuota`` folds to a string containing ``throttling``),
#: so a rate-limit-first walk would classify a spent weekly pool as a momentary
#: throttle and retry a model that cannot succeed.
_FAULT_KINDS: tuple[type[QuotaFault] | type[UnavailableFault] | type[RateLimitFault], ...] = (
    QuotaFault,
    UnavailableFault,
    RateLimitFault,
)

#: Reasons drawing on the small rate-limit budget rather than the transient one.
_RATE_LIMIT_REASONS: frozenset[str] = frozenset({"rate_limit"})


@dataclass
class RetryBudget:
    """Token-bucket retry budget — the cross-turn "stop the storm" guard.

    Drains by ``retry_cost`` per retry, refills by ``success_credit`` per
    success. Retries are refused once the bucket drops to half capacity, so a
    sustained outage degrades to immediate clean errors instead of a storm.
    """

    capacity: float = DEFAULT_BUDGET_CAPACITY
    retry_cost: float = DEFAULT_BUDGET_RETRY_COST
    success_credit: float = DEFAULT_BUDGET_SUCCESS_CREDIT
    _tokens: float = field(init=False)

    def __post_init__(self) -> None:
        """Start the bucket full."""
        self._tokens = float(self.capacity)

    @property
    def tokens(self) -> float:
        """Current token balance."""
        return self._tokens

    def can_retry(self) -> bool:
        """True while the bucket is above half capacity."""
        return self._tokens > self.capacity / 2.0

    def charge(self) -> None:
        """Debit one retry from the budget."""
        self._tokens = max(0.0, self._tokens - self.retry_cost)

    def credit(self) -> None:
        """Refill the budget after a successful call (capped at capacity)."""
        self._tokens = min(self.capacity, self._tokens + self.success_credit)


@dataclass
class CircuitBreaker:
    """Per-model consecutive-failure cooldown.

    A ratio-based breaker needs call volume a single agent loop doesn't have,
    so this trips on consecutive failures instead: after ``threshold`` in a row
    a model is skipped for ``cooldown`` seconds, then probed once. ``clock`` is
    injected for testability.
    """

    threshold: int = DEFAULT_CB_THRESHOLD
    cooldown: float = DEFAULT_CB_COOLDOWN
    clock: Callable[[], float] = _time.monotonic
    _fails: dict[str, int] = field(default_factory=dict)
    _open_until: dict[str, float] = field(default_factory=dict)

    def is_open(self, model: str) -> bool:
        """True while the model is cooling down; half-opens once it elapses."""
        until = self._open_until.get(model)
        if until is None:
            return False
        if self.clock() >= until:
            self._open_until.pop(model, None)
            self._fails[model] = 0
            return False
        return True

    def record_failure(self, model: str) -> None:
        """Count a failure; trip the breaker at the consecutive threshold."""
        if self.threshold <= 0:
            return
        self._fails[model] = self._fails.get(model, 0) + 1
        if self._fails[model] >= self.threshold:
            self._open_until[model] = self.clock() + self.cooldown

    def trip(self, model: str, cooldown: float | None = None) -> None:
        """Open the breaker for *model* now, with no failure streak required.

        For the case the streak heuristic cannot express: the PROVIDER stated
        this model is unusable and for how long. A quota that refills in days is
        not "cooling for 30s", so *cooldown* overrides the configured one and a
        value beyond the run's length benches the model for the whole run.

        Unlike :meth:`record_failure` this ignores ``threshold``: that knob
        tunes how many consecutive failures are read as ill health, and a
        provider's own declaration is not an inference to be tuned.
        """
        wait = self.cooldown if cooldown is None else cooldown
        if wait <= 0:
            return
        self._open_until[model] = self.clock() + wait
        self._fails[model] = max(self._fails.get(model, 0), 1)

    def record_success(self, model: str) -> None:
        """Reset the failure streak and clear any cooldown for the model."""
        self._fails.pop(model, None)
        self._open_until.pop(model, None)


_T = TypeVar("_T")


@dataclass
class CallDeadline:
    """Split bound on ONE model call: time-to-first-token, then stream idle.

    A single ``wait_for`` around a streamed call cannot tell slow-but-producing
    from dead — both are simply "still running at N seconds" — so the one knob
    that bounds a wedged provider also bounds a healthy long generation, and it
    has to be set loose enough for the second. This splits the question:

    * before the first chunk, :attr:`first_token` bounds the wait (``0`` defers
      to :attr:`total`);
    * once a chunk has arrived, :attr:`idle` bounds each GAP between chunks
      (``0`` disables it);
    * :attr:`total` remains the absolute ceiling throughout.

    So a stream that returns 200 and then goes quiet dies on the idle bound
    instead of riding the total ceiling, and it dies with a message NAMING the
    bound it hit — ``str(TimeoutError())`` is the empty string, which is what
    made these failures forensically blank.

    The producer reaches the live instance through :meth:`note_progress`, a
    contextvar rather than a parameter: the chunks are yielded deep inside the
    provider client, several libraries below the caller, and a signal cannot be
    threaded down through code this package does not own. The variable is set
    BEFORE the task is created so the task's context copy carries it, and what
    crosses is a reference to this mutable object — the callee never rebinds the
    variable, so nothing has to propagate back up.
    """

    total: float
    first_token: float = DEFAULT_FIRST_TOKEN_TIMEOUT
    idle: float = DEFAULT_STREAM_IDLE_TIMEOUT
    clock: Callable[[], float] = _time.monotonic
    _started: float = field(init=False, default=0.0)
    _last_chunk: float | None = field(init=False, default=None)

    _active: ClassVar[ContextVar[CallDeadline | None]] = ContextVar(
        "mewbo_call_deadline", default=None
    )

    def __post_init__(self) -> None:
        """Start the clock at construction, not at :meth:`run`."""
        self._started = self.clock()

    @classmethod
    def active(cls) -> CallDeadline | None:
        """The deadline bounding the call on this task, if any."""
        return cls._active.get()

    @classmethod
    def note_progress(cls) -> None:
        """Record that the in-flight call produced output. Safe with no call."""
        deadline = cls._active.get()
        if deadline is not None:
            deadline.record_progress()

    def record_progress(self) -> None:
        """Stamp the arrival of a chunk, re-arming the idle window."""
        self._last_chunk = self.clock()

    @property
    def produced(self) -> bool:
        """True once at least one chunk has arrived."""
        return self._last_chunk is not None

    def remaining(self) -> tuple[float, str]:
        """Seconds left on the tightest active bound, and that bound's name."""
        now = self.clock()
        bounds: list[tuple[float, str]] = []
        if self.total > 0:
            bounds.append((self._started + self.total - now, "total"))
        if self._last_chunk is None:
            if self.first_token > 0:
                bounds.append((self._started + self.first_token - now, "time-to-first-token"))
        elif self.idle > 0:
            bounds.append((self._last_chunk + self.idle - now, "stream-idle"))
        return min(bounds, default=(math.inf, "unbounded"))

    async def run(self, awaitable: Awaitable[_T]) -> _T:
        """Await *awaitable* under the split bound; raise ``TimeoutError`` on expiry.

        Cancellation of the caller cancels the inner task and propagates, so this
        stays transparent to the ``TaskGroup`` teardown the loop relies on.
        """
        token = self._active.set(self)
        # Created INSIDE the contextvar's scope: a task copies the context at
        # creation, so a task made before the set would never see the deadline.
        task: asyncio.Task[_T] = asyncio.ensure_future(awaitable)
        try:
            while True:
                left, bound = self.remaining()
                if left <= 0:
                    await self._cancel(task)
                    raise TimeoutError(
                        f"no response from the model within "
                        f"{self._limit(bound):.1f}s ({bound} bound)"
                    )
                done, _ = await asyncio.wait({task}, timeout=self._slice(left))
                if task in done:
                    return task.result()
        except asyncio.CancelledError:
            await self._cancel(task)
            raise
        finally:
            self._active.reset(token)

    def _slice(self, left: float) -> float | None:
        """How long to wait before re-evaluating. ``None`` means "no bound".

        The first chunk SWITCHES which bound governs, from the loose total
        ceiling to the tight idle window, and nothing wakes this loop when it
        arrives. So while no chunk has been seen, never sleep past the earliest
        moment an idle window could expire — otherwise a stream that produced
        once and went silent is only noticed when the total ceiling runs out,
        i.e. exactly the behaviour the split exists to replace.
        """
        if self._last_chunk is None and self.idle > 0:
            left = min(left, self.idle)
        return None if left == math.inf else left

    def _limit(self, bound: str) -> float:
        """The configured seconds behind the named bound, for the error message."""
        return {
            "total": self.total,
            "time-to-first-token": self.first_token,
            "stream-idle": self.idle,
        }.get(bound, 0.0)

    @staticmethod
    async def _cancel(task: asyncio.Task[Any]) -> None:
        """Cancel *task* and absorb its cancellation, never its result."""
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown is not the failure
            pass


class LlmResilienceExhausted(RuntimeError):
    """Raised when the retry + fallback chain is exhausted for one turn.

    Subclasses ``RuntimeError`` so existing ``except Exception`` handlers in
    the orchestrator still map it to ``done_reason="error"``, while carrying
    structured fields for events/telemetry and one-click recovery.
    """

    def __init__(
        self,
        models_tried: Iterable[str],
        last_error: BaseException | None,
        last_error_type: str,
        reason: str = "exhausted",
    ) -> None:
        """Capture the models tried and the final error for telemetry/recovery."""
        self.models_tried: list[str] = list(models_tried)
        self.last_error = last_error
        self.last_error_type = last_error_type
        self.reason = reason
        super().__init__(
            f"LLM call failed on all models ({', '.join(self.models_tried)}): "
            f"{self.describe_error(last_error)}"
        )

    @staticmethod
    def describe_error(exc: BaseException | None) -> str:
        """Render *exc* for the exhaustion message — never as the empty string.

        Several exception classes stringify to ``""`` (``TimeoutError`` is the
        one that bites: the classifier's most common transient verdict).
        Preserving the void verbatim sends a run's terminal failure to the
        store, the console and the next recovery turn as
        ``"LLM call failed on all models (<model>): "`` with the cause erased.
        That string is the durable forensic record after a trace ages out, so
        the cause must survive in it.

        Delegates the ``"<Type>: <msg>"`` rendering to
        :func:`mewbo_core.contracts.run_error.render_exception`, the SAME renderer the
        Langfuse span's ``status_message`` (``components.py:
        _span_status_message``) uses, so the two records of one failure never
        disagree. ``exc is None`` and an exception whose own ``__str__`` raises
        are handled here rather than in the shared renderer — neither is a
        genuine "exception to render": one has nothing to render, the other
        can't even produce ``str(exc)``.
        """
        if exc is None:
            return "Unknown"
        try:
            return render_exception(exc, limit=500)
        except Exception:  # noqa: BLE001 — defensive: some exc __str__ raise
            return type(exc).__name__[:500]


class EmptyModelResponse(RuntimeError):
    """A model call returned 200 carrying neither text content nor a tool call.

    The ladder advances on an EXCEPTION, so a response with nothing in it was
    accepted as the turn's answer: the run completed, the retry budget was
    credited and the breaker was cleared for a model that produced nothing. That
    is not a milder success — it is the same event as a timeout (the model
    emitted nothing) arriving over a connection the provider happened to close,
    so it is raised here and classified as a rung that failed.

    ``RuntimeError`` for the same reason :class:`LlmResilienceExhausted` is: an
    instance escaping the strategy still maps to ``done_reason="error"`` rather
    than to a crash no handler expects.
    """

    def __init__(self, model: str) -> None:
        """Name the model that returned nothing — that is the whole diagnosis."""
        self.model = model
        super().__init__(f"{model} returned no content and no tool calls")


@dataclass
class RetryStrategy:
    """Bounded retry/fallback state machine for one tool-use run.

    Holds the policy knobs and the live ``RetryBudget`` / ``CircuitBreaker``
    state; :meth:`run` drives one logical LLM call across the model chain using
    injected I/O. Terminal error classes are checked first: it retries transient
    failures with full-jitter backoff, switches on hopeless-here errors and
    halts on fatal ones — bounded by a per-call attempt count, a wall-clock
    deadline, the retry budget and the circuit breaker. The caller appends the
    returned message *after* :meth:`run` returns, so a retry never duplicates a
    tool call or bloats context with a partial generation.
    """

    timeout: float = DEFAULT_TIMEOUT
    first_token_timeout: float = DEFAULT_FIRST_TOKEN_TIMEOUT
    stream_idle_timeout: float = DEFAULT_STREAM_IDLE_TIMEOUT
    primary_retries: int = DEFAULT_PRIMARY_RETRIES
    fallback_retries: int = DEFAULT_FALLBACK_RETRIES
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_cap: float = DEFAULT_BACKOFF_CAP
    retry_after_cap: float = DEFAULT_RETRY_AFTER_CAP
    turn_deadline: float = DEFAULT_TURN_DEADLINE
    budget: RetryBudget = field(default_factory=RetryBudget)
    #: Separate, far smaller bucket for throttling. Kept apart from ``budget``
    #: rather than sharing it because the two failure classes have opposite
    #: economics — see ``DEFAULT_RATE_LIMIT_BUDGET_CAPACITY``.
    rate_limit_budget: RetryBudget = field(
        default_factory=lambda: RetryBudget(capacity=DEFAULT_RATE_LIMIT_BUDGET_CAPACITY)
    )
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    clock: Callable[[], float] = _time.monotonic
    #: Wall clock, injected separately from ``clock`` (which is monotonic and is
    #: what the turn deadline measures). A provider states a reset as an absolute
    #: moment, so comparing it needs a real date — and a test needs to pin one.
    #: A ``partial`` rather than a ``lambda``: a plain function assigned as a
    #: dataclass default is a descriptor and would bind ``self`` as its first
    #: argument on every read.
    wall_clock: Callable[[], datetime] = partial(datetime.now, timezone.utc)
    rng: Callable[[], float] = random.random
    # Sticky escalation: once a rescue model wins (the configured primary was
    # not the survivor), it is pinned and tried first on every subsequent turn —
    # so a dead primary is not re-probed each step. ``None`` until an escalation
    # succeeds; the circuit breaker + budget still bound any further switching.
    _pinned_model: str | None = None

    #: Substituted by the driver for an empty assistant turn, because empty
    #: content replayed in history makes extended-thinking models emit
    #: placeholder meta-text. It is declared HERE, next to the predicate that has
    #: to see through it: the sentinel is written in one module and read in
    #: another, and a second spelling of "this response carried nothing" is
    #: exactly how the evidence came to be detected and then discarded.
    NO_CONTENT_PLACEHOLDER: ClassVar[str] = "(no content)"

    @classmethod
    def response_text(cls, content: object) -> str:
        """Plain text carried by an ``AIMessage`` content field, or ``""``.

        Pure: takes the content, reads no state and performs no I/O, so every
        shape a provider produces — ``""``, ``[]``, a list of empty text blocks,
        the sentinel — is drivable directly from a test.
        """
        if isinstance(content, list):
            text = "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        else:
            text = (str(content) if content else "").strip()
        if text == cls.NO_CONTENT_PLACEHOLDER:
            return ""
        return text

    @classmethod
    def generated_nothing(cls, response: AIMessage) -> bool:
        """True when usage proves the model emitted ZERO output tokens.

        Pure, and the ONLY thing that convicts an empty response. Absent or
        unparsable usage returns False, so a stripped usage field can never
        manufacture a failed rung — the asymmetry is deliberate and load-bearing.
        """
        usage = getattr(response, "usage_metadata", None)
        if not isinstance(usage, dict):
            return False
        output = usage.get("output_tokens")
        return isinstance(output, int) and output == 0

    @classmethod
    def response_is_broken(cls, response: AIMessage) -> bool:
        """True when *response* carries no usable result the run can continue on.

        THE result contract. "Carries nothing" is NOT the rule on its own: a
        model with nothing to add after a tool result legitimately ends its turn
        empty, and failing that rung escalates a healthy model over an ordinary
        turn. What separates the two is how much the model actually GENERATED —
        arriving at "nothing to add" still spends output tokens, while a rung
        that produced no work reports a flat zero.

        **The end-of-turn signal is deliberately NOT consulted, and that is not
        an oversight.** The obvious discriminator is a terminal
        ``finish_reason``/``stop_reason``, and it is unusable here: the adapter
        puts the provider's finish reason on the GENERATION, never on the
        message, and ``ainvoke`` returns the message alone — so it is discarded
        before this predicate could ever see it. The streamed path never carries
        one either. A gate on it would therefore be False for every real
        response, silently degrading this contract to a blanket "empty means
        broken" rule and failing exactly the healthy turns described above. Do
        not reintroduce it without first confirming the adapter propagates it;
        a test stub can assert a signal production never sends.

        ``tool_calls`` is read defensively because a provider adapter is free to
        omit the attribute entirely, and a missing attribute means "no tool
        calls", never "assume the response is fine".
        """
        if getattr(response, "tool_calls", None):
            return False
        if cls.response_text(getattr(response, "content", "")):
            return False
        return cls.generated_nothing(response)

    @classmethod
    def from_config(cls) -> RetryStrategy:
        """Build a strategy from ``agent.retry`` config (hot-read per run)."""
        from mewbo_core.config import get_config_value as g

        cb_threshold = int(
            g("agent", "retry", "circuit_breaker_threshold", default=DEFAULT_CB_THRESHOLD)
        )
        cb_cooldown = float(
            g("agent", "retry", "circuit_breaker_cooldown", default=DEFAULT_CB_COOLDOWN)
        )
        budget_capacity = float(
            g("agent", "retry", "budget_capacity", default=DEFAULT_BUDGET_CAPACITY)
        )
        rate_limit_capacity = float(
            g(
                "agent",
                "retry",
                "rate_limit_budget_capacity",
                default=DEFAULT_RATE_LIMIT_BUDGET_CAPACITY,
            )
        )
        return cls(
            timeout=float(g("agent", "llm_call_timeout", default=DEFAULT_TIMEOUT)),
            first_token_timeout=float(
                g("agent", "llm_first_token_timeout", default=DEFAULT_FIRST_TOKEN_TIMEOUT)
            ),
            stream_idle_timeout=float(
                g("agent", "llm_stream_idle_timeout", default=DEFAULT_STREAM_IDLE_TIMEOUT)
            ),
            primary_retries=int(g("agent", "llm_call_retries", default=DEFAULT_PRIMARY_RETRIES)),
            fallback_retries=int(
                g("agent", "retry", "fallback_retries", default=DEFAULT_FALLBACK_RETRIES)
            ),
            backoff_base=float(g("agent", "retry", "backoff_base", default=DEFAULT_BACKOFF_BASE)),
            backoff_cap=float(g("agent", "retry", "backoff_cap", default=DEFAULT_BACKOFF_CAP)),
            retry_after_cap=float(
                g("agent", "retry", "retry_after_cap", default=DEFAULT_RETRY_AFTER_CAP)
            ),
            turn_deadline=float(
                g("agent", "retry", "turn_deadline", default=DEFAULT_TURN_DEADLINE)
            ),
            budget=RetryBudget(capacity=budget_capacity),
            rate_limit_budget=RetryBudget(capacity=rate_limit_capacity),
            breaker=CircuitBreaker(threshold=cb_threshold, cooldown=cb_cooldown),
        )

    @staticmethod
    def _retry_after(exc: BaseException) -> float | None:
        """Read ``Retry-After`` (seconds) from an error's httpx response."""
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        try:
            val = headers.get("retry-after")
        except AttributeError:
            return None
        try:
            return float(val) if val else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def classify(exc: BaseException, *, now: datetime | None = None) -> ErrorDecision:
        """Classify an LLM exception into the retry / switch / fatal model.

        ORDER IS THE CONTRACT. Cancellation is checked first and never retried
        (it bubbles past this layer). Everything after it is ranked by how
        SPECIFIC the evidence is, most specific first:

        1. a structured :class:`ProviderFault` — a code the provider contracts
           on, which is the only signal that separates a spent quota from a
           momentary throttle when both arrive as the same 429 exception class;
        2. the exception TYPE, including the timeout arms;
        3. the ``_SWITCH_HINTS`` prose substrings.

        The timeout arm sat above (1) and (3) alike, which made the ordering a
        latent trap rather than merely a preference: any error carrying a
        provider code but stringifying through a timeout-flavoured type was
        decided before the code was ever read. Context-window overflow stays
        ``switch_model`` so the caller can compact-then-switch.

        *now* is the wall clock for resolving a declared reset; it defaults for
        the many callers that only want the action, and :meth:`run` injects the
        strategy's own.
        """
        name = type(exc).__name__
        if isinstance(exc, asyncio.CancelledError):
            return ErrorDecision(RetryAction.FATAL, name, "cancelled")

        if isinstance(exc, EmptyModelResponse):
            # RETRY_SAME, not SWITCH_MODEL. An empty result is overwhelmingly a
            # transient one — a stream that died, a generation that produced
            # nothing once — and the same model usually answers on the next
            # attempt, so leaving it immediately benches a healthy model over one
            # blip. The ladder still escalates: once the per-model attempts are
            # spent the chain advances on ``retries_exhausted`` exactly as it
            # does for a timeout, and an all-empty chain exhausts and raises
            # rather than spinning.
            return ErrorDecision(RetryAction.RETRY_SAME, name, "empty_response")

        fault = ProviderFault.from_exception(exc)
        if fault is not None:
            return fault.decide(name, now or datetime.now(timezone.utc))

        if isinstance(exc, asyncio.TimeoutError):
            return ErrorDecision(RetryAction.RETRY_SAME, "TimeoutError", "timeout")

        try:
            import litellm.exceptions as lx
        except ImportError:
            if isinstance(exc, _DETERMINISTIC):
                return ErrorDecision(RetryAction.FATAL, name, "deterministic")
            return ErrorDecision(RetryAction.RETRY_SAME, name, "unknown")

        try:
            msg = str(getattr(exc, "message", "") or str(exc)).lower()
        except Exception:  # noqa: BLE001 — defensive: some exc __str__ raise
            msg = name.lower()

        def _is(attr: str) -> bool:
            cls = getattr(lx, attr, None)
            return cls is not None and isinstance(exc, cls)

        if _is("ContextWindowExceededError"):
            return ErrorDecision(RetryAction.SWITCH_MODEL, name, "context_window")
        if _is("AuthenticationError"):
            # An expired token may refresh on a different deployment; a bad key
            # won't — but switching is the safe, non-looping action either way.
            return ErrorDecision(RetryAction.SWITCH_MODEL, name, "auth")
        if _is("PermissionDeniedError"):
            return ErrorDecision(RetryAction.FATAL, name, "permission_denied")
        if _is("RateLimitError"):
            if any(h in msg for h in _SWITCH_HINTS):
                return ErrorDecision(RetryAction.SWITCH_MODEL, name, "no_deployments")
            ra = RetryStrategy._retry_after(exc)
            return ErrorDecision(RetryAction.RETRY_SAME, name, "rate_limit", retry_after=ra)
        if _is("ContentPolicyViolationError"):
            return ErrorDecision(RetryAction.FATAL, name, "content_policy")
        if _is("BadRequestError"):
            # Quota/billing exhaustion is often surfaced as a 400 invalid request
            # — hopeless on this provider, switchable.
            if any(h in msg for h in _SWITCH_HINTS):
                return ErrorDecision(RetryAction.SWITCH_MODEL, name, "quota_exhausted")
            # An unknown/retired model id is a 400 too — switch to a fallback
            # rather than dying fatally on this one.
            if "model" in msg and any(h in msg for h in _INVALID_MODEL_HINTS):
                return ErrorDecision(RetryAction.SWITCH_MODEL, name, "invalid_model")
            return ErrorDecision(RetryAction.FATAL, name, "bad_request")
        if _is("Timeout"):
            return ErrorDecision(RetryAction.RETRY_SAME, "Timeout", "timeout")
        if _is("InternalServerError") or _is("ServiceUnavailableError"):
            return ErrorDecision(RetryAction.RETRY_SAME, name, "server_error")
        if _is("BadGatewayError"):
            return ErrorDecision(RetryAction.RETRY_SAME, name, "bad_gateway")
        if _is("APIConnectionError"):
            return ErrorDecision(RetryAction.RETRY_SAME, name, "connection")

        if isinstance(exc, _DETERMINISTIC):
            return ErrorDecision(RetryAction.FATAL, name, "deterministic")
        return ErrorDecision(RetryAction.RETRY_SAME, name, "unknown")

    def backoff(self, attempt: int, retry_after: float | None = None) -> float:
        """Full-jitter exponential backoff: ``random(0, min(cap, base*2^(n-1)))``.

        A server ``Retry-After`` is honored as a *floor* (capped) — the server
        knows its recovery window better than the formula does.
        """
        ceiling = min(self.backoff_cap, self.backoff_base * (2 ** (max(1, attempt) - 1)))
        delay = self.rng() * max(0.0, ceiling)
        if retry_after is not None and retry_after > 0:
            delay = max(delay, min(retry_after, self.retry_after_cap))
        return delay

    def _deadline_reached(self, started: float) -> bool:
        return self.turn_deadline > 0 and (self.clock() - started) > self.turn_deadline

    async def run(
        self,
        *,
        models: Sequence[str],
        invoke: InvokeFn,
        emit: EmitFn,
        compact: CompactFn,
        agent_id: str,
        depth: int,
        step: int,
    ) -> tuple[AIMessage, str]:
        """Obtain one successful response across ``models``; raise on exhaustion.

        Returns ``(response, model_name)``. Raises :class:`LlmResilienceExhausted`
        when every model/attempt is spent. Cancellation bubbles up untouched.
        """
        started = self.clock()
        last_exc: BaseException | None = None
        last_reason = "exhausted"
        tried: list[str] = []
        compacted = False

        # The configured primary is always ``models[0]``; capture it BEFORE any
        # reorder so a successful rescue model can be recognised as != primary
        # (and then pinned). The pin reorders the chain so a dead primary is not
        # re-probed every turn — the breaker + budget still bound the switching.
        primary = models[0] if models else ""
        ordered = self._order_models(models)

        # Why the chain advanced to the model now being tried. Distinct from
        # ``last_reason`` (the final telemetry reason): a transient error that
        # exhausts the per-model cap advances with ``retries_exhausted`` even
        # though the underlying ``last_reason`` is e.g. ``timeout``.
        advance_reason = last_reason

        for idx, model_name in enumerate(ordered):
            is_fallback = idx > 0
            # Skip a cooling model when an alternative exists, but never skip the
            # sole primary — there would be nothing left to fall back to.
            if self.breaker.is_open(model_name) and (is_fallback or len(ordered) > 1):
                continue
            # A rung whose first attempt cannot even start was never tried, and
            # saying otherwise is not cosmetic: the exhaustion error names every
            # model in ``tried``, so checking the deadline only INSIDE the attempt
            # loop published an ``llm_fallback`` to — and blamed — a model this
            # run never called. Operators read that as a provider-wide outage
            # instead of a spent wall clock. The in-loop check below stays; it
            # terminates a rung mid-retry, which is a different question.
            if self._deadline_reached(started):
                last_reason = advance_reason = "deadline"
                last_exc = last_exc or TimeoutError("turn deadline exceeded")
                break
            if is_fallback:
                # Escalation is always sticky: the destination model is pinned
                # for the rest of the run on success (see the success branch).
                payload: LlmFallbackPayload = {
                    "agent_id": agent_id,
                    "depth": depth,
                    "step": step,
                    "from_model": tried[-1] if tried else model_name,
                    "to_model": model_name,
                    "reason": advance_reason,
                    "previous_error_type": (type(last_exc).__name__ if last_exc else "Unknown"),
                    "sticky": True,
                }
                emit({"type": "llm_fallback", "payload": payload})
            tried.append(model_name)
            attempts = self.primary_retries if not is_fallback else self.fallback_retries

            attempt = 0
            stop_chain = False
            while attempt < attempts:
                if self._deadline_reached(started):
                    last_reason = advance_reason = "deadline"
                    last_exc = last_exc or TimeoutError("turn deadline exceeded")
                    stop_chain = True
                    break
                attempt += 1
                try:
                    response = await CallDeadline(
                        total=self.timeout,
                        first_token=self.first_token_timeout,
                        idle=self.stream_idle_timeout,
                    ).run(invoke(model_name, is_fallback))
                    if self.response_is_broken(response):
                        # THE one result-contract seam: every path that produces
                        # a message — streamed or buffered — arrives here exactly
                        # once, so the rule is stated once. Raised INTO this
                        # method's own handler rather than branched on below, so
                        # an empty rung is charged, benched and retried by the
                        # one path every other failure takes. Reaching the
                        # success branch instead is what credited the budget and
                        # cleared the breaker for a model that returned nothing,
                        # making it look progressively healthier.
                        raise EmptyModelResponse(model_name)
                except asyncio.CancelledError:
                    raise  # cancellation is terminal — must bubble past retry
                except Exception as exc:  # noqa: BLE001 — provider errors are opaque
                    last_exc = exc
                    decision = self.classify(exc, now=self.wall_clock())
                    last_reason = decision.reason
                    self.breaker.record_failure(model_name)
                    # Context overflow: compact once, then retry the same model.
                    if decision.reason == "context_window" and not compacted and await compact():
                        compacted = True
                        attempt -= 1  # the compaction retry is "free"
                        continue
                    if decision.action is RetryAction.FATAL:
                        stop_chain = True
                        break
                    if decision.action is RetryAction.SWITCH_MODEL:
                        # A provider-declared cooldown (and ONLY that) benches
                        # the model, so a later turn does not re-probe a pool the
                        # provider said is empty for hours. An inferred switch —
                        # context overflow, a bad key — carries no cooldown,
                        # because neither is a fact about the model's health.
                        if decision.cooldown:
                            self.breaker.trip(model_name, decision.cooldown)
                        advance_reason = decision.reason  # hopeless-here → carry the cause
                        break  # advance to the next model in the chain
                    rate_limited = decision.reason in _RATE_LIMIT_REASONS
                    budget = self.rate_limit_budget if rate_limited else self.budget
                    if not budget.can_retry():
                        # A spent THROTTLING budget is not a spent run: the next
                        # model draws on a different pool, so the chain advances.
                        # A spent transient budget means the storm guard fired,
                        # which is a statement about the whole run, and stops it.
                        last_reason = advance_reason = (
                            "rate_limit_budget_exhausted" if rate_limited else "budget_exhausted"
                        )
                        stop_chain = not rate_limited
                        break
                    if attempt >= attempts:
                        # Per-model retry cap tripped on a transient error: the
                        # advance is "we gave up retrying", not a hopeless class.
                        # Charge the budget for this final failed call too — else
                        # exhausting a model is "free" and a chain of single-try
                        # fallbacks never depletes the cross-turn storm guard.
                        budget.charge()
                        advance_reason = "retries_exhausted"
                        break  # primary exhausted -> next model (if any)
                    budget.charge()
                    delay = self.backoff(attempt, decision.retry_after)
                    retry_payload: LlmRetryPayload = {
                        "agent_id": agent_id,
                        "depth": depth,
                        "step": step,
                        "model": model_name,
                        "attempt": attempt,
                        "max_attempts": attempts,
                        # NOT ``str(exc)`` — several exception classes stringify
                        # to "", and ``TimeoutError`` is both the most common
                        # verdict here and one of them, so this field was empty
                        # on exactly the retries an operator needs to read back.
                        # ``describe_error`` substitutes the type name.
                        "error": LlmResilienceExhausted.describe_error(exc)[:200],
                        "error_type": decision.error_type,
                        "delay": delay,
                        "retryable": True,
                    }
                    emit({"type": "llm_retry", "payload": retry_payload})
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    self.breaker.record_success(model_name)
                    self.budget.credit()
                    self.rate_limit_budget.credit()
                    # Sticky escalation: if a rescue model (not the configured
                    # primary) won, pin it so the next turn tries it first.
                    if model_name != primary:
                        self._pinned_model = model_name
                    return response, model_name

            if stop_chain:
                break

        raise LlmResilienceExhausted(
            tried, last_exc, type(last_exc).__name__ if last_exc else "Unknown", reason=last_reason
        )

    def _order_models(self, models: Sequence[str]) -> list[str]:
        """Return the model chain with a pinned rescue model tried first.

        Encapsulates the sticky-escalation reorder so the driver keeps passing
        ``[primary, *fallback_models]`` unchanged each turn. When nothing is
        pinned (or the pin is no longer in the chain), order is unchanged.
        """
        pinned = self._pinned_model
        if pinned and pinned in models:
            return [pinned, *(m for m in models if m != pinned)]
        return list(models)


@dataclass(frozen=True)
class PollClassRule:
    """Declares when a call to one tool is POLLING rather than progress.

    A name-only exemption set is what produced the original false positive, and
    a name-only set cannot express the shape that actually occurs: the tool that
    self-polls is usually the SAME tool that starts the work, distinguishable
    only by argument. ``agentic_search`` is the live case — one tool id whose
    args require exactly one of ``query`` (start the run, returns immediately)
    or ``run_id`` (fetch the run's state). Starting is progress; fetching is
    waiting. Exempting the id outright would blind the guard to a genuinely
    stuck agent re-issuing the same search forever.

    ``when_args`` empty means unconditional (``check_agents``, a pure wait
    primitive). Otherwise the call is a poll only when it carries one of the
    named arguments. Arguments arrive as a METHOD ARG — this model reads no
    state of its own, which is what lets a test drive every case directly.
    """

    tool_id: str
    when_args: frozenset[str] = frozenset()

    def matches(self, tool_name: str, args: Any = None) -> bool:
        """True when *tool_name* called with *args* is a poll under this rule."""
        if tool_name != self.tool_id:
            return False
        if not self.when_args:
            return True
        if not isinstance(args, dict):
            return False
        # A key present but empty (``run_id=None`` / ``""``) is not a poll: the
        # tool schema treats it as absent, so the guard must too.
        return any(args.get(key) not in (None, "", [], {}) for key in self.when_args)


# The built-in rules: a pure wait primitive, exempt on every call.
_DEFAULT_POLL_RULES: tuple[PollClassRule, ...] = tuple(
    PollClassRule(tool_id) for tool_id in sorted(DOOM_LOOP_EXEMPT_TOOLS)
)


def _call_name_and_args(call: Any) -> tuple[str, Any]:
    """Read ``(name, args)`` off a tool call in either dict or attribute form."""
    if isinstance(call, dict):
        return call.get("name", ""), call.get("args", {})
    return getattr(call, "name", ""), getattr(call, "args", {})


@dataclass
class DoomLoopGuard:
    """Detects a model genuinely stuck — same action, same OUTCOME, no progress.

    A doom loop is *not* merely "the same tool input repeated": a model that
    re-issues an identical call whose **result advances** (e.g. polling a wait
    primitive whose state moves on, or accumulating into a growing structure)
    is making progress and must not be halted. The guard therefore tracks both
    the per-turn input signature (:meth:`observe`, pre-execution) and the result
    signature (:meth:`record_result`, post-execution); :meth:`is_stuck` halts
    only when the last ``threshold`` turns share an identical input **and** an
    identical result. ``threshold <= 0`` disables detection.

    POLL-CLASS calls are dropped from both signatures — polling while the thing
    waited on runs is intended epoll, never a loop. The class is RESOLVED from
    declarations, not hardcoded (see :attr:`poll_rules` / :meth:`from_config`),
    and it is per-CALL rather than per-tool: ``check_agents`` is merely the
    built-in seed, and the tool that matters is exempt only on the argument
    shape that means "fetch state", never on the one that means "start work".
    """

    threshold: int = DEFAULT_DOOM_LOOP_THRESHOLD
    # Effective poll rules for THIS run. Defaults to the built-in seed so a
    # directly-constructed guard behaves exactly as before; the loop builds it
    # from the seed ∪ tool-declared ∪ operator-declared rules.
    poll_rules: tuple[PollClassRule, ...] = _DEFAULT_POLL_RULES
    _signatures: list[str] = field(default_factory=list)
    _results: list[str] = field(default_factory=list)
    # Tool ids whose CALL was a poll in the just-observed turn. Carried across
    # the observe → record_result pair because a result carries no arguments:
    # without it an argument-sensitive exemption could drop a call from the
    # input signature but keep its result, leaving the two out of step.
    _last_poll_ids: frozenset[str] = frozenset()

    @classmethod
    def from_config(
        cls, *, extra_poll_rules: Iterable[PollClassRule] = ()
    ) -> DoomLoopGuard:
        """Build a guard from ``agent.retry.doom_loop_threshold``.

        *extra_poll_rules* carries what the CALLER resolved for this run from
        tool declarations. Combined with the built-in seed and the operator's
        ``agent.retry.poll_tools`` list (names, so unconditional), which means a
        tool whose contract is "call me again until my run settles" is
        declarable at whichever seam owns it — and adding the next such tool
        never requires editing this module.
        """
        from mewbo_core.config import get_config_value

        declared = get_config_value("agent", "retry", "poll_tools", default=[])
        if isinstance(declared, str):
            declared = [part.strip() for part in declared.split(",") if part.strip()]
        rules: dict[tuple[str, frozenset[str]], PollClassRule] = {}
        for rule in (
            *_DEFAULT_POLL_RULES,
            *(PollClassRule(str(name)) for name in (declared or [])),
            *extra_poll_rules,
        ):
            rules[(rule.tool_id, rule.when_args)] = rule
        return cls(
            threshold=int(
                get_config_value(
                    "agent", "retry", "doom_loop_threshold", default=DEFAULT_DOOM_LOOP_THRESHOLD
                )
            ),
            poll_rules=tuple(rules.values()),
        )

    @staticmethod
    def is_poll_call(
        tool_name: str,
        args: Any = None,
        poll_rules: tuple[PollClassRule, ...] = _DEFAULT_POLL_RULES,
    ) -> bool:
        """True when this specific CALL counts as polling under *poll_rules*."""
        return any(rule.matches(tool_name, args) for rule in poll_rules)

    @staticmethod
    def poll_call_ids(
        tool_calls: Sequence[Any],
        poll_rules: tuple[PollClassRule, ...] = _DEFAULT_POLL_RULES,
    ) -> frozenset[str]:
        """Tool ids in this batch whose call was a poll (argument-sensitive)."""
        return frozenset(
            name
            for name, args in (_call_name_and_args(tc) for tc in tool_calls or [])
            if DoomLoopGuard.is_poll_call(name, args, poll_rules)
        )

    @staticmethod
    def signature(
        tool_calls: Sequence[Any],
        poll_rules: tuple[PollClassRule, ...] = _DEFAULT_POLL_RULES,
    ) -> str:
        """Stable signature of a tool-call batch (name + sorted args, no id).

        Poll-class CALLS are excluded, so a batch consisting only of them
        yields ``""`` (never counted toward a stuck streak).
        """
        parts: list[str] = []
        for tc in tool_calls or []:
            tc_name, tc_args = _call_name_and_args(tc)
            if DoomLoopGuard.is_poll_call(tc_name, tc_args, poll_rules):
                continue
            try:
                rendered = json.dumps(tc_args, sort_keys=True, default=str)
            except (TypeError, ValueError):
                rendered = repr(tc_args)
            parts.append(f"{tc_name}:{rendered}")
        return "|".join(parts)

    @staticmethod
    def result_signature(
        results: Sequence[Any],
        exclude_ids: frozenset[str] = DOOM_LOOP_EXEMPT_TOOLS,
    ) -> str:
        """Stable signature of a tool-result batch (success + content).

        Drops results for *exclude_ids* so it stays aligned with
        :meth:`signature`. A result carries no arguments, so the caller supplies
        the ids that were polls THIS turn (:meth:`poll_call_ids`) rather than
        this method re-deriving them — an argument-sensitive rule cannot be
        evaluated against a result alone. A *changing* signature across turns
        means the world advanced — i.e. the model is making progress even if it
        called the same tool.
        """
        parts: list[str] = []
        for r in results or []:
            tool_id = getattr(r, "tool_id", "") or ""
            if tool_id in exclude_ids:
                continue
            success = getattr(r, "success", True)
            content = getattr(r, "content", "") or ""
            parts.append(f"{int(bool(success))}:{content}")
        return "|".join(parts)

    def observe(self, tool_calls: Sequence[Any]) -> None:
        """Record this turn's tool-call batch (input signature, pre-execution)."""
        self._last_poll_ids = self.poll_call_ids(tool_calls, self.poll_rules)
        self._signatures.append(self.signature(tool_calls, self.poll_rules))

    def record_result(self, results: Sequence[Any]) -> None:
        """Record this turn's tool results (post-execution) for progress checks."""
        self._results.append(self.result_signature(results, self._last_poll_ids))

    def is_stuck(self) -> bool:
        """True only on genuine no-progress: identical input AND identical result.

        The just-observed turn has no result yet, so progress is judged from the
        prior identical-input turns' results. When no results have been recorded
        at all (a pure-input caller), this falls back to input-identity.
        """
        if self.threshold <= 0 or len(self._signatures) < self.threshold:
            return False
        tail = self._signatures[-self.threshold :]
        if not all(s == tail[0] and s != "" for s in tail):
            return False
        # Inputs are identical. Only a doom loop if the world also stood still.
        if not self._results:
            return True  # pure-input caller — input identity is all we have
        needed = self.threshold - 1
        if needed <= 0:
            return True
        res_tail = self._results[-needed:]
        if len(res_tail) < needed:
            return False  # not enough executed history yet — keep going
        return all(r == res_tail[0] for r in res_tail)


@dataclass
class WriteProgressSignal:
    """Counts consecutive steps without a write-tier tool execution.

    Distinct from :class:`DoomLoopGuard` (identical input *and* identical
    result = stuck): this tracks privilege TIER, not repetition — a long
    streak of read/execute/search/unknown-tier steps with no WRITE at all,
    even when every one of them is genuinely new work. Armed only when the
    agent could plausibly write at all (the two-gate check computed once at
    loop construction — see ``tool_use_loop.py``); when not armed every
    method here is inert, so a read-only or non-write session pays nothing.

    Observe-only: crossing the threshold emits telemetry (a
    ``write_progress_signal`` event) unconditionally. ``reminder_enabled``
    additionally injects a criterion-blind objective-restatement message — it
    never names this signal or its criteria, so it cannot teach an agent the
    tell it is being measured against.

    ``threshold <= 0`` disables detection entirely.
    """

    write_capable: bool = False
    threshold: int = DEFAULT_WRITE_PROGRESS_THRESHOLD
    event_interval: int = DEFAULT_WRITE_PROGRESS_EVENT_INTERVAL
    max_events: int = DEFAULT_WRITE_PROGRESS_MAX_EVENTS
    reminder_enabled: bool = False
    _nonprogress: int = 0
    _fires: int = 0

    @property
    def steps_since_write(self) -> int:
        """Consecutive steps observed since the last write-tier execution."""
        return self._nonprogress

    @classmethod
    def from_config(cls, *, write_capable: bool = False) -> WriteProgressSignal:
        """Build a signal from ``agent.write_progress_signal_*`` config.

        Gated by *write_capable*.
        """
        from mewbo_core.config import get_config_value

        return cls(
            write_capable=write_capable,
            threshold=int(
                get_config_value(
                    "agent",
                    "write_progress_signal_step_threshold",
                    default=DEFAULT_WRITE_PROGRESS_THRESHOLD,
                )
            ),
            event_interval=int(
                get_config_value(
                    "agent",
                    "write_progress_signal_event_interval",
                    default=DEFAULT_WRITE_PROGRESS_EVENT_INTERVAL,
                )
            ),
            max_events=int(
                get_config_value(
                    "agent",
                    "write_progress_signal_max_events",
                    default=DEFAULT_WRITE_PROGRESS_MAX_EVENTS,
                )
            ),
            reminder_enabled=bool(
                get_config_value(
                    "agent", "write_progress_signal_reminder_enabled", default=False
                )
            ),
        )

    def observe(self, results: Sequence[Any], specs_map: dict[str, ToolSpec]) -> None:
        """Record one step's progress verdict from its executed tool results.

        A step is progress iff any executed tool's spec resolves to the
        ``"write"`` capability tier — that resets the non-progress streak to
        zero. Everything else (a declared read/execute/search tier, AND a
        tool_id absent from ``specs_map`` entirely — session tools,
        spawn_agent, activate_skill and tool_search are never registry specs)
        falls through to the same non-progress branch: a tool this signal
        can't look up can't be PROVEN a write, so it is never credited as one.
        """
        is_write = any(
            (spec := specs_map.get(getattr(r, "tool_id", ""))) is not None
            and spec.capability_tier() == "write"
            for r in results
        )
        if is_write:
            self._nonprogress = 0
        else:
            self._nonprogress += 1

    def threshold_crossed(self) -> bool:
        """True (and consumes one event slot) exactly when a fresh event is due.

        Only for a write-capable agent with detection enabled and events
        remaining; fires once the non-progress streak crosses ``threshold``
        and again every ``event_interval`` steps after, so telemetry repeats
        periodically instead of exactly once.
        """
        if not self.write_capable or self.threshold <= 0 or self._fires >= self.max_events:
            return False
        if self._nonprogress < self.threshold:
            return False
        if (self._nonprogress - self.threshold) % self.event_interval != 0:
            return False
        self._fires += 1
        return True

    def exhausted(self) -> bool:
        """True once every allotted event has been spent — go quiet from here."""
        return self._fires >= self.max_events


def repair_tool_pairing(messages: list[Any]) -> int:
    """Rebalance tool_use / tool_result pairs in place; return repairs made.

    The model API rejects a dangling ``tool_use`` or an orphan ``tool_result``.
    A compaction slice can orphan a pair, so this drops orphan results and
    synthesizes an interrupted result for any unanswered call.
    """
    from langchain_core.messages import AIMessage, ToolMessage

    declared: set[str] = set()
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tcid:
                    declared.add(tcid)

    repaired = 0
    answered: set[str] = set()
    kept: list[Any] = []
    for m in messages:
        if isinstance(m, ToolMessage):
            if m.tool_call_id not in declared:
                repaired += 1  # orphan tool_result — drop it
                continue
            answered.add(m.tool_call_id)
        kept.append(m)

    rebuilt: list[Any] = []
    for m in kept:
        rebuilt.append(m)
        if isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tcid and tcid not in answered:
                    rebuilt.append(
                        ToolMessage(content=_INTERRUPTED_TOOL_RESULT, tool_call_id=tcid)
                    )
                    answered.add(tcid)
                    repaired += 1

    if repaired:
        messages[:] = rebuilt
    return repaired
