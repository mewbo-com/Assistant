"""Act-phase launcher — a down-only DI seam for stage 2 of a scoped refresh.

A scoped refresh runs sessionless (clone → scan → delta → finalize). Its stage 2
— rewriting the pages the delta pass flagged stale — needs the opposite: a real
Mewbo session, bound to the live job, driving the page read/write tools. Minting
a session is an app concern (``mewbo_api.wiki.jobs``), and this library must
never import up. So the api registers a concrete launcher at startup
(:meth:`ActLauncher.register`) and ``ScopedRefreshRunner`` drives stage 2
through it — the same inversion as
:class:`~mewbo_graph.scg.search_launcher.SearchLauncher` and
:class:`~mewbo_graph.scg.map_phase.MapPhaseSink`, applied to a session start.

**The seam is START-then-WAIT, and the split is load-bearing.** It is NOT the
async-by-handle shape of ``SearchLauncher``: the caller must not finalize until
stage 2 is over, because a scoped finalize writes ``status="complete"`` plus the
event the SSE stream closes on. But it must not be one blocking call either —
the caller re-stamps its own resume sidecar with the returned session id BEFORE
blocking on :meth:`wait`, so a process that dies inside the wait resumes knowing
not merely that stage 2 was owed but WHICH session was already running. Collapse
the two and a resume starts a second session rewriting the same pages
concurrently with the first.

**Absence is ``None``, everywhere.** No registered launcher means the act tier
is not installed and the refresh finalizes with no stage 2 at all. How a FAILED
run is reported is the caller's policy, not this seam's:
:meth:`wait` hands back an :class:`ActOutcome` whose ``ok`` says whether stage 2
did what it was asked, and the runner decides what a failure costs the job.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

if TYPE_CHECKING:  # pragma: no cover — typing only
    from mewbo_graph.wiki.types import WizardSubmission

# A session that has not settled yet. ``idle`` is deliberately NOT here: a
# session showing idle with no live run never started one, which is an outcome,
# not a wait state — the caller must not block on it forever.
IN_FLIGHT_STATUSES: frozenset[str] = frozenset({"running", "awaiting_approval"})


@dataclass(frozen=True)
class ActOutcome:
    """How stage 2 ended, as the refresh runner needs to read it.

    ``status`` is the backing session's derived status (``mewbo_core``'s
    ``SessionStatus``) or the literal ``"timeout"`` when the wait deadline
    expired with the session still in flight — a distinct answer from any status
    the session itself can carry, because "we stopped watching" is not "it
    stopped running".
    """

    session_id: str
    status: str
    error: str | None = None

    #: The one status that means the act phase did what it was asked to do.
    #: ``unmet_goal``/``incomplete`` are honest failures, not partial successes.
    OK_STATUSES: ClassVar[frozenset[str]] = frozenset({"completed"})

    @property
    def ok(self) -> bool:
        """True when stage 2 completed; False for every failure and the timeout."""
        return self.status in self.OK_STATUSES


class ActLauncherImpl(Protocol):
    """The concrete launcher the api registers (session-runtime bound)."""

    def start(
        self,
        *,
        slug: str,
        job_id: str,
        page_ids: Sequence[str],
        submission: WizardSubmission | None = None,
    ) -> str | None:
        """Start the act session for *job_id*; return its session id.

        *page_ids* is the narrowed plan — the pages the delta pass flagged — and
        is the ONLY work the session is asked to do. Per-page anchor hints are
        NOT passed: they are read from each page's own doc note, so the model is
        told what the planner actually recorded rather than a second opinion
        about it. *submission* carries the operator's own choices (model,
        instructions) since the sessionless runner is the only thing holding
        them.

        Returns as soon as the session is RUNNING, so the caller can record the
        id before it blocks. ``None`` means no session could be started at all —
        the same absent-tier answer an unregistered launcher gives, and
        deliberately not a failure: nothing began, so nothing is half-done.
        """
        ...

    def wait(self, session_id: str, *, timeout: float | None = None) -> ActOutcome:
        """Block until *session_id* settles; return how it ended."""
        ...


class ActLauncher:
    """Process-wide injectable launcher for the scoped refresh's act phase."""

    _impl: ClassVar[ActLauncherImpl | None] = None

    @classmethod
    def register(cls, impl: ActLauncherImpl | None) -> None:
        """Install the concrete launcher (called by the api at startup)."""
        cls._impl = impl

    @classmethod
    def reset(cls) -> None:
        """Clear the registered launcher (test isolation / graph-only teardown)."""
        cls._impl = None

    @classmethod
    def available(cls) -> bool:
        """True when a concrete launcher is registered."""
        return cls._impl is not None

    @classmethod
    def start(
        cls,
        *,
        slug: str,
        job_id: str,
        page_ids: Sequence[str],
        submission: WizardSubmission | None = None,
    ) -> str | None:
        """Start the act session via the registered launcher; ``None`` if none."""
        if cls._impl is None:
            return None
        return cls._impl.start(
            slug=slug, job_id=job_id, page_ids=page_ids, submission=submission
        )

    @classmethod
    def wait(cls, session_id: str, *, timeout: float | None = None) -> ActOutcome | None:
        """Wait for the act session to settle; ``None`` if no launcher is wired."""
        if cls._impl is None:
            return None
        return cls._impl.wait(session_id, timeout=timeout)


__all__ = ["IN_FLIGHT_STATUSES", "ActLauncher", "ActLauncherImpl", "ActOutcome"]
