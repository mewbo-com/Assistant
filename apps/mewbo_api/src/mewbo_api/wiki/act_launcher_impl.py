"""Concrete ``ActLauncher`` — the session-backed stage 2 of a scoped refresh.

Bridges the down-only ``mewbo_graph.plugins.wiki.act_launcher.ActLauncher`` seam
to THIS app's session lifecycle, so ``ScopedRefreshRunner`` (which runs
sessionless) can hand its narrowed page plan to a real Mewbo session without the
library importing up. The exact counterpart of ``_register_search_launcher``:
the engine cannot reach ``SessionRuntime``, so the api injects a launcher here.

Start and wait are two calls, not one: the caller records the session id in its
own resume sidecar between them, so a process that dies inside the wait resumes
knowing which session was already running instead of starting a second one over
the same pages. See the seam module for the full rationale.

**The contract this expects from ``mewbo_api.wiki.jobs``** — the module that owns
every session-start body in this feature, and where the act session's tool
ceiling, playbook and capability advertisement live beside ``INDEXER_TOOLS``::

    def _start_refresh_act_session(
        *,
        store: WikiStoreBase,
        runtime: Any,
        job_id: str,
        slug: str,
        page_ids: list[str],
        submission: WizardSubmission | None = None,
        hook_manager: Any = None,
    ) -> str:
        \"\"\"Create + start the act session for *job_id*; return the session id.\"\"\"

It is imported LAZILY, inside a ``try/except ImportError``, so this module stays
importable — and the launcher registrable — before that function exists or on a
build where it was removed: :meth:`start` then returns ``None``, which the seam
defines as "act phase absent". A launcher that cannot resolve the wiki store
degrades the same way rather than raising into the refresh's daemon thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from mewbo_core.common import get_logger
from mewbo_graph.plugins.wiki.act_launcher import IN_FLIGHT_STATUSES, ActOutcome

logging = get_logger(name="api.wiki.act_launcher_impl")

# A page rewrite is an agent fan-out over the flagged pages, so the ceiling is
# generous — it exists to stop a wedged session from pinning the refresh thread
# forever, not to bound normal work.
DEFAULT_ACT_TIMEOUT_S = 3600.0
_POLL_INTERVAL_S = 2.0


@dataclass(frozen=True)
class SessionActLauncher:
    """An ``ActLauncher`` impl over the api's session runtime.

    ``sleep``/``monotonic`` are injected fields so the wait is testable without
    real time passing — the clock is a collaborator, not an import.
    """

    runtime: Any = None
    hook_manager: Any = None
    timeout_seconds: float = DEFAULT_ACT_TIMEOUT_S
    poll_interval: float = _POLL_INTERVAL_S
    sleep: Callable[[float], None] = field(default=time.sleep)
    monotonic: Callable[[], float] = field(default=time.monotonic)

    # -- launcher protocol --------------------------------------------------

    def start(
        self,
        *,
        slug: str,
        job_id: str,
        page_ids: Sequence[str],
        submission: Any = None,
    ) -> str | None:
        """Mint + start the act session; ``None`` when no session could start.

        Returns promptly with the session id — the caller records it before
        calling :meth:`wait`, which is the whole reason the seam is split.
        """
        store = getattr(self.runtime, "wiki_store", None)
        if store is None:
            logging.warning("wiki act launcher: no wiki store on the runtime; skipping")
            return None
        try:
            from .jobs import _start_refresh_act_session  # noqa: PLC0415
        except ImportError:
            logging.info("wiki act launcher: no act session start body; act phase absent")
            return None

        session_id = _start_refresh_act_session(
            store=store,
            runtime=self.runtime,
            job_id=job_id,
            slug=slug,
            page_ids=list(page_ids),
            submission=submission,
            hook_manager=self.hook_manager,
        )
        # An empty session id is a refused start (the run registry already has a
        # run for this session). Report it as absence, matching the seam: the
        # caller then leaves the pages alone and finalizes rather than failing
        # the job over work that never began.
        return session_id or None

    def wait(self, session_id: str, *, timeout: float | None = None) -> ActOutcome:
        """Poll until the act session settles, or the deadline expires.

        Liveness is read from BOTH the run registry and the derived session
        status, because neither alone is honest for the whole window: right
        after ``start_async`` the transcript may not yet show a run, and after
        the run thread exits the registry has already forgotten it.
        """
        budget = self.timeout_seconds if timeout is None else timeout
        deadline = self.monotonic() + budget
        while True:
            status, error = self._status(session_id)
            if not self._in_flight(session_id, status):
                return ActOutcome(session_id=session_id, status=status, error=error)
            if self.monotonic() >= deadline:
                return ActOutcome(
                    session_id=session_id,
                    status="timeout",
                    error=f"act session did not settle within {budget:.0f}s",
                )
            self.sleep(self.poll_interval)

    # -- internals ----------------------------------------------------------

    def _in_flight(self, session_id: str, status: str) -> bool:
        """True while the session still has work to do."""
        if status in IN_FLIGHT_STATUSES:
            return True
        is_running = getattr(self.runtime, "is_running", None)
        return bool(is_running and is_running(session_id))

    def _status(self, session_id: str) -> tuple[str, str | None]:
        """Read the session's derived status + error, tolerating a store hiccup.

        A read failure must NOT be reported as a settled session — that would
        finalize a refresh whose pages are still being rewritten — so it reports
        the in-flight status and lets the deadline be the bound.
        """
        try:
            summary = self.runtime.summarize_session(session_id)
        except Exception as exc:  # pragma: no cover — defensive, store hiccup
            logging.warning("wiki act launcher: status read failed ({}); retrying", exc)
            return "running", None
        status = str(summary.get("status") or "idle")
        error = summary.get("error") or summary.get("done_reason")
        if status in ActOutcome.OK_STATUSES:
            return status, None
        return status, str(error) if error else None


__all__ = ["DEFAULT_ACT_TIMEOUT_S", "SessionActLauncher"]
