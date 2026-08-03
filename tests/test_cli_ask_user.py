"""Ask-user question CLI surface — answer-mapping + dispatcher tests.

Covers the four load-bearing seams the interactive TUI adds for the
``ask_user_question`` tool, without a Textual pilot:

- ``collect_answers`` — the pure fold from per-question widget state to the
  ordered ``QuestionAnswerItem`` list the modal dismisses with.
- ``collect_notes`` — the pure trim/blank-to-``None`` fold for the optional
  notes field.
- ``AskUserModal``'s timeout wiring (``on_mount`` arming ``set_timer``,
  ``_expire``, ``_settle``) — exercised with ``set_timer``/``dismiss`` mocked
  onto an unmounted instance, since the timer itself is real Textual
  machinery no unit test should re-implement.
- ``TuiQuestionDispatcher.dispatch`` — the outcome mapping (answered / declined
  / interrupted / timed_out), driven with a fake App whose ``call_from_thread``
  returns a canned modal dismiss value (DI makes this trivial — no real modal
  is mounted).
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

from mewbo_cli.tui.question_dispatcher import TuiQuestionDispatcher
from mewbo_cli.tui.widgets.ask_user_modal import (
    ASK_USER_EXPIRED,
    AskUserModal,
    _QuestionInput,
    collect_answers,
    collect_notes,
)
from mewbo_core.tooling.ask_user import AskUserQuestionArgs, QuestionAnswerItem


def _args(*questions: dict, timeout_seconds: int | None = None) -> AskUserQuestionArgs:
    payload: dict = {"questions": list(questions)}
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    return AskUserQuestionArgs.model_validate(payload)


_FREE_TEXT = {"header": "Notes", "question": "Anything else?"}
_SINGLE = {
    "header": "Auth",
    "question": "Which auth method?",
    "options": [{"label": "OAuth"}, {"label": "API key"}],
}
_MULTI = {
    "header": "Scopes",
    "question": "Which scopes?",
    "options": [{"label": "read"}, {"label": "write"}, {"label": "admin"}],
    "multi_select": True,
}


# ---------------------------------------------------------------------------
# collect_answers — the modal's pure answer-mapping core
# ---------------------------------------------------------------------------


def test_collect_free_text_answer():
    """A free-text question maps its Other text to a text answer item."""
    args = _args(_FREE_TEXT)
    result = collect_answers(args.questions, [_QuestionInput(other_text="ship it")])
    assert result == [QuestionAnswerItem(text="ship it")]


def test_collect_single_select_answer():
    """A single-select choice maps to a one-element selected_indexes item."""
    args = _args(_SINGLE)
    result = collect_answers(args.questions, [_QuestionInput(selected_indexes=[1])])
    assert result == [QuestionAnswerItem(selected_indexes=[1])]


def test_collect_multi_select_answer_dedupes_and_sorts():
    """A multi-select maps its toggled values to sorted, unique indexes."""
    args = _args(_MULTI)
    result = collect_answers(args.questions, [_QuestionInput(selected_indexes=[2, 0, 0])])
    assert result == [QuestionAnswerItem(selected_indexes=[0, 2])]


def test_free_text_wins_over_selection():
    """The ever-present Other text supersedes a selection on the same question."""
    args = _args(_SINGLE)
    result = collect_answers(
        args.questions,
        [_QuestionInput(selected_indexes=[0], other_text="  something else  ")],
    )
    assert result == [QuestionAnswerItem(text="something else")]


def test_out_of_bounds_indexes_are_dropped_as_unanswered():
    """An index past the option count is not a valid answer → unanswered → None."""
    args = _args(_SINGLE)
    result = collect_answers(args.questions, [_QuestionInput(selected_indexes=[9])])
    assert result is None


def test_any_unanswered_question_yields_none():
    """One blank question keeps the whole group unanswered (modal stays open)."""
    args = _args(_SINGLE, _FREE_TEXT)
    result = collect_answers(
        args.questions,
        [_QuestionInput(selected_indexes=[0]), _QuestionInput()],
    )
    assert result is None


def test_collect_preserves_question_order():
    """Items come back one-per-question in the questions' order."""
    args = _args(_SINGLE, _MULTI, _FREE_TEXT)
    result = collect_answers(
        args.questions,
        [
            _QuestionInput(selected_indexes=[0]),
            _QuestionInput(selected_indexes=[1]),
            _QuestionInput(other_text="notes"),
        ],
    )
    assert result == [
        QuestionAnswerItem(selected_indexes=[0]),
        QuestionAnswerItem(selected_indexes=[1]),
        QuestionAnswerItem(text="notes"),
    ]


# ---------------------------------------------------------------------------
# collect_notes — the modal's pure notes-field fold
# ---------------------------------------------------------------------------


def test_collect_notes_trims_and_keeps_text():
    """Surrounding whitespace is stripped from a real notes value."""
    assert collect_notes("  extra context for the deploy  ") == "extra context for the deploy"


def test_collect_notes_blank_becomes_none():
    """A whitespace-only notes field collapses to ``None``, never ``""``."""
    assert collect_notes("   ") is None
    assert collect_notes("") is None


# ---------------------------------------------------------------------------
# AskUserModal — the timer lives here, not in the dispatcher (see
# question_dispatcher.py's module docstring for why a wait_for-based deadline
# on the dispatcher side of the bridge cannot work). ``set_timer``/``dismiss``
# are mocked onto an unmounted instance — real Textual scheduling is out of
# scope for a unit test; only the wiring around it is under test here.
# ---------------------------------------------------------------------------


def test_on_mount_arms_the_timer_when_bounded():
    """A bounded call schedules ``_expire`` via ``set_timer`` at mount time."""
    modal = AskUserModal(_args(_SINGLE, timeout_seconds=5))
    fake_timer = Mock()
    modal.set_timer = Mock(return_value=fake_timer)
    modal.on_mount()
    modal.set_timer.assert_called_once_with(5, modal._expire)
    assert modal._timeout_timer is fake_timer


def test_on_mount_does_not_arm_a_timer_when_unbounded():
    """No ``timeout_seconds`` ⇒ no timer — byte-identical to the pre-timeout path."""
    modal = AskUserModal(_args(_SINGLE))
    modal.set_timer = Mock()
    modal.on_mount()
    modal.set_timer.assert_not_called()
    assert modal._timeout_timer is None


def test_expire_settles_with_the_sentinel_not_none():
    """The timer callback dismisses with ``ASK_USER_EXPIRED`` — never ``None``.

    ``None`` already means declined; conflating the two would tell the model
    the user answered by message when the question actually just ran out of
    time and stays open for a late answer.
    """
    modal = AskUserModal(_args(_SINGLE, timeout_seconds=5))
    modal.dismiss = Mock()
    modal._expire()
    modal.dismiss.assert_called_once_with(ASK_USER_EXPIRED)


def test_settle_is_idempotent_and_stops_the_timer():
    """A second settle (the same-tick race) is a no-op, not a double dismiss."""
    modal = AskUserModal(_args(_SINGLE, timeout_seconds=5))
    fake_timer = Mock()
    modal._timeout_timer = fake_timer
    modal.dismiss = Mock()
    modal._settle(ASK_USER_EXPIRED)
    modal._settle(None)  # e.g. a queued decline landing right after expiry
    fake_timer.stop.assert_called_once()
    modal.dismiss.assert_called_once_with(ASK_USER_EXPIRED)


# ---------------------------------------------------------------------------
# TuiQuestionDispatcher — outcome mapping over a fake App
# ---------------------------------------------------------------------------


class _FakeApp:
    """Stand-in for the mounted ``MewboApp``.

    ``call_from_thread`` is the only method the dispatcher touches; it returns a
    canned modal dismiss value (``list[QuestionAnswerItem] | AskUserExpired |
    None``) instead of ever mounting a real modal — the DI seam that makes this
    unit-testable. ``notes``/``submitted`` are stamped onto the modal instance
    the dispatcher constructed (mirroring what ``action_submit`` does on a real
    submit), so both the ordinary notes round-trip and the submit-vs-expiry
    race rescue are exercised without a real widget.
    """

    def __init__(
        self,
        dismiss_value,
        *,
        notes: str | None = None,
        submitted: list[QuestionAnswerItem] | None = None,
        raises: bool = False,
    ) -> None:
        self._dismiss_value = dismiss_value
        self._notes = notes
        self._submitted = submitted
        self._raises = raises

    def push_screen_wait(self, modal):  # pragma: no cover - passed as an arg only
        raise AssertionError("push_screen_wait must be reached via call_from_thread")

    def call_from_thread(self, func, *args, **kwargs):
        if self._raises:
            raise RuntimeError("app loop is gone")
        modal = args[0] if args else None
        if modal is not None:
            if isinstance(self._dismiss_value, list):
                modal.notes = self._notes
            if self._submitted is not None:
                modal.submitted = self._submitted
        return self._dismiss_value


def _dispatch(app_provider, args: AskUserQuestionArgs):
    dispatcher = TuiQuestionDispatcher(app_provider=app_provider)
    return asyncio.run(dispatcher.dispatch("session-1", args))


def test_dispatch_answered_maps_modal_items():
    """A returned item list → ``answered`` with the items + a ``cli`` chip."""
    args = _args(_SINGLE)
    items = [QuestionAnswerItem(selected_indexes=[0])]
    result = _dispatch(lambda: _FakeApp(items), args)
    assert result.outcome == "answered"
    assert result.answers == (QuestionAnswerItem(selected_indexes=[0]),)
    assert result.answered_via == "cli"
    # The rendered tool result reflects the chosen option label.
    assert "OAuth" in result.render(args)


def test_dispatch_declined_when_modal_dismissed_with_none():
    """``esc`` (None dismiss) → ``declined`` (the user answers by message)."""
    args = _args(_SINGLE)
    result = _dispatch(lambda: _FakeApp(None), args)
    assert result.outcome == "declined"
    assert result.answers == ()


def test_dispatch_interrupted_when_app_unavailable():
    """No mounted App → ``interrupted`` rather than wedging the run."""
    args = _args(_SINGLE)
    result = _dispatch(lambda: None, args)
    assert result.outcome == "interrupted"


def test_dispatch_interrupted_when_bridge_raises():
    """A failing modal bridge → ``interrupted`` (never propagates into the loop)."""
    args = _args(_SINGLE)
    result = _dispatch(lambda: _FakeApp(None, raises=True), args)
    assert result.outcome == "interrupted"


def test_dispatch_answered_includes_notes():
    """The modal's collected notes ride through to the dispatch result."""
    args = _args(_SINGLE)
    items = [QuestionAnswerItem(selected_indexes=[0])]
    result = _dispatch(lambda: _FakeApp(items, notes="please double-check staging"), args)
    assert result.outcome == "answered"
    assert result.notes == "please double-check staging"
    assert "Notes: please double-check staging" in result.render(args)


def test_dispatch_answered_notes_absent_when_none():
    """No notes collected (blank field, or no ``notes_placeholder``) → ``None``."""
    args = _args(_SINGLE)
    items = [QuestionAnswerItem(selected_indexes=[0])]
    result = _dispatch(lambda: _FakeApp(items, notes=None), args)
    assert result.outcome == "answered"
    assert result.notes is None


def test_dispatch_unbounded_call_still_blocks_until_answered():
    """No ``timeout_seconds`` ⇒ the plain path, unchanged."""
    args = _args(_SINGLE)
    assert args.timeout_seconds is None
    items = [QuestionAnswerItem(selected_indexes=[0])]
    result = _dispatch(lambda: _FakeApp(items), args)
    assert result.outcome == "answered"


def test_dispatch_maps_expired_sentinel_to_timed_out():
    """The modal owns the timer; the dispatcher just reads its sentinel dismiss value.

    Nothing here drives a real clock — the modal's ``set_timer`` wiring is
    covered separately above; this only checks the dispatcher's mapping once
    that sentinel comes back as the ``push_screen_wait`` result.
    """
    args = _args(_SINGLE, timeout_seconds=5)
    result = _dispatch(lambda: _FakeApp(ASK_USER_EXPIRED), args)
    assert result.outcome == "timed_out"
    assert result.answers == ()
    assert "the 5s this call allowed" in result.render(args)


def test_dispatch_expiry_loses_to_a_same_tick_submit():
    """``modal.submitted`` rescues a race where the timer wins the dismiss.

    Mirrors the api dispatcher's law that a genuine answer always outranks a
    withdraw that merely raced it in — a clock must never discard input a
    human actually gave.
    """
    args = _args(_SINGLE, timeout_seconds=5)
    items = [QuestionAnswerItem(selected_indexes=[0])]
    result = _dispatch(lambda: _FakeApp(ASK_USER_EXPIRED, submitted=items), args)
    assert result.outcome == "answered"
    assert result.answers == (QuestionAnswerItem(selected_indexes=[0]),)
