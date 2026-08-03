"""Startup honesty sweep for orphaned wiki-QA answers.

``QaSessionEndHook`` is the net for a QA run that ends cleanly without ever
calling ``wiki_emit_answer`` — but a session end is a HANDOFF from the run's
own ``finally``, and a process death (deploy, restart, OOM kill) never reaches
it at all. The transcript simply stops, the backing session is gone, and the
``QaAnswer`` stays at ``status: "running"`` with empty ``blocks`` forever: the
console SSE stream only ends on idle timeout, and a non-streaming consumer
(the MCP ``ask_wiki`` poll) waits out its full timeout against a row that will
never settle.

``QaAnswerSweeper`` is the QA-domain peer of ``SessionRunSweeper``
(``apps/mewbo_api/run_sweep.py`` — read it first, this mirrors its
miss-only-allowlist discipline): run ONCE at startup, it settles every
``running`` answer whose backing session is confirmed not running and whose
CURRENT TURN never even started emitting (no ``block_open``), via
``QaFinalizer.close`` — the same honest-error path the completion seam and
``QaSessionEndHook`` already use. An answer with partial blocks, or no
recoverable session binding, is left alone: the worst case is a MISSED orphan
(status stays ``running``, no regression), never a wrongly-settled live one.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from mewbo_core.common import get_logger

logging = get_logger(name="api.wiki.qa_sweep")

# Only settle answers whose backing session's LAST activity is recent — mirrors
# ``run_sweep._RECENT_ACTIVITY_WINDOW``. Keeps an ancient abandoned answer from
# resurfacing on a boot that first sees it; steady-state boots only ever see
# answers orphaned since the previous restart, well inside it.
_QA_RECENT_ACTIVITY_WINDOW = timedelta(days=7)

# The sweeper reads only the backing session's event TAIL to judge recency,
# mirroring ``run_sweep._TAIL_SCAN_LIMIT`` — never the whole transcript.
_QA_TAIL_SCAN_LIMIT = 64

# The honest close reason for an answer this sweep settles — distinct from
# both ``QaFinalizer.close``'s own auto-generated "wiki_emit_answer was never
# called" (a live model failure) and ``QaSessionEndHook``'s silent-exit case,
# so a reader of the persisted error can tell a crash-recovered zombie from a
# model that simply never called the tool.
_QA_SWEEP_ERROR = (
    "the session backing this answer ended (process restart) without "
    "completing it — settled by the startup sweep"
)


class QaAnswerSweeper:
    """Settle wiki-QA answers orphaned by a process restart, once at startup.

    Collaborators are injected as fields: the ``runtime`` (its ``wiki_store``,
    ``session_store`` and ``is_running``) and a ``now`` clock (a test passes a
    fixed lambda — no clock is ever patched).
    """

    def __init__(
        self,
        runtime: Any,
        *,
        now: Callable[[], datetime],
        window: timedelta = _QA_RECENT_ACTIVITY_WINDOW,
        tail_limit: int = _QA_TAIL_SCAN_LIMIT,
    ) -> None:
        """Wire the runtime + clock; ``window``/``tail_limit`` are test overrides."""
        self._runtime = runtime
        self._now = now
        self._window = window
        self._tail_limit = tail_limit
        self._done = False

    def sweep(self) -> int:
        """Settle every orphaned ``running`` QA answer; return the count settled.

        Never raises: a failure enumerating answers, or settling any single
        one, is logged and swallowed — a bad store or a bad transcript must
        never break startup. The in-process ``_done`` guard stops a redundant
        re-run within one process; ``QaFinalizer.close``'s own idempotency
        covers a second process/worker doing the same pass.
        """
        if self._done:
            return 0
        self._done = True
        store = self._runtime.wiki_store
        try:
            running = store.list_qa(status="running")
        except Exception:
            logging.warning("wiki QA sweep could not list answers", exc_info=True)
            return 0
        settled = 0
        for answer in running:
            try:
                if self._settle_if_orphaned(store, answer):
                    settled += 1
            except Exception:
                logging.warning(
                    "wiki QA sweep skipped {} after an error",
                    answer.answer_id,
                    exc_info=True,
                )
        return settled

    def _settle_if_orphaned(self, store: Any, answer: Any) -> bool:
        """Settle *answer* if it is orphaned; return whether it was settled.

        Cheap gates first (a missing session binding, a genuinely live
        session), then a bounded tail read for the recency window, then the
        domain check — so an answer that fails an early gate never pays for
        the later ones. Guarding on ``is_running`` is what makes this sweep
        safe to run before every other in-flight answer has necessarily
        settled: a genuinely live run is never touched.
        """
        from mewbo_graph.wiki.qa import QaFinalizer  # noqa: PLC0415

        session_id = store.get_qa_session(answer.answer_id)
        if not session_id:
            return False
        if self._runtime.is_running(session_id):
            return False
        tail = self._runtime.session_store.load_recent_events(
            session_id, limit=self._tail_limit
        )
        if not tail or not self._within_window(tail[-1]):
            return False
        events = QaFinalizer.current_turn_events(store.load_qa_events(answer.answer_id))
        if any(ev.get("type") == "block_open" for ev in events):
            # A partial emit is a different shape than this sweep settles —
            # left alone rather than guessed at (miss-only, never wrong).
            return False
        return QaFinalizer.close(store, answer.answer_id, error=_QA_SWEEP_ERROR)

    def _within_window(self, event: Any) -> bool:
        """True iff the event's timestamp is within the recency window.

        A missing/unparseable timestamp is treated as in-window (settle
        rather than silently skip) — same default as ``SessionRunSweeper``.
        """
        raw = event.get("ts")
        if not isinstance(raw, str):
            return True
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            return True
        return (self._now() - ts) <= self._window


__all__ = ["QaAnswerSweeper"]
