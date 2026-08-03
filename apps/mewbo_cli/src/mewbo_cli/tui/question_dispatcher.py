#!/usr/bin/env python3
"""Ask-user question dispatch — CLI (Textual) side.

Sibling of the api's ``ApiQuestionDispatcher``: core owns the ``SessionTool``
(``AskUserQuestionTool``) and the down-only ``QuestionDispatcher`` DI seam; this
module owns the CONCRETE dispatcher the interactive TUI registers at startup.
When the root agent calls ``ask_user_question``, core dispatches through the
process-wide seam to :meth:`TuiQuestionDispatcher.dispatch`, which opens the
blocking :class:`~mewbo_cli.tui.widgets.ask_user_modal.AskUserModal` and returns
the user's answers synchronously as a ``QuestionDispatchResult``.

The thread bridge is the SAME one the permission/plan modals use
(``cli_master._modal_resolver`` / ``_plan_approval_resolver``): ``dispatch``
runs on the run's worker thread (``run_sync`` drives the async loop off the UI
thread), and ``app.call_from_thread(app.push_screen_wait, modal)`` schedules the
modal on the App's own loop and blocks the caller until the user resolves it.
``dispatch`` is ``async`` and awaited inside the loop, so the blocking bridge is
run via ``asyncio.to_thread`` — the worker's event loop stays responsive while
the human answers, and the modal still blocks exactly as the sync resolvers do.

**The wait here is UNBOUNDED even when ``args.timeout_seconds`` is set.** A
tempting first cut is to race ``push_screen_wait`` against
``asyncio.wait_for`` from this side of the bridge — that does not work:
``push_screen_wait`` only ever returns once the modal itself calls
``dismiss()``, and cancelling the *awaiting* coroutine on a `wait_for` timeout
does not reach into the background thread blocked inside it — that thread is
already running inside the executor and cannot be interrupted mid-flight, so
the modal would stay parked on screen (and the thread wedged) for the rest of
the process. The fix is for the deadline to live INSIDE the modal
(``AskUserModal.on_mount`` arms its own ``set_timer``, see that module's
docstring) — expiry is then just another Textual event dismissing the screen
on the App's own loop, exactly like ``esc``, and this dispatcher only ever
needs to interpret the resulting dismiss value.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from mewbo_core.tooling.ask_user import (
    AskUserQuestionArgs,
    QuestionAnswerItem,
    QuestionDispatchResult,
)

from mewbo_cli.cli_theme import DEFAULT_PALETTE, Palette
from mewbo_cli.tui.widgets.ask_user_modal import ASK_USER_EXPIRED, AskUserModal

# answered_via chip recorded on an answered result — mirrors the api's
# ``X-Mewbo-Surface`` → ``answered_via`` convention ("answered on cli").
_ANSWERED_VIA = "cli"


class TuiQuestionDispatcher:
    """Concrete ``QuestionDispatcherImpl`` for the interactive Textual CLI.

    Atomic class: the mounted ``MewboApp`` is injected as a provider callable —
    the App exists only post-mount, so it is resolved lazily, like the
    transcript-hub sink and the fleet hooks — plus the semantic palette. One
    instance is registered into ``QuestionDispatcher`` in
    ``cli_master._run_app`` for the interactive-TTY path only; the plain-REPL /
    ``--query`` / no-TTY paths register nothing, so the tool never binds there.
    """

    def __init__(
        self,
        *,
        app_provider: Callable[[], Any],
        palette: Palette | None = None,
    ) -> None:
        """Bind the lazy App provider and the modal palette."""
        self._app_provider = app_provider
        self._palette = palette or DEFAULT_PALETTE

    async def dispatch(
        self, session_id: str, args: AskUserQuestionArgs
    ) -> QuestionDispatchResult:
        """Open the blocking modal; map its dismiss value to a dispatch result.

        ``None`` (esc) → ``declined``; a list of items → ``answered``;
        ``ASK_USER_EXPIRED`` → ``timed_out`` (the modal's own timer fired — see
        that module for why the deadline lives there, not here). An unavailable
        App or a modal failure → ``interrupted`` (never wedges the run — the
        tool result then tells the model to proceed).
        """
        app = self._app_provider()
        if app is None:
            return QuestionDispatchResult(outcome="interrupted")
        modal = AskUserModal(args, palette=self._palette)
        try:
            answers = await asyncio.to_thread(
                app.call_from_thread, app.push_screen_wait, modal
            )
        except Exception:  # noqa: BLE001 — a modal failure must not wedge the run
            return QuestionDispatchResult(outcome="interrupted")
        if answers is ASK_USER_EXPIRED:
            # ``modal.submitted`` is stamped before the modal settles, so a
            # submit landing in the same event-loop tick as expiry is still
            # visible here and WINS — the api dispatcher holds the same law
            # for a withdraw that loses a race to a genuine answer; a clock
            # must never discard input a human actually gave.
            if modal.submitted is not None:
                return self._answered(modal, modal.submitted)
            return QuestionDispatchResult(outcome="timed_out")
        if answers is None:
            return QuestionDispatchResult(outcome="declined")
        return self._answered(modal, answers)

    def _answered(
        self, modal: AskUserModal, answers: list[QuestionAnswerItem]
    ) -> QuestionDispatchResult:
        """Build the answered result, reading notes off the still-live modal."""
        return QuestionDispatchResult(
            outcome="answered",
            answers=tuple(answers),
            answered_via=_ANSWERED_VIA,
            notes=modal.notes,
        )


__all__ = ["TuiQuestionDispatcher"]
