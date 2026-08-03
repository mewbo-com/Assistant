#!/usr/bin/env python3
"""Sub-agent spawning tool for the agent hypervisor.

``SpawnAgentTool`` creates a child ``ToolUseLoop`` instance, registers it
in the ``AgentHypervisor``, runs it to completion, and returns the result.
Tool scoping follows the "filter before binding" pattern: denied
tools are removed from the child's ``bind_tools()`` list so the child LLM
never sees them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any, ClassVar, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_core.agents.agent_context import AgentContext, AgentDepthExceeded
from mewbo_core.agents.hypervisor import (
    ACTIVE_STATUSES,
    AgentHandle,
    AgentResult,
    AgentStatus,
    AutonomyTier,
    DelegationContract,
    ModelTier,
    ScheduledSpawn,
    SpawnRefusalCode,
    SpawnUnit,
    SummaryKind,
)
from mewbo_core.classes import ActionStep, OrchestrationState, TaskQueue
from mewbo_core.common import MockSpeaker, discover_project_instructions, get_logger
from mewbo_core.components import LangfuseTraceLink, langfuse_child_task_link
from mewbo_core.config import get_config_value
from mewbo_core.contracts.run_error import RunError
from mewbo_core.contracts.types import Event
from mewbo_core.contracts.verification import CommandVerification
from mewbo_core.hooks import HookManager
from mewbo_core.permissions import PermissionPolicy
from mewbo_core.safety.plane import SafetyPlane
from mewbo_core.tooling.session_tools import SessionToolRegistry
from mewbo_core.tooling.tool_registry import CapabilityMode, ToolRegistry, ToolSpec, filter_specs
from mewbo_core.workspaces.project_catalog import (
    MANAGED_PREFIX,
    ProjectCatalog,
    ProjectResolutionError,
)
from mewbo_core.workspaces.workspace import WorkspaceMode

logging = get_logger(name="core.spawn_agent")

# Delegation approval policy. PARSED + carried on the spawn
# schema in v1 but ENFORCED by nothing yet — the approval-gate wiring lands in a
# later wave. Declared here as the vocabulary the schema pins via ``get_args``.
ApprovalPolicy = Literal["never", "on_failure", "on_request"]

# Cap the compressed child result echoed onto the ``stop`` lifecycle event so a
# verbose sub-agent answer can't bloat the parent transcript / run event log.
# Mirrors the ``AgentResult.summary`` cap (here a touch larger so a probe's
# whole evidence block survives for the trace's response panel).
_SUB_AGENT_SUMMARY_CAP = 1500

@dataclass
class AgentError:
    """Structured error context from a failed sub-agent."""

    agent_id: str
    depth: int
    task: str  # First 200 chars of task description
    error: str  # Exception message
    last_tool: str | None = None
    steps_completed: int = 0

    def __str__(self) -> str:  # noqa: D105
        parts = [f"Agent {self.agent_id} (depth={self.depth})"]
        parts.append(f"failed after {self.steps_completed} steps")
        if self.last_tool:
            parts.append(f"at tool '{self.last_tool}'")
        parts.append(f": {self.error}")
        return " ".join(parts)


class SpawnAgentTask(BaseModel):
    """One entry in a ``spawn_agents`` batch.

    Carries the SAME per-task fields as the single ``spawn_agent`` schema, but
    validated at definition: ``extra="forbid"`` rejects stray keys so a
    malformed fan-out fails fast instead of silently dropping a field, and a
    blank ``task`` is refused (an empty delegation is never intentional).
    """

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)
    model: str | None = None
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    # Deprecated — retained for schema/prompt compatibility, never enforced.
    max_steps: int | None = None
    acceptance_criteria: str | None = None
    agent_type: str | None = None
    # The workspace THIS child runs in, as a project key the catalog lists.
    # Absent (the default) inherits the parent's directory. Because the batch
    # and the single
    # spawn share one core, a single ``spawn_agents`` call can point each entry
    # at a different project — one fleet per repository under one hypervisor.
    # A key that resolves to nothing REFUSES the spawn (see
    # ``SpawnAgentTool._child_workspace``); it never degrades to the parent's
    # directory.
    project: str | None = Field(default=None, min_length=1)
    # Opt-in bounded auto-retry, parsed downstream by ``RetryPolicy``.
    # A batch entry can carry it just like a single spawn — one transient
    # failure in a wide fan-out then re-delegates instead of dropping a lane.
    retry: dict[str, Any] | None = None
    # Opt-in per-agent delegation bounds, parsed downstream by
    # ``DelegationContract``. A batch entry gets the same LAYERED-UNDER-the-
    # session-budget ceiling as a single spawn.
    contract: dict[str, Any] | None = None
    # Opt-in ground-truth completion check, parsed downstream by
    # ``CommandVerification.from_value``. When the master switch is on AND this
    # child can act (capability_mode ∈ {execute, all}), its claimed completion
    # is gated behind this command passing. Default off / inactive leaves the
    # completion ungated; a supplied-but-inactive spec is surfaced in the
    # spawn response + event, never silently dropped.
    verification: dict[str, Any] | None = None
    # Task-typed Communication Unit shape for
    # this sub-agent's final summary. ``None`` (default) is the untyped path —
    # ``_spawn_one`` stamps ``AgentResult.summary_kind`` with its own "generic"
    # default in that case, so an unset field changes nothing about a spawn's
    # behaviour or output.
    summary_kind: SummaryKind | None = None
    # Coarse delegation privilege ceiling — privilege attenuation.
    # A pre-filter LAYERED UNDER
    # ``allowed_tools``/``denied_tools`` — it can only remove more tools, never
    # add. Gates BOTH surfaces (the two-surface law): file/registry tools
    # via ``filter_specs`` AND per-agent session action tools via
    # ``SessionToolRegistry.build_for`` (session tools default to tier
    # ``execute``, so ``read_only`` admits none unless declared ``read``).
    # ``"all"`` (default) = no capability filtering.
    # Narrowed monotonically against the parent's effective
    # mode at spawn time, so a child can only ever restrict further. See
    # ``CapabilityMode`` for the tier law.
    capability_mode: CapabilityMode = "all"
    # Filesystem-containment ceiling — the SECOND privilege axis,
    # orthogonal to ``capability_mode``. Defaults to ``workspace_write`` (the
    # sensible sub-agent default: reads + writes confined to the workspace), yet
    # because narrowing is min-wins against the parent's own tier AND the ROOT
    # default is ``full_access``, a child spawned off a default root resolves to
    # ``workspace_write`` — so THIS seam, not the root's tier, is where the
    # filesystem firebreak is actually drawn. Narrowed monotonically, so a child
    # can only ever restrict further. See ``WorkspaceContainment`` for the tier
    # law and ``agent.workspace_enforcement`` for the switch that arms it.
    workspace_mode: WorkspaceMode = "workspace_write"
    # Delegation approval policy — PARSED + validated here but
    # ENFORCED by nothing in v1 (the approval-gate wiring is a later wave). Kept
    # on the schema now so the wire contract is forward-stable; a spawn that sets
    # it today behaves exactly as ``on_failure`` (i.e. no gate).
    approval_policy: ApprovalPolicy = "on_failure"

    # The ONE mapping from a declared
    # ``summary_kind`` to the one-line directive appended to the child's task
    # text. A `Literal`-keyed class constant, not a per-call `if kind ==`
    # chain: ``_spawn_one`` does a plain dict lookup. ``"generic"`` has no
    # entry — it is the untyped default and appends nothing.
    SUMMARY_KIND_DIRECTIVES: ClassVar[dict[SummaryKind, str]] = {
        "evidence": (
            "Your final summary must be an evidence package: the "
            "facts/quotes/paths the parent needs, not narrative."
        ),
        "running_summary": (
            "Your final summary must be a running summary: the task's "
            "cumulative state so far, written so a fresh reader needs no "
            "prior turns to pick it up."
        ),
        "code_signature": (
            "Your final summary must be a function/class-signature catalog: "
            "the names, signatures, and one-line purpose of every "
            "function/class you touched or introduced."
        ),
    }

    def to_args(self) -> dict[str, Any]:
        """Project to the ``args`` dict the single-spawn path consumes.

        Unset (``None``) fields are dropped so the downstream ``args.get(...)``
        defaults apply exactly as they do for an ad-hoc ``spawn_agent`` call —
        keeping the batch a thin reuse of ``_spawn_one`` rather than a fork.
        """
        return {k: v for k, v in self.model_dump().items() if v is not None}


@dataclass(frozen=True)
class ChildWorkspace:
    """The directory ONE child runs in, paired with the rules that govern it.

    The two are ONE fact, so they travel as one value: a child pointed at
    another project must read THAT project's instruction files, and a pair free
    to drift is how a fleet ends up working in one repository under another's
    rules — a miss that produces plausible work rather than an error.

    Resolved ONCE per spawn, at admission, rather than read off the tool at each
    attempt: :meth:`SpawnAgentTool.rebind_cwd` may move the workspace between a
    child's retry attempts, and a child must finish where it started.
    """

    cwd: str | None
    project_instructions: str | None


# What one spawn ATTEMPT reports back — a third vocabulary, deliberately
# neither of the other two. ``AgentStatus`` (the authority) describes where a
# registered agent sits in its lifecycle; ``AgentResultStatus`` describes what a
# child ACHIEVED. This one answers "how did the spawn CALL end", which spans
# both: an admission-time refusal (``rejected``) and an accepted-but-not-yet-run
# child (``submitted``) have no achievement to report, while a depth refusal
# reports ``cannot_solve`` and never becomes a registered agent at all.
#
# Its members are :data:`SettledStatus` (every terminal a settle can actually
# reach) plus the three outcomes that exist only at the spawn CALL: ``submitted``
# (accepted, not yet run), ``rejected`` (refused at admission), and
# ``cannot_solve`` (a depth refusal, which answers with a real ``AgentResult``
# and never becomes a registered agent).
#
# **It is NOT a superset of ``AgentResultStatus``, and an earlier docstring here
# claimed it was.** The member it drops is ``partial``, which nothing in this
# repository mints — a dead member on a vocabulary one layer down. Admitting it
# here would have propagated the dead value into a second vocabulary and into
# the batch wire payload, so this seam reports the terminal it SETTLED
# (``SettledStatus``) rather than re-reading the result's wider declared type.
# Resolving ``partial`` itself — minting it for the budget-exhausted subset of
# ``UNACHIEVED_DONE_REASONS``, or deleting it — is a change to
# ``AgentResultStatus`` and to three client mirrors, not to this alias.
#
# Typed rather than a bare ``str`` so no call site can invent a state the batch
# payload has no arm for. ``tests/test_child_settlement_parity.py`` pins the
# containment, so widening any of these vocabularies fails there rather than
# silently changing what a spawn is able to report.
SpawnOutcomeStatus = Literal[
    "submitted", "completed", "failed", "cancelled", "cannot_solve", "rejected"
]


@dataclass
class _SpawnOutcome:
    """Internal result of one admission+spawn attempt.

    Shared by the single (``run_async``) and batch (``run_batch_async``) entry
    points so both flow through the identical ``_spawn_one`` path. ``content``
    is the human-readable payload; ``agent_id`` is the spawned child's id
    (``None`` when nothing was spawned); ``status`` is the lifecycle/admission
    state; ``code`` is the TYPED cause of a refusal, ``None`` whenever a child
    was actually created.

    ``submitted`` covers BOTH a child that started immediately and one accepted
    while the fleet was full — it has an ``agent_id`` and a registered handle
    either way, and it will run. ``code`` exists because one opaque sentence
    cannot separate five unrelated causes, only one of which is worth retrying
    — and that one (capacity) refuses nothing at all.

    For a settled child, ``status`` is the terminal the settle REACHED
    (:data:`SettledStatus`), not a re-read of ``AgentResult.status`` — the two
    are the same value, but the result's declared type is wider than anything a
    settle can produce and routing it here would drag a dead member into this
    vocabulary. See :data:`SpawnOutcomeStatus`.
    """

    content: str
    agent_id: str | None
    status: SpawnOutcomeStatus
    code: SpawnRefusalCode | None = None

    def report(self) -> str:
        """The tool payload the model reads for this outcome.

        A refusal is reported through the SHARED structured-error envelope
        (``{"error": {code, message, permanence}}``), because that is already
        the codebase's contract for "a handled failure returned rather than
        raised": the loop reclassifies it as a FAILED step, the model still
        reads the full typed detail, and no second envelope grammar has to be
        invented. Marking it ``permanent`` is the load-bearing part — every
        refusal reachable here fails identically on a retry, and the loop has
        nowhere else to learn that from, since a ``MockSpeaker`` carries only
        ``content``.

        Serialized with ``json.dumps``, NOT ``str(dict)``. The graph suites'
        envelopes travel as a Python repr and ``_SessionToolError.parse`` reads
        them with ``ast.literal_eval``, whose docstring warns that a
        ``json.dumps`` payload is silently declined — but that warning is about
        payloads carrying ``true``/``false``/``null``. Every field here is a
        ``str``, so the JSON form is valid Python-literal syntax and the
        parser's cheap-reject already admits a leading ``{"error"``. JSON is
        also what the model reads, and it is what makes the payload parseable
        by every other consumer. ``tests/test_spawn_agent_flow.py`` pins the
        round trip against the REAL parser rather than leaving it to reasoning
        — a decline here would silently restore the ``success=True`` defect.

        Gated on ``rejected`` specifically, not on ``code``: a depth-exceeded
        spawn answers ``cannot_solve`` with a real ``AgentResult`` body and
        carries its code INSIDE it, because the parent is owed a result there
        rather than an error. Every other outcome returns ``content``
        unchanged.
        """
        if self.status != "rejected" or self.code is None:
            return self.content
        return json.dumps(
            {
                "error": {
                    "code": self.code,
                    "message": self.content,
                    "permanence": "permanent",
                }
            }
        )


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded auto-retry / re-delegation policy for a spawned sub-agent.

    Opt-in via the ``retry`` spawn-schema field; **DEFAULT OFF** (``max == 0``)
    so an unset/absent ``retry`` runs the child exactly once. On a *retryable*
    terminal failure the spawn bridge
    re-delegates the **same task on the same handle** (retaining the one already
    held semaphore slot for the whole sequence) up to ``max`` extra attempts,
    sleeping :meth:`backoff_for` with exponential growth between them.

    Model-level causes are deliberately NOT re-escalated here: every child
    ``ToolUseLoop`` run already drives the fallback ladder internally, so a
    fresh attempt gets a fresh ladder — this layer only re-runs a whole child
    whose loop died. ``rejected`` (declined at admission, before the loop) and
    ``cancelled`` (parent-cancelled → ``CancelledError``, re-raised, never
    retried) are structurally unreachable by the retry loop.
    """

    max: int = 0
    on: tuple[str, ...] = ("timeout", "failed")
    backoff: float = 1.0

    # The coarse retry-cause vocabulary. ``failed`` is the catch-all transient
    # terminal failure; ``timeout`` is the timeout-flavoured subset (mapped via
    # the shared classifier so this layer never re-derives provider semantics).
    _CAUSES: frozenset[str] = frozenset({"timeout", "failed"})

    @classmethod
    def from_value(cls, value: object) -> RetryPolicy:
        """Parse + validate the schema ``retry`` object. Unset/invalid → OFF.

        Validation is total (never raises): a malformed field degrades to the
        safe default rather than failing a spawn, since ``retry`` is an optional
        resilience hint, not a correctness contract.
        """
        if not isinstance(value, Mapping):
            return cls()
        raw_max: Any = value.get("max", 0)
        try:
            max_retries = max(0, int(raw_max))
        except (TypeError, ValueError):
            max_retries = 0
        on_val = value.get("on")
        if isinstance(on_val, (list, tuple)):
            on = tuple(str(x) for x in on_val if str(x) in cls._CAUSES)
        else:
            on = ("timeout", "failed")
        if not on:  # an explicit-but-empty/invalid list falls back to both
            on = ("timeout", "failed")
        raw_backoff: Any = value.get("backoff", 1.0)
        try:
            backoff = max(0.0, float(raw_backoff))
        except (TypeError, ValueError):
            backoff = 1.0
        return cls(max=max_retries, on=on, backoff=backoff)

    @property
    def enabled(self) -> bool:
        """True when at least one retry is permitted."""
        return self.max > 0

    def should_retry(self, cause: str, attempt: int) -> bool:
        """True when another attempt is allowed for this failure ``cause``.

        ``attempt`` is the number of attempts made so far (the one that just
        failed). Total attempts are bounded at ``max + 1``.
        """
        return attempt <= self.max and cause in self.on

    def backoff_for(self, attempt: int) -> float:
        """Exponential backoff (seconds) before the next attempt.

        ``attempt`` is the failed attempt's index (1-based), so the first retry
        waits ``backoff``, the second ``2 * backoff``, etc.
        """
        return self.backoff * (2 ** (max(1, attempt) - 1))

    @staticmethod
    def classify_cause(exc: BaseException) -> str:
        """Map a child-loop exception to a coarse retry cause.

        Reuses the ``RetryStrategy`` classifier's reason taxonomy (DRY — the
        delegation layer never re-derives provider/timeout semantics): a
        timeout/deadline-flavoured failure is ``"timeout"``; everything else
        (transient or otherwise) collapses to the generic ``"failed"``.
        """
        from mewbo_core.llm.llm_resilience import LlmResilienceExhausted, RetryStrategy

        reason = ""
        inner: BaseException = exc
        if isinstance(exc, LlmResilienceExhausted):
            reason = exc.reason or ""
            inner = exc.last_error or exc
        if reason not in ("timeout", "deadline"):
            reason = RetryStrategy.classify(inner).reason
        return "timeout" if reason in ("timeout", "deadline") else "failed"


# ---------------------------------------------------------------------------
# Terminal outcomes — HOW one child ended, as data the settle path reads
# ---------------------------------------------------------------------------

# The three terminals a SETTLE can produce — deliberately narrower than either
# vocabulary it feeds, because it is their INTERSECTION and one value has to
# satisfy both: ``AgentHypervisor.mark_done``/the attestation take
# ``AgentStatus``, while ``AgentResult`` takes ``AgentResultStatus``.
#
# Every excluded member is excluded for a reason a reader should not have to
# re-derive. ``submitted``/``running`` are non-terminal, so a settle can never
# mint one. ``rejected`` is decided at admission and returns before a child is
# ever registered, so it never reaches a settle either. ``cannot_solve`` and
# ``partial`` are ACHIEVEMENT verdicts with no hypervisor counterpart — the
# depth refusal builds its ``AgentResult`` directly for exactly that reason and
# does not route through :meth:`SpawnAgentTool._settle_child`.
#
# Typing this ``AgentStatus`` instead would compile only because the two
# vocabularies happen to overlap on these three, and would silently admit
# ``submitted`` into an ``AgentResult`` that has no arm for it.
SettledStatus = Literal["completed", "failed", "cancelled"]


@dataclass(frozen=True)
class _ChildTerminal:
    """How one spawned child ENDED — the family behind a single settle path.

    Three variants: the child's loop returned (:class:`_ChildSettled`), a
    parent cancelled it (:class:`_ChildCancelled`), or it raised
    (:class:`_ChildFailed`). Each owns its OWN answers for the terminal status,
    the ``stop`` event's fields, what the attestation records and the
    ``AgentResult`` handed back — so :meth:`SpawnAgentTool._settle_child` runs
    one unconditional sequence and there is no ``if kind ==`` anywhere.

    Why the family exists: without it that sequence is written TWICE — once in
    ``_spawn_one`` for the blocking nested spawn, once in
    ``_run_child_lifecycle`` for the background root manager — and two copies
    drift, starting with which model they name in the error they build.
    A second copy of a settle nobody re-reads diverges silently; it never
    raises, it just tells two callers different things about the same child.

    Plain frozen dataclasses, not Pydantic: these are hot in-process values
    that cross no trust boundary.
    """

    summary_kind: SummaryKind

    @property
    def status(self) -> SettledStatus:
        """The terminal this child settles into — see :data:`SettledStatus`."""
        raise NotImplementedError

    @property
    def stop_detail(self) -> str:
        """``detail`` on this child's ONE terminal ``stop`` event."""
        raise NotImplementedError

    @property
    def stop_summary(self) -> str | None:
        """Compressed CU echoed onto the ``stop`` event; ``None`` for none."""
        return None

    @property
    def stop_summary_kind(self) -> SummaryKind | None:
        """CU shape stamped on the ``stop`` event — it rides WITH a summary."""
        return self.summary_kind

    @property
    def attested_summary(self) -> str:
        """Text the attestation chain HASHES (never persisted verbatim)."""
        return ""

    @property
    def attested_done_reason(self) -> str | None:
        """Short label the terminal attestation records."""
        return self.stop_detail

    def registry_error(
        self, child_ctx: AgentContext | None, handle: AgentHandle | None
    ) -> AgentError | None:
        """Structured cause for ``mark_done`` — ``None`` when there is none.

        The context and the handle arrive as ARGS: a terminal describes an
        outcome and must not reach for the live objects that produced it.
        """
        return None

    def build_result(
        self, handle: AgentHandle | None, attestation_hash: str
    ) -> AgentResult:
        """The Communication Unit this terminal hands back to the spawner."""
        raise NotImplementedError

    @staticmethod
    def _steps(handle: AgentHandle | None) -> int:
        """Steps consumed, degrading to 0 for a child that never got a handle."""
        return handle.steps_completed if handle is not None else 0

    @staticmethod
    def _attempts(handle: AgentHandle | None) -> int:
        """Admissions+runs of this task; 1 when there is no handle to ask."""
        return handle.attempts if handle is not None else 1


@dataclass(frozen=True)
class _ChildSettled(_ChildTerminal):
    """The child's loop RETURNED — its own state names the terminal.

    A return is not evidence of success, which is why the status comes from
    :meth:`OrchestrationState.terminal_status` rather than from the mere fact
    that the loop stopped.
    """

    tq: Any
    state: OrchestrationState
    summary: str

    @property
    def status(self) -> SettledStatus:
        """Projected from the child's own state, never from the mere return."""
        return self.state.terminal_status()

    @property
    def stop_detail(self) -> str:
        """The loop's ``done_reason``, or ``completed`` when it named none."""
        return self.state.done_reason or "completed"

    @property
    def stop_summary(self) -> str | None:
        """The CU, capped so a verbose child cannot bloat the parent's log."""
        return self.summary[:_SUB_AGENT_SUMMARY_CAP]

    @property
    def attested_summary(self) -> str:
        """The UNCAPPED CU — the chain hashes it, so nothing rides verbatim."""
        return self.summary

    @property
    def attested_done_reason(self) -> str | None:
        """The RAW ``done_reason`` — no ``"completed"`` substitute.

        The attestation records what the loop actually reported; the ``stop``
        event's ``detail`` is a human-facing label and may fall back.
        """
        return self.state.done_reason

    def build_result(
        self, handle: AgentHandle | None, attestation_hash: str
    ) -> AgentResult:
        """The child's answer, its terminal, and its verifier provenance."""
        # Build Communication Unit — compressed context for parent
        return AgentResult(
            content=self.tq.task_result or self.state.done_reason or "No result",
            status=self.status,
            steps_used=self._steps(handle),
            summary=self.summary[:500],
            attempts=self._attempts(handle),
            summary_kind=self.summary_kind,
            attestation_hash=attestation_hash,
            # Projected from the child's state so a spawner sees an honest
            # done-claim: None when the gate never ran, else pass/fail.
            verified=self.state.verified,
            verify_attempts=self.state.verify_attempts,
        )


@dataclass(frozen=True)
class _ChildCancelled(_ChildTerminal):
    """A parent cancelled this child — terminal, and never retried."""

    partial: str

    @property
    def status(self) -> SettledStatus:
        """Always ``cancelled`` — a parent's cancellation is the whole story."""
        return "cancelled"

    @property
    def stop_detail(self) -> str:
        """Names WHO ended the child, which its bare status cannot."""
        return "cancelled by parent"

    @property
    def stop_summary_kind(self) -> SummaryKind | None:
        """``None`` — a cancelled child produced no CU, so it declares no shape."""
        return None

    @property
    def attested_summary(self) -> str:
        """Whatever the child had answered before it was stopped."""
        return self.partial

    @property
    def attested_done_reason(self) -> str | None:
        """The bare terminal, not the event's human-facing detail."""
        return "cancelled"

    def build_result(
        self, handle: AgentHandle | None, attestation_hash: str
    ) -> AgentResult:
        """A CU with no content — but the declared shape is still reported."""
        return AgentResult(
            content="Cancelled",
            status="cancelled",
            steps_used=self._steps(handle),
            attempts=self._attempts(handle),
            summary_kind=self.summary_kind,
            attestation_hash=attestation_hash,
        )


@dataclass(frozen=True)
class _ChildFailed(_ChildTerminal):
    """The child's loop RAISED — the exception classified and bounded ONCE.

    A provider exception can embed an entire upstream HTML error page, so every
    field derived here routes through :class:`RunError` rather than a raw
    ``str(exc)`` slice.
    """

    run_error: RunError
    partial: str
    task_desc: str

    @classmethod
    def of(
        cls,
        exc: BaseException,
        *,
        child_ctx: AgentContext | None,
        summary_kind: SummaryKind,
        task_desc: str,
        partial: str,
        fallback_model: str = "",
    ) -> _ChildFailed:
        """Classify *exc* against the model the child's loop actually ran on.

        ``child_ctx.model_name`` is what ``ToolUseLoop`` was constructed from,
        so it is the model that failed — and it is the ONE spelling reachable
        from both settle paths. It equals the spawn's own resolved model by
        construction (``AgentContext.child`` stores ``model_name or
        self.model_name``, and the context is frozen with no mutation site), so
        naming it here changes no value; it removes the second name.

        *fallback_model* covers only the window before a child context exists at
        all — a raise between resolving the model and ``child()`` returning.
        """
        model = child_ctx.model_name if child_ctx is not None else fallback_model
        return cls(
            summary_kind=summary_kind,
            run_error=RunError.from_exception(exc, model=model),
            partial=partial,
            task_desc=task_desc,
        )

    @property
    def status(self) -> SettledStatus:
        """Always ``failed`` — a raise leaves no room for a softer reading."""
        return "failed"

    @property
    def stop_detail(self) -> str:
        """``title``, not ``brief``.

        The detail is a lane label in the console and the attestation's short
        ``done_reason``; a bounded title is markup-free by construction, so it
        can never carry the head of an upstream HTML error page into either.
        """
        return self.run_error.title

    @property
    def stop_summary(self) -> str | None:
        """Partial work, which survives the failure."""
        return self.partial

    @property
    def attested_summary(self) -> str:
        """The same partial work, hashed into the chain rather than stored."""
        return self.partial

    def registry_error(
        self, child_ctx: AgentContext | None, handle: AgentHandle | None
    ) -> AgentError:
        """The bounded failure context ``check_agents`` and the tree render."""
        return AgentError(
            agent_id=child_ctx.agent_id if child_ctx else "unknown",
            depth=child_ctx.depth if child_ctx else 0,
            task=self.task_desc[:200],
            error=self.run_error.brief(),
            last_tool=handle.last_tool_id if handle else None,
            steps_completed=self._steps(handle),
        )

    def build_result(
        self, handle: AgentHandle | None, attestation_hash: str
    ) -> AgentResult:
        """The bounded blurb, never the raw exception, plus the partial work."""
        brief = self.run_error.brief()
        return AgentResult(
            content=f"Sub-agent failed: {brief}",
            status="failed",
            steps_used=self._steps(handle),
            warnings=[brief],
            # Checkpoint — partial work survives failure
            summary=self.partial,
            # The last error after a spent retry budget; attempts shows how
            # many re-delegations were tried before giving up.
            attempts=self._attempts(handle),
            summary_kind=self.summary_kind,
            attestation_hash=attestation_hash,
        )


# ---------------------------------------------------------------------------
# Plugin-generic body substitution (KISS — no Jinja, no template engine)
# ---------------------------------------------------------------------------

# ``${VAR:-default}`` — bash-style fallback. ``\w+`` caps the variable name
# to identifier characters; ``[^}]*`` keeps the default body shell-literal
# (no nested ``}``) without needing a full parser.
_BASH_DEFAULT_RE = re.compile(r"\$\{(\w+):-([^}]*)\}")


# ------------------------------------------------------------------
# Tool schema (injected into bind_tools, NOT in ToolRegistry)
# ------------------------------------------------------------------

SPAWN_AGENT_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": (
            "Spawn a sub-agent for a genuinely independent subtask that "
            "benefits from parallel execution. Do NOT use for simple "
            "sequential operations — use your tools directly instead. "
            "The sub-agent starts immediately when the fleet has a free "
            "concurrency slot and otherwise waits until one frees, so a busy "
            "fleet delays a spawn — it never drops one. Either way it comes "
            "back 'submitted' with a real agent_id you monitor with "
            "check_agents. A spawn can still be REFUSED, but "
            "only for a permanent reason (unknown agent_type, unresolvable "
            "project, unavailable model, delegation depth limit): the refusal "
            "names it in a 'code' field, and re-issuing the same call "
            "unchanged will fail identically — fix the named argument instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The specific task for the sub-agent to complete",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for this sub-agent",
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tool IDs the sub-agent is allowed to use. OMIT the "
                        "field for all tools; an empty list grants none."
                    ),
                },
                "denied_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool IDs explicitly denied to the sub-agent.",
                },
                "max_steps": {
                    "type": "integer",
                    "description": (
                        "Deprecated. Sub-agents run until natural completion. "
                        "This field is retained for prompt compatibility but "
                        "is not enforced."
                    ),
                },
                "acceptance_criteria": {
                    "type": "string",
                    "description": (
                        "How to verify this sub-task is complete "
                        "(e.g., 'file exists and tests pass'). "
                        "Contract-first decomposition."
                    ),
                },
                "agent_type": {
                    "type": "string",
                    "description": (
                        "Name of a registered agent type to use "
                        "(e.g. 'feature-dev:code-reviewer'). "
                        "Loads pre-defined system prompt, tool scope, and model "
                        "from the agent registry."
                    ),
                },
                "project": {
                    "type": "string",
                    "description": (
                        "Project key naming the workspace this sub-agent works "
                        "in, exactly as the project catalog lists it (e.g. a "
                        f"configured project name or '{MANAGED_PREFIX}<id>'). "
                        "OMIT it to use your own working directory — that is the "
                        "default, and the right choice whenever the subtask is "
                        "about the code you are already in. Set it to run a "
                        "sub-agent against a DIFFERENT repository: in one "
                        "spawn_agents call each entry may name its own project, "
                        "so several fleets work in several repositories at once "
                        "under one session. The sub-agent reads that "
                        "project's own instruction files. A key that names no "
                        "checked-out directory is refused — the spawn does not "
                        "fall back to your directory."
                    ),
                },
                "retry": {
                    "type": "object",
                    "description": (
                        "Optional bounded auto-retry for THIS sub-agent. Default "
                        "off. On a transient terminal failure the SAME task is "
                        "re-delegated up to 'max' times with exponential backoff "
                        "— use for a wide fan-out so one transient failure does "
                        "not silently drop a workstream. Model-level failures "
                        "already reuse the built-in fallback ladder within each "
                        "attempt; cancelled/rejected agents are never retried."
                    ),
                    "properties": {
                        "max": {
                            "type": "integer",
                            "description": "Max extra retry attempts (0 = off, default).",
                        },
                        "on": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["timeout", "failed"]},
                            "description": "Failure causes to retry. Default: both.",
                        },
                        "backoff": {
                            "type": "number",
                            "description": "Base backoff seconds between attempts. Default 1.0.",
                        },
                    },
                },
                "summary_kind": {
                    "type": "string",
                    "enum": list(get_args(SummaryKind)),
                    "description": (
                        "Task-typed shape for this sub-agent's final summary "
                        "(e.g. an evidence package vs. a running summary). "
                        "Default: generic (untyped)."
                    ),
                },
                "capability_mode": {
                    "type": "string",
                    "enum": list(get_args(CapabilityMode)),
                    "description": (
                        "Coarse privilege ceiling for ALL of this sub-agent's "
                        "tools — both file/registry tools AND session action "
                        "tools (submit/mint/commit/arm) — layered UNDER "
                        "allowed_tools/denied_tools (it only removes tools, never "
                        "adds). 'read_only' = read-only tools only (no writes, "
                        "shell, or session actions); 'execute' = all declared "
                        "read/write/exec tools; 'all' (default) = no capability "
                        "restriction. Narrowed against this agent's own ceiling, "
                        "so a child can never widen it. "
                        "Privilege attenuation."
                    ),
                },
                "workspace_mode": {
                    "type": "string",
                    "enum": list(get_args(WorkspaceMode)),
                    "description": (
                        "Filesystem-containment ceiling for this sub-agent — the "
                        "paths its tools may touch, orthogonal to capability_mode "
                        "(which gates WHICH tools it holds). 'read_only' = reads "
                        "confined to the workspace, no writes anywhere; "
                        "'workspace_write' (default) = reads + writes confined to "
                        "the workspace root + scratch; 'full_access' = no path "
                        "restriction. Narrowed against this agent's own ceiling, "
                        "so a child can never widen it. "
                        "Privilege attenuation."
                    ),
                },
                "approval_policy": {
                    "type": "string",
                    "enum": list(get_args(ApprovalPolicy)),
                    "description": (
                        "When this sub-agent should pause for parent approval "
                        "('never', 'on_failure' (default), 'on_request'). Reserved "
                        "for a later wave — accepted and validated now, not yet "
                        "enforced."
                    ),
                },
                "contract": {
                    "type": "object",
                    "description": (
                        "Optional bounds on THIS sub-agent's own delegation budget "
                        "— layered UNDER the shared session step budget, never "
                        "above it. Default off (no contract == an unbounded "
                        "child)."
                    ),
                    "properties": {
                        "max_steps": {
                            "type": "integer",
                            "description": (
                                "Max tool-execution steps for this agent alone "
                                "(0 = unlimited, default). Warned inside "
                                "step_warn_headroom, force-wrapped-up at the limit."
                            ),
                        },
                        "max_wall_s": {
                            "type": "number",
                            "description": (
                                "Max wall-clock seconds this agent may run "
                                "(0 = unlimited, default). Warned at 80%, "
                                "force-cancelled at 100% by the watchdog."
                            ),
                        },
                        "max_tokens": {
                            "type": "integer",
                            "description": (
                                "Advisory token ceiling (0 = no signal, default). "
                                "Best-effort only: cost accounting is out of "
                                "scope, and enforcement depends on the model "
                                "client actually reporting usage."
                            ),
                        },
                        "autonomy": {
                            "type": "string",
                            "enum": list(get_args(AutonomyTier)),
                            "description": (
                                "'atomic' strips this agent's (and every "
                                "descendant's) ability to spawn further "
                                "sub-agents — a hard delegation firebreak. "
                                "'open_ended' (default) delegates normally."
                            ),
                        },
                        "model_tier": {
                            "type": "string",
                            "enum": list(get_args(ModelTier)),
                            "description": (
                                "Coarse model-cost hint resolved against the "
                                "deployment's agent.model_tiers map. Never "
                                "overrides an explicit model arg or an "
                                "agent_type's configured model."
                            ),
                        },
                        "step_warn_headroom": {
                            "type": "integer",
                            "description": (
                                "Steps before max_steps at which the warning "
                                "fires (default 3)."
                            ),
                        },
                    },
                },
                "verification": {
                    "type": "object",
                    "description": (
                        "Optional ground-truth completion check. When enabled by "
                        "the deployment AND this sub-agent can act, its claimed "
                        "completion is accepted ONLY if this command exits 0 — a "
                        "failure re-drives the agent with the command's output up "
                        "to the configured retry cap. Use for a task with an "
                        "objective pass/fail check (tests, a build, a linter). "
                        "Default off (no verification == an ungated child)."
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["command"],
                            "description": (
                                "Verification kind. Only 'command' today (run an "
                                "argv, exit 0 = pass). Optional — defaults to "
                                "'command'."
                            ),
                        },
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": (
                                "Command + args as a list (NEVER a shell string) "
                                "— e.g. ['pytest', '-q']. Run under a scrubbed "
                                "environment; exit 0 is a pass."
                            ),
                        },
                        "cwd": {
                            "type": "string",
                            "description": (
                                "Working directory for the check (defaults to the "
                                "agent's workspace)."
                            ),
                        },
                        "timeout_s": {
                            "type": "number",
                            "description": (
                                "Per-check timeout in seconds (default 60), "
                                "clamped down to the deployment's ceiling."
                            ),
                        },
                    },
                    "required": ["argv"],
                },
            },
            "required": ["task"],
        },
    },
}


# Batch fan-out. The array's ``items`` schema IS the single
# ``spawn_agent`` parameters object (DRY — one source of truth for the per-task
# fields), so every entry takes the same fields and a new spawn field is picked
# up by both tools automatically.
SPAWN_AGENTS_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "spawn_agents",
        "description": (
            "Fan out MULTIPLE independent sub-agents in ONE call — the preferred "
            "path when you have N genuinely independent subtasks. Every entry is "
            "admitted together (reliable parallel admission even if you can't emit "
            "N parallel tool-calls), returning an ORDERED list of agent_ids you "
            "monitor with check_agents. Each entry takes the SAME fields as "
            "spawn_agent. Fan out as wide as the work genuinely is: every entry "
            "comes back 'submitted' with an agent_id, and any beyond the fleet's "
            "concurrency limit simply start later as their siblings finish (the "
            "reply's 'dispatched'/'deferred' counts say how many of each), so a "
            "wide batch is throttled, never truncated. An entry is only "
            "'rejected' for "
            "a permanent reason, which its 'code' names. Do NOT use for "
            "sequential work — use your tools directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": SPAWN_AGENT_SCHEMA["function"]["parameters"],  # type: ignore[index]
                    "description": (
                        "Independent sub-tasks to spawn concurrently. Order is "
                        "preserved in the returned agent_ids."
                    ),
                },
            },
            "required": ["tasks"],
        },
    },
}


CHECK_AGENTS_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "check_agents",
        "description": (
            "Check the status of all spawned sub-agents. Returns the agent "
            "tree with progress notes and completed results. Use after "
            "spawning agents to monitor progress and collect results. "
            "Process-level monitoring."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wait": {
                    "type": "boolean",
                    "description": (
                        "If true, wait up to timeout seconds for at least "
                        "one running agent to complete before returning. "
                        "Default: false."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": ("Max seconds to wait when wait=true. Default: 30."),
                },
            },
            "required": [],
        },
    },
}

STEER_AGENT_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "steer_agent",
        "description": (
            "Send a steering message to a running sub-agent, or cancel it. "
            "Use to inject context, course-correct, or stop stuck agents. "
            "Adaptive coordination."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        "The agent_id to steer (8-char prefix from check_agents output)."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["message", "cancel"],
                    "description": (
                        "Action: 'message' sends NL feedback to the agent, "
                        "'cancel' cancels the agent."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": (
                        "The steering message to send (required when action='message')."
                    ),
                },
            },
            "required": ["agent_id", "action"],
        },
    },
}


# ------------------------------------------------------------------
# SpawnAgentTool
# ------------------------------------------------------------------


class SpawnAgentTool:
    """Spawns a child ToolUseLoop as a sub-agent."""

    def __init__(
        self,
        *,
        agent_context: AgentContext,
        tool_registry: ToolRegistry,
        permission_policy: PermissionPolicy,
        approval_callback: Callable[[ActionStep], bool] | None = None,
        hook_manager: HookManager,
        safety_plane: SafetyPlane | None = None,
        project_instructions: str | None = None,
        user_instructions: str | None = None,
        cwd: str | None = None,
        agent_registry: Any = None,
        session_tool_registry: SessionToolRegistry | None = None,
        session_capabilities: tuple[str, ...] = (),
        enable_skills: bool = True,
        catalog: ProjectCatalog | None = None,
    ) -> None:
        """Initialize with parent context and shared registries."""
        self._agent_context = agent_context
        self._tool_registry = tool_registry
        self._permission_policy = permission_policy
        self._approval_callback = approval_callback
        self._hook_manager = hook_manager
        self._safety_plane = safety_plane
        self._project_instructions = project_instructions
        # Operator-authored custom instructions are inherited by every child,
        # exactly like the project instructions — they describe the deployment,
        # not one agent's task, so a sub-agent that lost them would be running
        # under different rules than its parent.
        self._user_instructions = user_instructions
        self._cwd = cwd
        # The catalog a per-spawn ``project`` key is resolved through. Injected
        # rather than reached for: a CLI drive has no app-side stores to build
        # one from, and ``None`` there must refuse a ``project`` outright rather
        # than resolve it to something plausible.
        self._catalog = catalog
        self._agent_registry = agent_registry
        self._session_tool_registry = session_tool_registry
        self._session_capabilities = session_capabilities
        # Children inherit the parent drive's skill policy: a headless search
        # run disables auto-skill injection for the ROOT *and* every probe it
        # spawns (the audit found every server-side agent burning step 1 on
        # ``activate_skill``).
        self._enable_skills = enable_skills
        # Plan-mode context — set by ToolUseLoop.run() so children
        # inherit the session's plan path and mode.
        self.session_id: str | None = None
        self.parent_mode: str = "act"
        # The parent's EFFECTIVE spec set, stamped by ToolUseLoop.run() alongside
        # the plan context (both are run()-time state — the loop only learns its
        # own specs when it is handed them, long after this tool is constructed).
        # Containment must be monotone: a child narrows, never widens. Deriving a
        # child from the raw registry instead would hand it tools the parent never
        # held, since a parent's set can be narrowed by scoping this tool cannot
        # reconstruct (a console session's ``allowed_tools`` over MCP tools, an
        # ancestor's own filtering). ``None`` means unstamped — fall back to the
        # registry so a directly-constructed tool keeps the full set.
        self.parent_tool_specs: list[ToolSpec] | None = None
        # Track lifecycle manager tasks for deterministic cleanup.
        self._lifecycle_tasks: list[asyncio.Task[None]] = []

    def rebind_active_model(self, model_name: str) -> None:
        """Re-seat this tool's parent context onto an escalated model.

        The public seam for the loop's sticky model escalation. A parent that
        healed itself onto a rescue model must not keep spawning children onto
        the dead one: :meth:`_resolve_model` falls back to the parent context's
        ``model_name``, and each child's own context is derived from it, so a
        stale value here re-infects the whole subtree.

        ``AgentContext`` is frozen, so this REPLACES rather than mutates — and
        that is exactly why the seam is a method and not an attribute write.
        Knowing the context is a frozen dataclass is this class's business, not
        the loop's; reaching in to do the ``replace`` from outside couples the
        caller to a representation it should never have to know. Idempotent, so
        the loop may call it on every turn.
        """
        if not model_name or model_name == self._agent_context.model_name:
            return
        self._agent_context = replace(self._agent_context, model_name=model_name)

    def rebind_cwd(self, cwd: str, *, project_instructions: str | None) -> None:
        """Re-point the workspace every FUTURE child inherits.

        The seam a session-level project switch drives: from here on, a spawn
        that names no ``project`` of its own lands in *cwd* and reads
        *project_instructions* instead of the ones this tool was built with.

        Already-running children are deliberately untouched. A live child's loop
        captured its own directory when it was built and has been resolving
        paths against it ever since; moving that out from under it would be a
        race no one owns — its containment root, its verifier's working
        directory and its half-finished edits would disagree about where it is.
        A child finishes where it started; the switch applies to the next one.
        That is also why a spawn resolves its :class:`ChildWorkspace` once at
        admission rather than re-reading these fields on each retry attempt.
        """
        self._cwd = cwd
        self._project_instructions = project_instructions

    def _child_workspace(self, project: object) -> ChildWorkspace:
        """Resolve the workspace ONE child runs in, or refuse to spawn it.

        An absent ``project`` inherits this agent's own directory and
        instructions — the overwhelmingly common case.

        A named project is resolved through the injected catalog, and a failure
        REFUSES rather than falling back: a fleet quietly working in the wrong
        repository produces plausible-looking work no one thinks to check, which
        is a far worse outcome than a spawn that never started. The caller named
        a key deliberately, so the parent's directory is not a safe substitute
        for it.

        Raises:
            ProjectResolutionError: the key names no runnable directory, or
                there is no catalog to resolve it with.
        """
        if not isinstance(project, str) or not project.strip():
            return ChildWorkspace(
                cwd=self._cwd, project_instructions=self._project_instructions
            )
        key = project.strip()
        if self._catalog is None:
            raise ProjectResolutionError(
                "no_catalog",
                f"Project '{key}' cannot be resolved: this session has no project "
                "catalog. Omit 'project' to run the sub-agent in this agent's own "
                "directory.",
            )
        cwd = self._catalog.resolve(key)
        # A child in another repository reads THAT repository's rules. Handing
        # it the parent's is the same class of miss as handing it the parent's
        # directory, and the quieter of the two.
        return ChildWorkspace(
            cwd=cwd, project_instructions=discover_project_instructions(cwd)
        )

    async def run_async(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a single sub-agent. Returns the result as a MockSpeaker.

        Thin wrapper over :meth:`_spawn_one` — the batch path
        (:meth:`run_batch_async`) shares the same core. The outcome is
        projected through :meth:`_SpawnOutcome.report`, not read off
        ``content``: reading the string would throw away the refusal's status
        and typed cause, handing the model a bare sentence for five different
        failures.
        """
        args = (
            action_step.tool_input
            if isinstance(action_step.tool_input, dict)
            else {"task": str(action_step.tool_input)}
        )
        outcome = await self._spawn_one(args, blocking_admit=True)
        return MockSpeaker(content=outcome.report())

    async def run_batch_async(self, action_step: ActionStep) -> MockSpeaker:
        """Fan out a batch of independent sub-agents from ONE tool call.

        Pure composition over :meth:`_spawn_one`, in two phases. Each entry is
        RESOLVED first (its workspace, agent type and model settled, its handle
        registered, its id minted), then the whole batch is handed to the
        scheduler in ONE atomic acceptance: every entry that resolved is
        accepted, and the scheduler decides only which start now and which
        wait. A batch is therefore throttled by concurrency, never truncated by
        it. Marking the surplus ``rejected`` and discarding it would read to
        the caller as N agents when only ``max_concurrent`` existed.

        Returns the ordered ``agent_id``s; the model collects results via the
        existing ``check_agents``. The orchestration loop is untouched.
        """
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        raw_tasks = raw.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return MockSpeaker(
                content="ERROR: spawn_agents requires a non-empty 'tasks' array."
            )
        try:
            tasks = [SpawnAgentTask.model_validate(entry) for entry in raw_tasks]
        except ValidationError as exc:
            return MockSpeaker(content=f"ERROR: invalid spawn_agents task: {exc}")

        agents: list[dict[str, Any]] = []
        agent_ids: list[str | None] = []
        units: list[SpawnUnit] = []
        accepted = 0
        for idx, task in enumerate(tasks):
            outcome = await self._spawn_one(
                task.to_args(), blocking_admit=False, batch=units, batch_index=idx
            )
            agent_ids.append(outcome.agent_id)
            if outcome.agent_id is not None:
                accepted += 1
            entry: dict[str, Any] = {
                "index": idx,
                "agent_id": outcome.agent_id,
                "status": outcome.status,
                "task": task.task[:200],
            }
            if outcome.agent_id is None:
                # ADDITIVE, and load-bearing: a refused slot is otherwise the
                # only place a refusal's cause is discarded. ``code`` is the
                # machine-readable half — "no free concurrency slot" would be a
                # wrong answer that sends the caller retrying a project key
                # that will never resolve.
                entry["reason"] = outcome.content
                if outcome.code is not None:
                    entry["code"] = outcome.code
            agents.append(entry)

        # ONE atomic acceptance for the whole fan-out. Nothing here can refuse;
        # the flags say only which entries got a slot immediately. Every
        # accepted entry stays ``submitted`` either way — starting now versus
        # waiting is a SCHEDULING fact, reported as a count, never as a
        # per-agent status the whole client tree would have to learn.
        decisions = await self._agent_context.registry.accept_batch(units)
        started = sum(1 for dispatched in decisions if dispatched)

        rejected = len(tasks) - accepted
        deferred = len(units) - started
        summary = f"Accepted {accepted}/{len(tasks)} agent(s)"
        if units:
            summary += (
                f" — {started} started now, {deferred} waiting for a free slot "
                "(they start automatically)"
            )
        if rejected:
            summary += f"; {rejected} refused — see each entry's 'code' and 'reason'"
        summary += ". Use check_agents to monitor progress and collect results."
        return MockSpeaker(
            content=json.dumps(
                {
                    "kind": "agent_batch",
                    "text": summary,
                    "agents": agents,
                    "agent_ids": agent_ids,
                    # ``accepted`` is the name the count now deserves and the
                    # one consumers read FIRST; ``spawned`` is retained at the
                    # same value so an older reader is unaffected and so the
                    # consumer-side ``accepted ?? spawned`` fallback keeps
                    # serving events already in the store. Emitting only
                    # ``spawned`` would leave every consumer permanently on its
                    # fallback branch with the preferred key dead.
                    "accepted": accepted,
                    "spawned": accepted,
                    "dispatched": started,
                    "deferred": deferred,
                    "rejected": rejected,
                }
            )
        )

    def _mark_dispatched(
        self,
        child_ctx: AgentContext,
        handle: AgentHandle,
        *,
        task_desc: str,
        agent_type: str | None,
        model: str,
        contract: DelegationContract,
        verification_note: str | None,
        model_fallback_note: str | None,
    ) -> None:
        """Flip an ACCEPTED child to running and write its start provenance.

        The ONE seam for "this agent is actually beginning now", shared by the
        deferred root path (called from the scheduler's launcher) and the
        nested path (called inline, since a nested child runs on its parent's
        slot and is never deferred). Everything here is deliberately downstream
        of acceptance: a child that waits for a slot and is cancelled first
        must leave no start hook, no ``start`` event and no spawn attestation
        behind, because none of them would ever be closed.

        ``started_at`` is re-stamped HERE rather than trusted from
        construction: it is what a wall-clock contract burns down against, and
        a child that waited must not arrive with part of its deadline spent.
        The ``start`` event is emitted while the handle still reads
        ``submitted``; the flip to ``running`` follows.
        """
        handle.started_at = time.monotonic()
        self._hook_manager.run_on_agent_start(handle)
        self._emit_event(
            child_ctx,
            "start",
            task_desc,
            handle=handle,
            verification=verification_note,
            model_fallback=model_fallback_note,
        )
        handle.attestation_spawn_hash = self._record_spawn_attestation(
            child_ctx,
            agent_type=agent_type,
            model=model,
            capability_mode=child_ctx.capability_mode,
            contract=contract,
        )
        # Transition to "running" when execution begins.
        handle.status = "running"

    @staticmethod
    def _refusal(*, code: SpawnRefusalCode, reason: str) -> _SpawnOutcome:
        """Build the ONE refusal shape — a typed cause beside the human reason.

        Every refusal reachable from here is PERMANENT: the caller must change
        an argument, because re-issuing the same call fails identically.
        Capacity is deliberately absent — an over-subscribed spawn waits in the
        :class:`~mewbo_core.agents.hypervisor.AgentQueue`, it is never refused.
        """
        return _SpawnOutcome(
            content=f"ERROR: {reason}", agent_id=None, status="rejected", code=code
        )

    async def _spawn_one(
        self,
        args: dict[str, Any],
        *,
        blocking_admit: bool = True,
        batch: list[SpawnUnit] | None = None,
        batch_index: int = 0,
    ) -> _SpawnOutcome:
        """Admit and launch ONE sub-agent — shared core of single + batch spawn.

        ``blocking_admit`` names the CALLER SHAPE, not an admission mode:
        admission never blocks or refuses for capacity, so the flag only
        distinguishes a single, deliberate ``spawn_agent`` (``True`` →
        ``critical`` queue precedence) from one entry of a wide ``spawn_agents``
        fan-out (``False`` → ``normal``), keeping a targeted delegation from
        starving behind a 26-way batch.

        ``batch`` defers acceptance: when supplied, the resolved unit is
        appended to it instead of being handed to the scheduler here, so
        :meth:`run_batch_async` can accept the whole fan-out in one atomic
        step. ``batch_index`` is that entry's declared position.

        Returns a :class:`_SpawnOutcome` carrying the human-readable
        ``content`` plus the structured ``agent_id``/``status``/``code``.
        """
        from mewbo_core.llm.prompt_registry import get_prompt_registry

        registry_prompts = get_prompt_registry()
        task_desc = str(args.get("task", ""))
        acceptance_criteria = str(args.get("acceptance_criteria", "") or "")
        if acceptance_criteria:
            # Contract-first decomposition: delegation is contingent upon the
            # outcome having precise verification.
            task_desc += registry_prompts.render(
                "spawn.acceptance_criteria", acceptance_criteria=acceptance_criteria
            )
        # Task-typed Communication Unit: an
        # explicit `summary_kind` appends its one-line format directive to the
        # child's task text and is stamped onto every `AgentResult` this spawn
        # eventually returns. Unset/unrecognised -> "generic", the untyped
        # summary, which appends nothing to the task text. The directive
        # lookup is a plain dict read on `SpawnAgentTask` — never a branch here.
        raw_summary_kind = args.get("summary_kind")
        summary_kind: SummaryKind = "generic"
        if (
            isinstance(raw_summary_kind, str)
            and raw_summary_kind in SpawnAgentTask.SUMMARY_KIND_DIRECTIVES
        ):
            summary_kind = cast(SummaryKind, raw_summary_kind)
            task_desc += f"\n\n{SpawnAgentTask.SUMMARY_KIND_DIRECTIVES[summary_kind]}"
        # Coarse delegation privilege ceiling. The raw
        # REQUESTED mode; the child context narrows it against this agent's own
        # effective ceiling in ``child()``, so it can only ever restrict.
        raw_capability_mode = args.get("capability_mode", "all")
        requested_capability_mode = (
            raw_capability_mode if isinstance(raw_capability_mode, str) else "all"
        )
        # Filesystem-containment ceiling. The raw REQUESTED tier;
        # ``child()`` narrows it min-wins against this agent's own effective tier,
        # so it can only ever restrict. Default ``workspace_write`` mirrors the
        # ``SpawnAgentTask`` field default (inert until enforcement is flipped on).
        raw_workspace_mode = args.get("workspace_mode", "workspace_write")
        requested_workspace_mode = (
            raw_workspace_mode if isinstance(raw_workspace_mode, str) else "workspace_write"
        )
        # Per-agent delegation bounds — layered UNDER the shared
        # session step budget, never above it. Parsed total (never raises); an
        # absent/malformed ``contract`` is the disabled default: an unbounded
        # child.
        contract = DelegationContract.from_value(args.get("contract"))
        # Ground-truth completion check — parsed total (never raises); an
        # absent/malformed ``verification`` is ``None`` (the ungated child).
        # Threaded into the child loop, which owns the authoritative two-gate
        # activeness decision; the inactive-note below is only for reporting.
        verification = CommandVerification.from_value(args.get("verification"))
        # Per-spawn workspace, resolved BEFORE anything is registered so a
        # refusal costs nothing. An absent ``project`` inherits the parent's
        # directory + instructions; an unresolvable one refuses with the
        # catalog's own reason, and NEVER falls back.
        try:
            workspace = self._child_workspace(args.get("project"))
        except ProjectResolutionError as exc:
            return self._refusal(
                code="unresolvable_project",
                reason=f"sub-agent not spawned — {exc.message}",
            )
        model_override = args.get("model")

        # agent_type: look up registered agent definition and apply its config.
        agent_type = args.get("agent_type")
        if agent_type and self._agent_registry:
            agent_def = self._agent_registry.get(
                agent_type, self._session_capabilities
            )
            if agent_def is None:
                return self._refusal(
                    code="unknown_agent_type",
                    reason=f"Unknown agent type '{agent_type}'",
                )
            # Prepend agent system prompt to task, running the plugin-generic
            # body substitution first so ``${SESSION_ID}``,
            # ``${CLAUDE_PLUGIN_ROOT}``, and bash-style ``${VAR:-default}``
            # expansions resolve before the body hits the child LLM.
            body_subs = {
                "SESSION_ID": self.session_id or "",
                "CLAUDE_PLUGIN_ROOT": agent_def.plugin_root,
            }
            rendered_body = self.substitute_agent_body(agent_def.body, body_subs)
            task_desc = registry_prompts.render(
                "spawn.task_body", body=rendered_body, task=task_desc
            )
            # Apply agent's tool scope if specified and not overridden by caller.
            # ``is not None`` so an AgentDef declaring ``tools: []`` (grant
            # nothing) is applied rather than skipped as if it declared nothing.
            if agent_def.allowed_tools is not None and "allowed_tools" not in args:
                args["allowed_tools"] = agent_def.allowed_tools
            if agent_def.denied_tools and "denied_tools" not in args:
                args["denied_tools"] = agent_def.denied_tools
            # Apply agent's model if specified.
            # Registered agent types with a configured model are authoritative.
            # LLM's model arg on spawn_agent is ignored — config has already made this decision.
            # (Ad-hoc spawns without agent_type continue to honor the LLM's model arg.)
            if agent_def.model:
                model_override = agent_def.model

        registry = self._agent_context.registry

        # A declared ``model_tier`` is the THIRD, LOWEST-priority model source
        # — applied only when neither an agent_type's configured model nor the
        # caller's explicit ``model`` arg already set one (explicit always wins).
        if contract.model_tier is not None:
            raw_tier_map = get_config_value("agent", "model_tiers", default={})
            tier_map = raw_tier_map if isinstance(raw_tier_map, dict) else {}
            allowed_models = self._coerce_list(
                get_config_value("agent", "allowed_models", default=[])
            )
            tier_override = contract.resolve_model_override(
                model_override, tier_map, allowed_models or None
            )
            if tier_override is not None:
                model_override = tier_override

        # 1. Resolve and validate model. A model the gateway will refuse kills
        # the child at step 0 while the parent runs on healthily, and an
        # AgentDef-pinned model is the common way in: it overrides the caller
        # entirely, so nothing upstream ever checked it against what this
        # deployment can actually serve. Under the curated-foundry assumption
        # the declared allowlist IS that check.
        #
        # Falling back beats refusing. The parent's own model is proven — it is
        # what this agent is running on right now — so a child whose declared
        # model is unavailable runs on a model that works instead of dying
        # before its first step. Surfaced in the response + start event, never
        # silently swapped: a caller that pinned a model is owed the fact that
        # it did not get it.
        model_fallback_note: str | None = None
        resolved_model = self._resolve_model(model_override)
        if resolved_model.startswith("ERROR:"):
            parent_model = self._agent_context.model_name
            if not parent_model:
                return self._refusal(
                    code="model_unavailable",
                    reason=resolved_model[len("ERROR: ") :],
                )
            model_fallback_note = (
                f"requested model unavailable ({resolved_model[len('ERROR: '):]}); "
                f"running on the parent's model '{parent_model}'"
            )
            logging.warning("Sub-agent model fallback: {}", model_fallback_note)
            resolved_model = parent_model

        # 2. No admission gate here any more. A depth>=1 spawn drives its child
        # INLINE, so its own slot covers the child for the whole run: the
        # parent is blocked, not running, and only RUNNING consumes a slot.
        # Taking a second slot for a subtree that adds no concurrency is what
        # would deadlock a nested fan-out — every depth-1 parent holding one
        # slot while waiting for another. Root spawns are the ones that genuinely
        # add concurrency, and they go through the queue below.

        child_ctx: AgentContext | None = None
        handle: AgentHandle | None = None
        tq = None  # Initialized early so error handlers can read partial results
        # Set once the root path hands this child to the scheduler + background
        # lifecycle manager: they own its registration, its terminal event and
        # its slot from that point, so the finally must not settle it here.
        lifecycle_owned = False
        try:
            # 3. Create child context. The effective capability_mode is the
            # narrower of the request and this agent's own ceiling; the
            # effective ``atomic`` bit is likewise this contract's OR'd
            # with whatever the parent already carries (see ``child()``).
            child_ctx = self._agent_context.child(
                model_name=resolved_model,
                capability_mode=requested_capability_mode,
                workspace_mode=requested_workspace_mode,
                atomic=contract.atomic,
            )

            # NO-SILENT-DROP: a supplied verification spec whose gate is inert
            # for THIS child (master switch off, or a below-execute
            # capability_mode) is surfaced verbatim in the spawn response +
            # event, never dropped to an invisible null. Non-``None`` only when
            # a spec was supplied AND it will not run — the child loop owns the
            # authoritative decision via the SAME predicate.
            verification_inactive_note: str | None = None
            if verification is not None:
                verification_inactive_note = CommandVerification.inactive_reason(
                    enabled=bool(
                        get_config_value("agent", "verification_enabled", default=False)
                    ),
                    capability_mode=child_ctx.capability_mode,
                )

            # 4. Register in registry.
            # Agent starts as "submitted", transitions to "running"
            handle = AgentHandle(
                agent_id=child_ctx.agent_id,
                parent_id=child_ctx.parent_id,
                depth=child_ctx.depth,
                model_name=child_ctx.model_name,
                task_description=task_desc[:200],
                # The AgentDef name this child was spawned as (``None`` for an
                # ad-hoc spawn) — carried on the handle so every lifecycle event
                # (incl. the background ``stop`` in ``_run_child_lifecycle``,
                # which only holds the handle) can stamp the lane identity.
                agent_type=agent_type if isinstance(agent_type, str) else None,
                status="submitted",
                message_queue=child_ctx.message_queue,
                contract=contract,
            )
            await registry.register(handle)
            # The ``start`` event, the start hook and the spawn attestation all
            # fire at DISPATCH (``_mark_dispatched``), not here. Registration is
            # only acceptance, and a capacity-deferred child may be cancelled
            # or fail to launch before it ever runs — emitting a start for it
            # would leave a span no ``stop`` ever closes, which is precisely
            # the "a start with no stop pins that agent live forever" trap.

            # 5. Filter tool specs (the "filter before binding" pattern). The
            # capability_mode is the child's EFFECTIVE (already-narrowed) mode.
            child_specs = self._filter_tool_specs(
                args, capability_mode=child_ctx.capability_mode
            )

            # 6. Resolve child tool scope + opt-in bounded-retry policy.
            # Privilege attenuation — sub-agents
            # inherit parent's approval policy (not None, which blocks all writes).
            # Three-state, and both collapses matter: ``_coerce_list`` cannot
            # tell absent from empty (it returns ``[]`` for either), so the
            # ``is None`` test happens BEFORE coercion. A parent that spawns a
            # child with ``allowed_tools: []`` means zero tools; reading that as
            # "unrestricted" handed the child the parent's entire spec set.
            _requested_allowed = args.get("allowed_tools")
            child_allowed_tools = (
                None
                if _requested_allowed is None
                else self._coerce_list(_requested_allowed)
            )
            retry = RetryPolicy.from_value(args.get("retry"))

            # Root agent delegates non-blockingly
            # to maintain continuous monitoring capability (epoll model).
            if self._agent_context.depth == 0:
                # Non-blocking: the lifecycle manager drives the child (with
                # bounded retry) and stores the result in the background. Bound
                # to non-optional locals so the launcher closes over values the
                # type checker can see are present.
                launch_ctx, launch_handle = child_ctx, handle
                child_id = launch_ctx.agent_id
                # Captured HERE, not inside ``_launch``: a deferred unit is
                # dispatched by whichever task frees a slot, so by the time the
                # launcher runs the spawning span is neither ambient nor even in
                # the same task. The link is what lets the child's first span
                # name a parent that was actually exported.
                spawn_link = LangfuseTraceLink.capture()

                async def _launch() -> None:
                    """Start this child on the slot the scheduler just handed it."""
                    self._mark_dispatched(
                        launch_ctx,
                        launch_handle,
                        task_desc=task_desc,
                        agent_type=agent_type if isinstance(agent_type, str) else None,
                        model=resolved_model,
                        contract=contract,
                        verification_note=verification_inactive_note,
                        model_fallback_note=model_fallback_note,
                    )
                    with langfuse_child_task_link(spawn_link):
                        lm_task = asyncio.create_task(
                            self._run_child_lifecycle(
                                launch_ctx,
                                launch_handle,
                                child_specs,
                                child_allowed_tools,
                                task_desc,
                                retry,
                                summary_kind,
                                contract,
                                verification,
                                workspace,
                            )
                        )
                    self._lifecycle_tasks.append(lm_task)

                unit = SpawnUnit(
                    spawn=ScheduledSpawn(
                        agent_id=child_id,
                        batch_index=batch_index,
                        priority="critical" if blocking_admit else "normal",
                        enqueued_at=time.monotonic(),
                    ),
                    launch=_launch,
                )
                # Scheduler + lifecycle manager own this child from here.
                lifecycle_owned = True
                # ``submitted`` whether it starts now or waits for a slot: it
                # is accepted and registered either way, and A2A's SUBMITTED is
                # exactly "acknowledged and accepted". The launcher flips it to
                # ``running`` when a slot is actually handed over.
                handle.status = "submitted"
                started_now = False
                if batch is not None:
                    batch.append(unit)
                else:
                    started_now = await registry.accept(unit)
                submitted_body: dict[str, Any] = {
                    "agent_id": child_id,
                    "status": "submitted",
                    "task": task_desc[:200],
                    "message": (
                        "Agent spawned. Use check_agents to monitor "
                        "progress and collect results."
                    )
                    if started_now
                    else (
                        "Agent accepted; the fleet is at its concurrency limit, "
                        "so it starts automatically as soon as a slot frees. "
                        "Use check_agents to monitor it."
                    ),
                }
                # NO-SILENT-DROP: surface a supplied-but-inert verification so the
                # caller never reads a dropped gate as an invisible null. Same
                # contract for a model this deployment cannot serve.
                if verification_inactive_note is not None:
                    submitted_body["verification"] = verification_inactive_note
                if model_fallback_note is not None:
                    submitted_body["model_fallback"] = model_fallback_note
                return _SpawnOutcome(
                    content=json.dumps(submitted_body),
                    agent_id=child_id,
                    status="submitted",
                )

            # A nested spawn dispatches immediately — it runs INSIDE the
            # parent's slot, so there is nothing to wait for and nothing to
            # defer.
            self._mark_dispatched(
                child_ctx,
                handle,
                task_desc=task_desc,
                agent_type=agent_type if isinstance(agent_type, str) else None,
                model=resolved_model,
                contract=contract,
                verification_note=verification_inactive_note,
                model_fallback_note=model_fallback_note,
            )

            # Blocking: current behavior for non-root agents. The retry driver
            # re-delegates the SAME task on a retryable terminal failure (a
            # single attempt when retry is off), raising the last error once the
            # attempt budget is spent.
            tq, state = await self._drive_with_retry(
                child_ctx=child_ctx,
                handle=handle,
                child_specs=child_specs,
                child_allowed_tools=child_allowed_tools,
                task_desc=task_desc,
                retry=retry,
                contract=contract,
                verification=verification,
                workspace=workspace,
            )

            # 7. Settle, with the terminal the child actually reached — the
            # loop returning rather than raising is not evidence of success.
            settled = _ChildSettled(
                summary_kind=summary_kind,
                tq=tq,
                state=state,
                summary=self._child_summary(tq, state),
            )
            result = await self._settle_child(child_ctx, handle, settled)
            result_body = asdict(result)
            if verification_inactive_note is not None:
                result_body["verification"] = verification_inactive_note
            if model_fallback_note is not None:
                result_body["model_fallback"] = model_fallback_note
            return _SpawnOutcome(
                content=json.dumps(result_body),
                agent_id=child_ctx.agent_id,
                # The terminal this settle REACHED, not the result's wider
                # declared type — see ``_SpawnOutcome``. Same value either way.
                status=settled.status,
            )

        except AgentDepthExceeded as exc:
            # child() raises this before `handle` is built or registered, so
            # there is nothing registered to mark done — just surface the result.
            result = AgentResult(
                content=f"Depth exceeded: {exc}",
                status="cannot_solve",
                steps_used=0,
                warnings=[str(exc)],
                summary_kind=summary_kind,
            )
            # A depth refusal answers with a real ``AgentResult`` rather than
            # the bare refusal envelope, so its typed cause rides INSIDE that
            # body — the parent still gets one shape to parse, and the cause
            # is typed rather than only inferable from the prose.
            depth_body = asdict(result)
            depth_body["code"] = "depth_exceeded"
            return _SpawnOutcome(
                content=json.dumps(depth_body),
                agent_id=None,
                status="cannot_solve",
                code="depth_exceeded",
            )

        except asyncio.CancelledError:
            await self._settle_child(
                child_ctx,
                handle,
                _ChildCancelled(
                    summary_kind=summary_kind,
                    partial=(tq.task_result or "") if tq is not None else "",
                ),
            )
            raise  # Re-raise for TaskGroup propagation.

        except Exception as exc:
            logging.error("Sub-agent failed: {}", exc)
            result = await self._settle_child(
                child_ctx,
                handle,
                _ChildFailed.of(
                    exc,
                    child_ctx=child_ctx,
                    summary_kind=summary_kind,
                    task_desc=task_desc,
                    partial=(tq.task_result or "")[:500] if tq is not None else "",
                    fallback_model=resolved_model,
                ),
            )
            return _SpawnOutcome(
                content=json.dumps(asdict(result)),
                agent_id=child_ctx.agent_id if child_ctx else None,
                status="failed",
            )

        finally:
            # Nothing to release: a depth>=1 child ran on its parent's slot,
            # and a root child's slot belongs to the scheduler, which hands it
            # to the lifecycle manager and takes it back exactly once on settle.
            if child_ctx is not None and not lifecycle_owned:
                # Cancel any children spawned by this sub-agent, settling each
                # one in the transcript — see _cascade_cancel_children.
                await self._cascade_cancel_children(child_ctx)

                await registry.unregister(child_ctx.agent_id)

    # ------------------------------------------------------------------
    # Terminal settle — the ONE way a child reaches its terminal state
    # ------------------------------------------------------------------

    async def _settle_child(
        self,
        child_ctx: AgentContext | None,
        handle: AgentHandle | None,
        terminal: _ChildTerminal,
    ) -> AgentResult:
        """Settle ONE child into its terminal — the ONE sequence, for every end.

        Registry mark-done, the stop hook, the single terminal ``stop`` event
        and the terminal attestation happen here, in this order, however the
        child ended. Everything that DIFFERS between a completion, a
        cancellation and a failure is data *terminal* owns, so there is no
        branch on the kind here.

        Both spawn paths ran their own copy of this sequence — the blocking
        nested spawn in :meth:`_spawn_one` and the background manager in
        :meth:`_run_child_lifecycle` — and the copies had drifted. One owner is
        what makes a change to how a child settles reach both callers.

        *child_ctx* and *handle* are optional because a failure can land before
        either exists: the settle then performs the parts it still can rather
        than raising a second exception over the first. The attestation hash is
        ``""`` in that case, exactly as it is when no chain is wired.
        """
        registry = self._agent_context.registry
        if child_ctx is not None:
            await registry.mark_done(
                child_ctx.agent_id,
                terminal.status,
                error=terminal.registry_error(child_ctx, handle),
            )
        if handle is not None:
            self._hook_manager.run_on_agent_stop(handle)
        attestation_hash = ""
        if child_ctx is not None and handle is not None:
            self._emit_terminal_stop(
                child_ctx.event_logger,
                handle,
                terminal.stop_detail,
                summary=terminal.stop_summary,
                summary_kind=terminal.stop_summary_kind,
            )
            attestation_hash = self._record_terminal_attestation(
                child_ctx,
                handle,
                terminal_state=terminal.status,
                summary_kind=terminal.summary_kind,
                summary_text=terminal.attested_summary,
                done_reason=terminal.attested_done_reason,
            )
        return terminal.build_result(handle, attestation_hash)

    @staticmethod
    def _child_summary(tq: TaskQueue, state: OrchestrationState) -> str:
        """Return the child's Communication Unit, falling back to its summary.

        The heaviest children reach their parent with an EMPTY ``task_result``:
        their work landed as store side-effects and their final turn produced no
        text, so everything they learned was discarded at the collection seam.
        A compaction summary is the one compressed record of that work the child
        already produced, so it is used rather than handing the parent nothing.

        This narrows the hole; it does not close it. A child that neither
        answered nor compacted still has no CU, which needs a forced closing
        summary turn inside the child loop.
        """
        if isinstance(tq.task_result, str) and tq.task_result.strip():
            return tq.task_result
        if isinstance(state.summary, str) and state.summary.strip():
            return state.summary
        return ""

    @staticmethod
    def _coerce_timeout(value: object, default: float = 30.0) -> float:
        """Coerce a model-supplied wait timeout, degrading to *default*.

        ``tool_input`` is authored by the model, so this is a trust boundary and
        the read is where it has to be validated. A bare ``float(...)`` over an
        untyped value raises ``ValueError``/``TypeError`` straight out of a
        monitoring call — a malformed argument would kill the run instead of
        merely being ignored. Total, mirroring ``RetryPolicy.from_value``.

        ``bool`` is excluded explicitly: it is an ``int`` subclass, so
        ``timeout: true`` would otherwise silently mean one second.
        """
        if isinstance(value, bool) or value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return default
        return default

    @staticmethod
    def _coerce_list(value: object) -> list[str]:
        """Coerce a loose config or tool-argument value to a list of strings.

        Sibling of :meth:`_coerce_timeout` and total for the same reason: it
        reads BOTH an operator's config value (``allowed_models``, which a
        deployment may spell as a comma-separated string) and a model-supplied
        ``allowed_tools``/``denied_tools``, so neither may raise into a spawn.

        It cannot tell ABSENT from EMPTY — both answer ``[]`` — which is why
        every three-state ``allowed_tools`` read tests ``is None`` BEFORE
        coercing.
        """
        if isinstance(value, list):
            return [str(v) for v in value if v]
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        return []

    @staticmethod
    def substitute_agent_body(
        body: str,
        subs: Mapping[str, str],
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Render an agent's body with plugin-generic variable substitution.

        Three passes, in order:

        1. ``${KEY}`` literal substitution from *subs*. Core passes
           ``SESSION_ID`` and ``CLAUDE_PLUGIN_ROOT``; plugins author their
           prompts against these names.
        2. Bash-style ``${VAR:-default}`` — if ``VAR`` is unset in *env*,
           the text expands to ``default``. If ``VAR`` is set, it expands
           to the env value. This keeps plugin prompts self-documenting
           (operator override path is obvious in the source).
        3. Plain ``$VAR`` expansion as a final pass, matching
           :func:`os.path.expandvars` semantics. Unset variables remain
           literal so authors can spot typos at glance.

        A ``staticmethod`` on this class rather than a loose module function:
        this tool is the only production caller, and *subs*/*env* arrive as
        ARGS so the renderer stays testable with a fake environment. The
        module-level ``substitute_agent_body`` name is a thin alias over it, so
        the import path plugins and tests already use is unchanged.
        """
        if env is None:
            env = os.environ
        # Pass 1: direct substitutions.
        for key, value in subs.items():
            body = body.replace(f"${{{key}}}", value)

        # Pass 2: bash-style ${VAR:-default}. Read from env; fall back to default.
        def _bash_default(match: re.Match[str]) -> str:
            var_name, default = match.group(1), match.group(2)
            return env.get(var_name, default)

        body = _BASH_DEFAULT_RE.sub(_bash_default, body)

        # Pass 3: plain ``$VAR`` expansion for anything still referencing env.
        # Matches ``os.path.expandvars`` semantics without touching the real
        # ``os.environ`` when a test supplies a fake *env* mapping.
        def _plain_var(match: re.Match[str]) -> str:
            var_name = match.group(1)
            return env.get(var_name, match.group(0))

        return re.sub(r"\$(\w+)", _plain_var, body)

    async def has_live_owned_runs(self) -> bool:
        """True while any agent this session owns is still non-terminal.

        The ownership index behind the promise-as-completion gate: a clean
        terminal declared while owned work is live is a promise, not a
        completion. Exposed here because the hypervisor is the only thing that
        knows, and the seam that must ask is the one accepting a completion
        claim.
        """
        try:
            agents = await self._agent_context.registry.list_all()
        except Exception:  # pragma: no cover - defensive; never fail a run here
            return False
        return any(handle.status in ACTIVE_STATUSES for handle in agents)

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def _resolve_model(self, model_override: object) -> str:
        """Resolve model for the child agent.

        Returns model name, or ``"ERROR: ..."`` string on validation failure.
        """
        allowed_models = self._coerce_list(
            get_config_value("agent", "allowed_models", default=[])
        )
        default_sub = str(get_config_value("agent", "default_sub_model", default="") or "").strip()

        if model_override and isinstance(model_override, str):
            model = model_override.strip()
            if allowed_models and model not in allowed_models:
                return (
                    f"ERROR: Model '{model}' not in allowed_models. "
                    f"Available: {', '.join(allowed_models)}"
                )
            return model

        if default_sub:
            return default_sub

        return self._agent_context.model_name

    # ------------------------------------------------------------------
    # Tool spec filtering (the "filter before binding" pattern)
    # ------------------------------------------------------------------

    def _filter_tool_specs(
        self, args: dict[str, Any], *, capability_mode: str = "all"
    ) -> list[ToolSpec]:
        """Filter tool specs for a child agent.

        Filtering starts from the PARENT's effective set (the registry only when
        this tool was never stamped), so the child can only ever narrow it —
        the same monotone containment ``capability_mode`` already enforces at
        :meth:`AgentContext.child`, applied to the spec set itself.

        Denied tools are removed from the child's ``bind_tools()`` list —
        the child LLM never sees them. ``capability_mode`` is the
        child's effective privilege ceiling, applied as a coarse pre-filter
        LAYERED UNDER the allow/deny gates (it only removes more, never adds).
        """
        parent_specs = self.parent_tool_specs
        if parent_specs is None:
            parent_specs = self._tool_registry.list_specs()
        # ``allowed_tools`` is three-state — see ``_spawn_one``'s resolution of
        # ``child_allowed_tools``. Absent stays unrestricted; an empty list is
        # forwarded as an empty list so ``filter_specs`` grants nothing.
        requested_allowed = args.get("allowed_tools")
        return filter_specs(
            parent_specs,
            allowed=(
                None if requested_allowed is None else self._coerce_list(requested_allowed)
            ),
            denied=self._coerce_list(args.get("denied_tools") or []),
            capability_mode=capability_mode,
        )

    # ------------------------------------------------------------------
    # Agent management handlers (root-only)
    # ------------------------------------------------------------------

    async def handle_check_agents(self, action_step: ActionStep) -> MockSpeaker:
        """Return agent tree state with completed results and progress.

        Emits a JSON payload with ``kind: "agent_tree"``. The ``text`` field
        carries the rendered ASCII tree the LLM consumes; the ``agents`` list
        is the structured snapshot the console uses to render CheckAgentsCard.
        """
        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        wait = bool(args.get("wait", False))
        timeout = self._coerce_timeout(args.get("timeout"))
        registry = self._agent_context.registry
        parent_id = self._agent_context.agent_id

        if wait:
            running = await registry.collect_running(parent_id)
            if running:
                waiters = [asyncio.create_task(h.done_event.wait()) for h in running]
                # asyncio.wait() does not raise on timeout — it returns
                # (done, pending) with pending non-empty when the deadline hits.
                _done, pending = await asyncio.wait(
                    waiters,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()

        tree = await registry.render_agent_tree(
            exclude_agent_id=self._agent_context.agent_id,
        )
        completed = await registry.collect_completed(parent_id)
        running = await registry.collect_running(parent_id)

        parts: list[str] = []
        if tree:
            parts.append(f"Agent tree:\n{tree}")

        if completed:
            parts.append("\nCompleted results:")
            for h in completed:
                r = h.result
                if r:
                    parts.append(f"  [{h.agent_id[:8]}] {r.status}: {r.summary or r.content[:300]}")

        if running:
            parts.append(f"\n{len(running)} agent(s) still running.")
        elif not completed:
            parts.append("No agents spawned.")

        text = "\n".join(parts) or "No agents."

        agents_payload: list[dict[str, Any]] = []
        for h in await registry.list_visible(exclude_agent_id=self._agent_context.agent_id):
            entry: dict[str, Any] = {
                "id": h.agent_id,
                "parent_id": h.parent_id,
                "depth": h.depth,
                "task": h.task_description,
                "status": h.status,
                "steps_completed": h.steps_completed,
                "last_tool_id": h.last_tool_id,
                "progress_note": h.progress_note,
                "compaction_count": h.compaction_count,
                "attempts": h.attempts,  # Retry provenance for the console
                "result": (
                    {
                        "status": h.result.status,
                        "summary": h.result.summary,
                        "content": h.result.content,
                    }
                    if h.result is not None
                    else None
                ),
            }
            # Additive, ONLY when a real bound was declared, so a
            # contract-less child's payload carries no key. Carries LIVE
            # progress (steps/elapsed) alongside the bounded-scalar
            # ``snapshot()`` shape the attestation record reuses.
            if h.contract.enabled:
                entry["contract"] = {
                    **h.contract.snapshot(),
                    "steps_completed": h.steps_completed,
                    "elapsed_s": time.monotonic() - h.started_at,
                }
            agents_payload.append(entry)

        payload = {
            "kind": "agent_tree",
            "text": text,
            "agents": agents_payload,
            "parent_id": parent_id,
            "wait": wait,
        }
        return MockSpeaker(content=json.dumps(payload))

    async def handle_steer_agent(self, action_step: ActionStep) -> MockSpeaker:
        """Send a steering message to or cancel a running agent."""
        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        agent_id = str(args.get("agent_id", ""))
        action = str(args.get("action", ""))
        message = str(args.get("message", ""))
        registry = self._agent_context.registry

        # Resolve short prefix to full agent_id.
        handle = await registry.get(agent_id)
        if handle is None:
            all_agents = await registry.list_all()
            matches = [h for h in all_agents if h.agent_id.startswith(agent_id)]
            if len(matches) == 1:
                handle = matches[0]
                agent_id = handle.agent_id
            elif len(matches) > 1:
                return MockSpeaker(
                    content=f"ERROR: Ambiguous prefix '{agent_id}' matches {len(matches)} agents.",
                )
            else:
                return MockSpeaker(
                    content=f"ERROR: Agent '{agent_id}' not found.",
                )

        if action == "cancel":
            reason = await registry.cancel_agent(agent_id)
            if reason is None:
                return MockSpeaker(
                    content=f"Agent {agent_id[:8]} cancelled.",
                )
            return MockSpeaker(
                content=f"Agent {agent_id[:8]} cannot cancel: {reason}",
            )
        if action == "message":
            if not message:
                return MockSpeaker(
                    content="ERROR: 'message' is required when action='message'.",
                )
            reason = await registry.send_message(
                agent_id,
                f"[From parent] {message}",
            )
            if reason is None:
                return MockSpeaker(content="Message sent.")
            return MockSpeaker(content=f"Message failed: {reason}")

        return MockSpeaker(
            content=f"ERROR: Unknown action '{action}'. Use 'message' or 'cancel'.",
        )

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_event(
        self,
        ctx: AgentContext,
        action: str,
        detail: str,
        handle: AgentHandle | None = None,
        summary: str | None = None,
        summary_kind: SummaryKind | None = None,
        verification: str | None = None,
        model_fallback: str | None = None,
    ) -> None:
        """Emit a sub_agent lifecycle event.

        ``summary`` (additive, set only on the terminal ``stop``) carries the
        child's compressed result — the Communication Unit a downstream consumer
        can project as the sub-agent's actual response (e.g. the agentic-search
        trace's per-lane evidence block, where the lifecycle ``detail`` is just
        the ``done_reason``). Omitted on every other phase, so a consumer
        reading only the base keys is unaffected.
        ``summary_kind`` rides alongside ``summary`` and is
        likewise omitted when it's the untyped ``"generic"`` default, so a
        consumer that never declared a kind sees no new key.
        ``verification`` (additive, set only on ``start`` when a supplied spec's
        gate is inert) is the no-silent-drop note — omitted otherwise, so a
        spawn with no verification, or with an active one, carries no key.
        ``model_fallback`` is the same contract for a declared model this
        deployment cannot serve: present only when the child was moved off it.
        """
        if ctx.event_logger is None:
            return
        self._write_lifecycle(
            ctx.event_logger,
            action=action,
            agent_id=ctx.agent_id,
            parent_id=ctx.parent_id,
            depth=ctx.depth,
            model=ctx.model_name,
            detail=detail,
            handle=handle,
            summary=summary,
            summary_kind=summary_kind,
            verification=verification,
            model_fallback=model_fallback,
        )

    def _write_lifecycle(
        self,
        logger: Callable[[Event], None],
        *,
        action: str,
        agent_id: str,
        parent_id: str | None,
        depth: int,
        model: str,
        detail: str,
        handle: AgentHandle | None = None,
        summary: str | None = None,
        summary_kind: SummaryKind | None = None,
        verification: str | None = None,
        model_fallback: str | None = None,
    ) -> None:
        """Write ONE ``sub_agent`` lifecycle event — the single payload shape.

        Identity is passed explicitly rather than read off a context because a
        parent settling a child during teardown cascade holds the child's
        HANDLE, not its context. Consoles parse this shape, so every lifecycle
        phase must build it here and nowhere else.
        """
        payload: dict[str, Any] = {
            "action": action,
            "agent_id": agent_id,
            "parent_id": parent_id,
            "depth": depth,
            "model": model,
            "detail": detail,
            "status": handle.status if handle else action,
            "steps_completed": handle.steps_completed if handle else 0,
            "input_tokens": handle.input_tokens if handle else 0,
            "output_tokens": handle.output_tokens if handle else 0,
        }
        # ``agent_type`` (additive) carries the spawned AgentDef name so a
        # consumer can label the lane by its DEFINITION (e.g.
        # ``scg-path-probe``) instead of falling back to the model name —
        # the agentic-search trace projection's lane identity. Read off the
        # handle so the terminal ``stop`` (emitted from the background
        # lifecycle manager, which holds only the handle) carries it too.
        # Omitted for an ad-hoc spawn (no ``agent_type``) so a consumer
        # reading only the base keys is untouched.
        if handle is not None and handle.agent_type:
            payload["agent_type"] = handle.agent_type
        if summary:
            payload["summary"] = summary
        if summary_kind and summary_kind != "generic":
            payload["summary_kind"] = summary_kind
        if verification:
            payload["verification"] = verification
        if model_fallback:
            payload["model_fallback"] = model_fallback
        event: Event = {"type": "sub_agent", "payload": payload}
        logger(event)

    def _emit_terminal_stop(
        self,
        logger: Callable[[Event], None] | None,
        handle: AgentHandle,
        detail: str,
        *,
        summary: str | None = None,
        summary_kind: SummaryKind | None = None,
    ) -> None:
        """Write this agent's ONE terminal ``stop``, at most once.

        Every way an agent can settle — success, failure, cancellation, or a
        parent's teardown cascade — routes through here, because a consumer
        derives liveness from the last payload it sees and a start with no stop
        pins that agent live forever. Several of those paths can fire for the
        SAME agent (a cascade cancel lands, then the cancelled child's own
        handler unwinds), so the at-most-once guarantee is latched on the handle
        rather than assumed from the paths being mutually exclusive.

        Called AFTER the registry has been marked done, so ``handle.status``
        already carries the terminal state the payload reports.
        """
        if handle.terminal_emitted:
            return
        handle.terminal_emitted = True
        if logger is None:
            return
        self._write_lifecycle(
            logger,
            action="stop",
            agent_id=handle.agent_id,
            parent_id=handle.parent_id,
            depth=handle.depth,
            model=handle.model_name,
            detail=detail,
            handle=handle,
            summary=summary,
            summary_kind=summary_kind,
        )

    async def _cascade_cancel_children(self, ctx: AgentContext) -> None:
        """Cancel this agent's own children and settle each one in the log.

        A child of a settling agent has no one left to drive it, and one still
        at ``submitted`` never had a loop task whose cancellation could raise
        into its own handler — so the terminal is written HERE.

        The skip condition is the emission latch, NOT the child's status: a
        terminal status is no evidence a terminal EVENT was ever written. Other
        teardown paths (the child loop's own end-of-run cascade, the
        hypervisor's shutdown force-mark) settle handles in memory only, and
        they run first — so a child arriving here already marked ``cancelled``
        is precisely the one whose span would otherwise stay open forever.

        ``mark_done`` follows the cancel so the emitted payload always reports a
        terminal status: ``cancel_agent`` is a no-op for a child that never got
        an asyncio task, which would otherwise emit a ``stop`` still reading
        ``submitted``.
        """
        registry = self._agent_context.registry
        for child in await registry.list_children(ctx.agent_id):
            if child.terminal_emitted:
                continue
            if child.status in ACTIVE_STATUSES:
                await registry.cancel_agent(child.agent_id)
                await registry.mark_done(child.agent_id, "cancelled")
                detail = "parent agent settled; child cancelled with it"
            else:
                # Already terminal in memory but never written: report how it
                # actually settled rather than claiming this cascade ended it.
                detail = f"settled as {child.status} with no terminal event recorded"
            self._emit_terminal_stop(ctx.event_logger, child, detail)

    # ------------------------------------------------------------------
    # Attestation provenance
    # ------------------------------------------------------------------

    def _record_spawn_attestation(
        self,
        child_ctx: AgentContext,
        *,
        agent_type: str | None,
        model: str,
        capability_mode: str,
        contract: DelegationContract,
    ) -> str:
        """Best-effort spawn provenance record for ``child_ctx``.

        ``""`` when no ``AttestationChain`` is wired for this session (the
        common case today — the feature is orchestrator-injected and gated on
        config) or the chain's own append failed; never raises.
        """
        chain = getattr(child_ctx.registry, "attestation", None)
        if chain is None:
            return ""
        return chain.record_spawn(
            child_ctx.event_logger,
            agent_id=child_ctx.agent_id,
            parent_id=child_ctx.parent_id,
            depth=child_ctx.depth,
            agent_type=agent_type,
            model=model,
            capability_mode=capability_mode,
            contract=contract,
        )

    def _record_terminal_attestation(
        self,
        child_ctx: AgentContext,
        handle: AgentHandle,
        *,
        terminal_state: AgentStatus,
        summary_kind: SummaryKind,
        summary_text: str,
        done_reason: str | None,
    ) -> str:
        """Best-effort terminal provenance record for ``child_ctx``.

        Mirrors :meth:`_record_spawn_attestation`'s no-chain/no-raise contract.
        ``summary_text`` is hashed inside the chain, never persisted verbatim.
        """
        chain = getattr(child_ctx.registry, "attestation", None)
        if chain is None:
            return ""
        return chain.record_terminal(
            child_ctx.event_logger,
            agent_id=child_ctx.agent_id,
            parent_id=child_ctx.parent_id,
            depth=child_ctx.depth,
            terminal_state=terminal_state,
            spawn_hash=handle.attestation_spawn_hash,
            attempts=handle.attempts,
            steps_completed=handle.steps_completed,
            input_tokens=handle.input_tokens,
            output_tokens=handle.output_tokens,
            summary_kind=summary_kind,
            summary_text=summary_text,
            done_reason=done_reason,
        )

    # ------------------------------------------------------------------
    # Bounded retry driver
    # ------------------------------------------------------------------

    def _build_child_loop(
        self,
        child_ctx: AgentContext,
        child_allowed_tools: list[str] | None,
        contract: DelegationContract | None = None,
        verification: CommandVerification | None = None,
        workspace: ChildWorkspace | None = None,
    ) -> Any:
        """Construct a fresh child ``ToolUseLoop`` for one attempt.

        A fresh loop per attempt means each retry gets its own
        ``RetryStrategy`` (the model-fallback ladder) — so model-level recovery
        is reused, never reinvented at this layer. ``contract``,
        ``verification`` and ``workspace`` ride along unchanged across retries —
        they are the SPAWNER's declared bound/check/directory for this child,
        not per-attempt state. All three default to off/absent so existing
        callers are unaffected; the child loop owns the authoritative two-gate
        decision on whether verification runs.
        """
        # Import here to avoid a circular import at module load time.
        from mewbo_core.loop.tool_use_loop import ToolUseLoop

        # An absent workspace is a caller that resolved none, which inherits
        # this agent's current directory — the pre-``project`` behaviour.
        ws = workspace or ChildWorkspace(
            cwd=self._cwd, project_instructions=self._project_instructions
        )
        return ToolUseLoop(
            agent_context=child_ctx,
            tool_registry=self._tool_registry,
            permission_policy=self._permission_policy,
            approval_callback=self._approval_callback,
            hook_manager=self._hook_manager,
            # Forwarded unchanged — the SAME object as the parent, never
            # re-loaded from disk or config. A child inherits its parent's
            # plane exactly as it inherits ``hook_manager``; there is no path
            # by which a descendant runs under a weaker (or no) plane than the
            # session that spawned it.
            safety_plane=self._safety_plane,
            project_instructions=ws.project_instructions,
            user_instructions=self._user_instructions,
            session_tool_registry=self._session_tool_registry,
            allowed_tools=child_allowed_tools,
            # A spawned sub-agent's allowlist is AUTHORITATIVE — its specs are
            # already strictly filtered (``_filter_tool_specs``, no built-in
            # exemption), and the spawn_agent gate must honour it so a leaf
            # scoped without spawn_agent cannot recurse into copies of itself.
            strict_tool_scope=True,
            cwd=ws.cwd,
            session_id=self.session_id,
            session_capabilities=self._session_capabilities,
            enable_skills=self._enable_skills,
            contract=contract or DelegationContract(),
            verification=verification,
        )

    async def _drive_with_retry(
        self,
        *,
        child_ctx: AgentContext,
        handle: AgentHandle,
        child_specs: list[ToolSpec],
        child_allowed_tools: list[str] | None,
        task_desc: str,
        retry: RetryPolicy,
        contract: DelegationContract | None = None,
        verification: CommandVerification | None = None,
        workspace: ChildWorkspace | None = None,
    ) -> tuple[Any, Any]:
        """Run the child loop, re-delegating the SAME task on a retryable failure.

        Returns ``(tq, state)`` from the first attempt that reached a real
        completion. Re-raises the LAST exception once the attempt budget is
        spent or the failure cause is not in ``retry.on`` (default off ⇒
        exactly one attempt).
        ``CancelledError`` is never retried — parent cancellation is terminal
        and bubbles straight up.

        BOTH child-death shapes are retryable: an exception out of the loop, and
        a loop that RETURNS having stopped short (a halt, a spent budget, a
        failed ground-truth check). The second shape is the common one, so
        watching only the first is why this contract had never once fired in
        production. A stopped-short return is still handed back after the
        budget is spent — the caller reports it honestly rather than raising.

        Slot discipline: the one semaphore slot already acquired in
        ``run_async`` is *held across all attempts* — re-admission re-uses that
        slot rather than releasing and racing for a new one, so concurrency stays
        bounded exactly as on the no-retry path. ``handle.attempts`` is bumped per
        attempt so the agent tree / ``check_agents`` surface the re-delegation.
        """
        attempt = 0
        while True:
            attempt += 1
            handle.attempts = attempt
            # Each attempt is a fresh run on the SAME handle/agent_id: reset the
            # transient running state (the prior attempt's loop marked it failed
            # in its own finally) so the tree reflects the live attempt.
            handle.status = "running"
            handle.error = None
            child_loop = self._build_child_loop(
                child_ctx, child_allowed_tools, contract, verification, workspace
            )
            # The child's whole span subtree hangs off whatever is ambient at
            # THIS create_task; a blocking (depth ≥ 1) spawn still sits inside
            # the spawning span here, while the root path already carries the
            # link its launcher bound. Capturing covers both without a branch.
            with langfuse_child_task_link():
                child_task = asyncio.create_task(
                    child_loop.run(task_desc, tool_specs=child_specs, mode=self.parent_mode)
                )
            # Populate asyncio_task so cancel_agent() and 3-phase cleanup target
            # the live attempt.
            handle.asyncio_task = child_task
            try:
                tq, state = await child_task
            except asyncio.CancelledError:
                raise  # parent cancellation is terminal — never retried
            except Exception as exc:  # noqa: BLE001 — child-loop failure is opaque
                cause = RetryPolicy.classify_cause(exc)
                if not retry.should_retry(cause, attempt):
                    raise
                await self._back_off_before_retry(child_ctx, handle, retry, attempt, cause)
                continue

            # A child that stops short RETURNS; it does not raise. Doom-loop
            # halts, spent budgets and failed ground-truth checks are the
            # dominant child-death shapes and every one of them arrives here as
            # an ordinary return — so a policy watching only the exception path
            # could never fire for them, which is why the contract had never
            # fired at all. Bucketed as the generic ``failed`` cause: nothing
            # about a halt names a provider, so ``timeout`` would be a lie.
            # Keyed on ``failed`` rather than "anything but completed": a child
            # that returns having been CANCELLED also stops short of completed,
            # and re-delegating it would restart work somebody deliberately
            # stopped. Cancellation is structurally unretryable on the raising
            # path directly above; a cooperative cancel returns instead of
            # raising, so it needs the same guarantee stated here.
            if state.terminal_status() == "failed" and retry.should_retry(
                "failed", attempt
            ):
                await self._back_off_before_retry(
                    child_ctx, handle, retry, attempt, state.done_reason or "failed"
                )
                continue
            return tq, state

    async def _back_off_before_retry(
        self,
        child_ctx: AgentContext,
        handle: AgentHandle,
        retry: RetryPolicy,
        attempt: int,
        cause: str,
    ) -> None:
        """Announce a re-delegation and sleep its backoff.

        Shared by both retryable paths so a raising failure and a stopped-short
        return are announced identically — a consumer counting re-delegations
        must not have to know which shape produced one.
        """
        delay = retry.backoff_for(attempt)
        self._emit_event(
            child_ctx,
            "retry",
            f"attempt {attempt} {cause}; re-delegating (max {retry.max})",
            handle=handle,
        )
        logging.warning(
            "Sub-agent {} attempt {} failed ({}); retrying in {:.1f}s",
            child_ctx.agent_id[:8],
            attempt,
            cause,
            delay,
        )
        if delay > 0:
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Non-blocking lifecycle manager
    # ------------------------------------------------------------------

    async def _run_child_lifecycle(
        self,
        child_ctx: AgentContext,
        handle: AgentHandle,
        child_specs: list[ToolSpec],
        child_allowed_tools: list[str] | None,
        task_desc: str,
        retry: RetryPolicy,
        summary_kind: SummaryKind,
        contract: DelegationContract | None = None,
        verification: CommandVerification | None = None,
        workspace: ChildWorkspace | None = None,
    ) -> None:
        """Background lifecycle manager for non-blocking child execution.

        Drives the child (with bounded retry), stores the ``AgentResult`` on the
        handle, and notifies the parent via ``send_to_parent``.

        Emits a lifecycle event at each phase transition and stores the CU on
        the handle for async retrieval. ``summary_kind``
        is the caller's declared CU shape, resolved once in
        ``_spawn_one`` and stamped onto every ``AgentResult`` this manager builds.
        """
        registry = self._agent_context.registry
        tq = None
        try:
            tq, state = await self._drive_with_retry(
                child_ctx=child_ctx,
                handle=handle,
                child_specs=child_specs,
                child_allowed_tools=child_allowed_tools,
                task_desc=task_desc,
                retry=retry,
                contract=contract,
                verification=verification,
                workspace=workspace,
            )

            # The child's REAL terminal, not the fact that its loop returned —
            # see ``OrchestrationState.terminal_status``.
            handle.result = await self._settle_child(
                child_ctx,
                handle,
                _ChildSettled(
                    summary_kind=summary_kind,
                    tq=tq,
                    state=state,
                    summary=self._child_summary(tq, state),
                ),
            )

        except asyncio.CancelledError:
            handle.result = await self._settle_child(
                child_ctx,
                handle,
                _ChildCancelled(
                    summary_kind=summary_kind,
                    partial=(tq.task_result or "") if tq is not None else "",
                ),
            )

        except Exception as exc:
            logging.error("Sub-agent lifecycle failed: {}", exc)
            handle.result = await self._settle_child(
                child_ctx,
                handle,
                _ChildFailed.of(
                    exc,
                    child_ctx=child_ctx,
                    summary_kind=summary_kind,
                    task_desc=task_desc,
                    partial=(tq.task_result or "")[:500] if tq is not None else "",
                ),
            )

        finally:
            # Cascade cleanup to children of this child, settling each one in
            # the transcript — see _cascade_cancel_children.
            await self._cascade_cancel_children(child_ctx)

            # Notify parent before releasing the semaphore slot.
            # Result and status are set in the try/except blocks above.
            # The handle stays in the registry so check_agents and
            # render_agent_tree can surface the result; session cleanup()
            # clears it at session end.
            if handle and handle.result:
                notification = (
                    f"[Agent {child_ctx.agent_id[:8]} {handle.result.status}] "
                    f"Task: {task_desc} | "
                    f"{handle.result.summary or handle.result.content[:300]}"
                )
            else:
                _status = handle.status if handle else "unknown"
                notification = f"[Agent {child_ctx.agent_id[:8]} {_status}] Task: {task_desc}"
            await registry.send_to_parent(child_ctx.agent_id, notification)

            # The dispatch pump: this hands the freed slot straight to whatever
            # spawn has been waiting for one, so a queued child starts on the
            # completion path that every settling agent already runs.
            await registry.release()

    async def await_lifecycle_managers(self, timeout: float = 3.0) -> None:
        """Settle every background lifecycle manager before the run tears down.

        Called from ``ToolUseLoop.run()``'s finally block. Collect-or-cancel:
        managers get *timeout* to finish on their own, then the stragglers are
        cancelled — and, crucially, AWAITED.

        Cancelling without awaiting is what leaked children. ``cancel()`` only
        schedules the ``CancelledError``; the handler that marks the child done
        and writes its ONE terminal ``stop`` runs on a later turn of the event
        loop, which never comes if the loop tears down first. The child was then
        settled by nothing in this process — its span stayed open until a boot
        sweep reaped it days later, or forever. Awaiting here is what makes
        settlement in-process rather than next-boot.

        Exceptions are swallowed by ``return_exceptions``: these tasks own their
        own terminal reporting, and a manager that fails while being torn down
        must not take down the run that is already ending.

        **The set to await is not fixed at entry.** Every manager that settles
        hands its slot to a QUEUED child through the dispatch pump, which
        creates a new manager — so this DRAINS in rounds against one deadline
        rather than awaiting a snapshot. Cancelling waiting units up front
        would be the simpler code and the wrong behaviour: it would discard, at
        teardown, exactly the work this scheduler exists to stop discarding.
        Only once the budget is spent are the still-waiting units dropped —
        after that nothing will ever dispatch them, so they must be settled
        rather than left reading as live.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            pending = [t for t in self._lifecycle_tasks if not t.done()]
            remaining = deadline - time.monotonic()
            if not pending or remaining <= 0:
                break
            await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.ALL_COMPLETED,
            )

        # ROOT ONLY. The queue is SESSION-global while this method runs at the
        # end of EVERY agent's loop, root or child — so an unguarded clear here
        # let the first sub-agent to finish discard the root's still-waiting
        # fan-out, which is precisely the loss this scheduler exists to stop.
        # A depth>=1 tool owns nothing in the queue by construction: a nested
        # spawn runs inline on its parent's slot and is never enqueued.
        if self._agent_context.depth == 0:
            await self._agent_context.registry.cancel_pending()

        still_pending = [t for t in self._lifecycle_tasks if not t.done()]
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)
        self._lifecycle_tasks.clear()


# Thin alias, not a second implementation — the renderer lives on the class
# that drives it (see :meth:`SpawnAgentTool.substitute_agent_body`), and this
# keeps the module-level import path plugins and tests already use.
substitute_agent_body = SpawnAgentTool.substitute_agent_body


__all__ = [
    "AgentError",
    "CHECK_AGENTS_SCHEMA",
    "ChildWorkspace",
    "RetryPolicy",
    "SPAWN_AGENT_SCHEMA",
    "SPAWN_AGENTS_SCHEMA",
    "STEER_AGENT_SCHEMA",
    "SpawnAgentTask",
    "SpawnAgentTool",
    "substitute_agent_body",
]
