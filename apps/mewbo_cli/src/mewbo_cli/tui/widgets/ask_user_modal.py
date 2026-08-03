#!/usr/bin/env python3
"""Ask-user question modal for the Mewbo TUI (ask-user-question feature).

:class:`AskUserModal` is a :class:`~textual.screen.ModalScreen` that presents
the 1-4 structured questions of one ``ask_user_question`` tool call and blocks
the run's worker thread (via ``push_screen_wait``) until the user answers or
declines — the SAME resolver pattern as
:class:`~mewbo_cli.tui.widgets.permission_modal.PermissionModal` and
:class:`~mewbo_cli.tui.widgets.plan_modal.PlanApprovalModal`.

Each question renders as a header chip + question text, its options as a
keyboard-navigable list (an ``OptionList`` for single-select radio semantics, a
``SelectionList`` for multi-select checkboxes — the same widgets ``cli_dialogs``
already uses), plus an ever-present "Other" free-text ``Input``. ``Tab`` moves
between questions (Textual's native focus traversal); digits ``1``-``4`` pick an
option in the focused question; ``ctrl+s`` (or ``Enter`` in a text field)
submits; ``esc`` declines.

Dismiss value (returned via ``push_screen_wait``)::

    list[QuestionAnswerItem]  — one item per question, in order (answered)
    None                      — the user pressed esc without answering (declined)
    ASK_USER_EXPIRED          — the modal's own timeout fired (timed_out)

Free text WINS over a selection when both are present (the "Other" box is the
always-available answer core guarantees). A submit with any question left
unanswered is refused (a hint shows) rather than dismissing a half-answer.

When ``args.notes_placeholder`` is set, an extra free-text box renders below
the questions. Its cleaned value lands on the ``notes`` attribute rather than
the dismiss value itself — widening the dismiss type would touch every caller
of ``push_screen_wait`` for a field only the answered path ever populates, so
the dispatcher reads ``modal.notes`` off the (still-live) modal instance right
after ``push_screen_wait`` returns.

When ``args.timeout_seconds`` is set, the modal arms its OWN ``set_timer`` in
``on_mount`` rather than the dispatcher racing a deadline against the blocking
bridge. The bridge (``question_dispatcher.py``) blocks a background thread
inside `push_screen_wait`, which only ever returns once THIS screen calls
``dismiss()`` — nothing external to the screen can unblock it, and cancelling
an *awaiting* coroutine on the dispatcher side does not reach into that thread.
Owning the timer here means expiry is just another Textual event, handled on
the App's own loop exactly like a keypress, with no cross-thread signalling at
all. ``ASK_USER_EXPIRED`` is a dedicated sentinel — not ``None`` — because
``None`` already means "declined" (the user answered by a new message
instead), which would misdescribe a question that merely ran out of time and
stays open for a late answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from mewbo_core.tooling.ask_user import (
    AskUserQuestionArgs,
    QuestionAnswerItem,
    QuestionOption,
    UserQuestion,
)
from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Input, OptionList, SelectionList, Static

from mewbo_cli.cli_icons import ICONS
from mewbo_cli.cli_theme import DEFAULT_PALETTE, Palette


@dataclass
class _QuestionInput:
    """The answer state collected from one question's widgets at submit time."""

    selected_indexes: list[int] = field(default_factory=list)
    other_text: str = ""


def collect_answers(
    questions: Sequence[UserQuestion],
    states: Sequence[_QuestionInput],
) -> list[QuestionAnswerItem] | None:
    """Fold per-question widget state into ordered answer items.

    The ever-present "Other" free text WINS when present, else the selected
    option indexes. Returns ``None`` if ANY question is unanswered (no text and
    no in-bounds selection) so the caller keeps the modal open instead of
    dismissing a half-answer. Pure — no widgets, no I/O — the unit-tested core
    of the modal's answer mapping.
    """
    items: list[QuestionAnswerItem] = []
    for question, state in zip(questions, states):
        text = (state.other_text or "").strip()
        if text:
            items.append(QuestionAnswerItem(text=text))
            continue
        indexes = sorted({i for i in state.selected_indexes if 0 <= i < len(question.options)})
        if indexes:
            items.append(QuestionAnswerItem(selected_indexes=indexes))
            continue
        return None
    return items


def collect_notes(raw: str) -> str | None:
    """Trim the optional notes field; blank collapses to ``None``, never ``""``.

    Mirrors the render-side contract in ``AskUserQuestionArgs.render_answers``,
    which also treats a stripped-empty notes string as absent.
    """
    trimmed = raw.strip()
    return trimmed or None


class AskUserExpired:
    """Dismiss sentinel for the modal's own timeout — distinct from ``None``.

    ``None`` already means "declined" (esc — the user answers by a new
    message instead); reusing it for expiry would tell the model the same
    lie for a question that merely ran out of time and stays open. A
    dedicated identity, rather than a string/enum member, keeps the check at
    the dispatcher a plain ``is`` comparison with no import of core's
    ``QuestionOutcome`` literal into this widget module.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        """Render as the module-level constant name, for readable diffs/logs."""
        return "ASK_USER_EXPIRED"


ASK_USER_EXPIRED = AskUserExpired()


class AskUserModal(ModalScreen["list[QuestionAnswerItem] | AskUserExpired | None"]):
    """Blocking answer surface for one ``ask_user_question`` tool call.

    Construct with the tool's :class:`AskUserQuestionArgs`; mount via
    ``push_screen_wait`` to block the run's worker thread until the user
    resolves it. Dismiss value is ``list[QuestionAnswerItem]`` (answered, one
    per question in order), ``None`` (``esc`` — declined), or
    ``ASK_USER_EXPIRED`` (the modal's own ``timeout_seconds`` timer fired).
    """

    DEFAULT_CSS = """
    AskUserModal {
        align: center middle;
    }
    #ask-modal-container {
        width: 80%;
        max-width: 120;
        height: auto;
        max-height: 90%;
        border: solid $accent;
        background: $panel;
        padding: 1 2;
    }
    #ask-modal-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #ask-modal-body {
        height: auto;
        max-height: 30;
        overflow-y: auto;
        margin-bottom: 1;
    }
    .ask-question {
        height: auto;
        margin-bottom: 1;
    }
    .ask-q-header {
        text-style: bold;
    }
    .ask-q-text {
        margin-bottom: 1;
    }
    .ask-question OptionList, .ask-question SelectionList {
        height: auto;
        max-height: 10;
        margin-bottom: 1;
    }
    .ask-question Input {
        margin-bottom: 1;
    }
    #ask-modal-notes {
        margin-bottom: 1;
    }
    #ask-modal-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    #ask-modal-buttons Button {
        margin: 0 1;
        min-width: 18;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "submit", "Submit", show=True),
        Binding("escape", "decline", "Decline", show=True),
        Binding("1", "pick(1)", "Pick 1", show=False),
        Binding("2", "pick(2)", "Pick 2", show=False),
        Binding("3", "pick(3)", "Pick 3", show=False),
        Binding("4", "pick(4)", "Pick 4", show=False),
    ]

    def __init__(
        self,
        args: AskUserQuestionArgs,
        *,
        palette: Palette | None = None,
        id: str | None = None,  # noqa: A002
        classes: str | None = None,
    ) -> None:
        """Bind the question args and palette."""
        super().__init__(id=id, classes=classes)
        self._args = args
        self._palette = palette or DEFAULT_PALETTE
        # Single-select choice per question (explicit radio semantics: the last
        # OptionSelected wins). Multi-select reads ``SelectionList.selected``
        # live at submit; free text reads the ``Input`` value live.
        self._single_choice: dict[int, int] = {}
        # Populated at submit time only; stays ``None`` on a declined dismiss.
        # The dispatcher reads this attribute directly off the (still-live)
        # instance after ``push_screen_wait`` returns — see the module docstring.
        self.notes: str | None = None
        # Set by ``action_submit`` before it dismisses; the dispatcher reads it
        # to resolve a submit-versus-expiry race in the user's favour.
        self.submitted: list[QuestionAnswerItem] | None = None
        # Armed in ``on_mount`` when the call is bounded; ``None`` otherwise.
        self._timeout_timer: Timer | None = None
        # Guards ``_settle`` against a double dismiss when the timer's own
        # callback and a keybinding both land in the same event-loop tick.
        self._resolved = False

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Lay out the modal: title, one block per question, and the buttons."""
        with Vertical(id="ask-modal-container"):
            yield Static(
                f"{ICONS.tool_pending} A few questions before I continue",
                id="ask-modal-title",
            )
            with VerticalScroll(id="ask-modal-body"):
                for qi, question in enumerate(self._args.questions):
                    yield from self._compose_question(qi, question)
                if self._args.notes_placeholder:
                    yield Static("Notes (optional)", classes="ask-q-header")
                    yield Input(placeholder=self._args.notes_placeholder, id="ask-modal-notes")
            yield Static(self._hint_text(), id="ask-modal-hint", markup=True)
            with Horizontal(id="ask-modal-buttons"):
                yield Button("(ctrl+s) Submit", id="btn-submit", variant="success")
                yield Button("(esc) Decline", id="btn-decline", variant="error")

    def _compose_question(self, qi: int, question: UserQuestion) -> ComposeResult:
        """Render one question: header chip, text, options, and an Other field."""
        accent = self._palette.accent
        with Vertical(id=f"q-{qi}", classes="ask-question"):
            yield Static(
                f"[{accent}]▌ {markup_escape(question.header)}[/{accent}]",
                classes="ask-q-header",
                markup=True,
            )
            yield Static(markup_escape(question.question), classes="ask-q-text", markup=True)
            if question.kind == "multi_select":
                selections = [
                    (self._option_label(opt), i, False)
                    for i, opt in enumerate(question.options)
                ]
                yield SelectionList(*selections, id=f"opts-{qi}")
            elif question.kind == "single_select":
                yield OptionList(
                    *[self._option_label(opt) for opt in question.options],
                    id=f"opts-{qi}",
                )
            yield Input(placeholder=self._other_placeholder(question), id=f"other-{qi}")

    def on_mount(self) -> None:
        """Focus the first question's option list (or its Other field); arm the timeout.

        The timer lives HERE, on the App's own event loop, rather than as a
        deadline raced from the dispatcher's side of the blocking bridge — see
        the module docstring for why that split matters.
        """
        for wid in ("#opts-0", "#other-0"):
            try:
                self.query_one(wid).focus()
                break
            except Exception:  # noqa: BLE001 — best-effort initial focus
                continue
        if self._args.timeout_seconds is not None:
            self._timeout_timer = self.set_timer(self._args.timeout_seconds, self._expire)

    # ------------------------------------------------------------------
    # Selection capture
    # ------------------------------------------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Record a single-select choice (radio semantics: last selection wins).

        Guarded to the single-select ``OptionList``: a ``SelectionList`` is an
        ``OptionList`` too, but multi-select is read from ``.selected`` at
        submit, so its own toggles must not be mistaken for a radio pick.
        """
        if isinstance(event.option_list, SelectionList):
            return
        qi = self._qi_of(event.option_list)
        if qi is not None:
            self._single_choice[qi] = event.option_index
            self._refresh_hint()

    def action_pick(self, number: int) -> None:
        """Digit shortcut: pick option ``number`` in the focused question."""
        focused = self.focused
        qi = self._qi_of(focused)
        if qi is None:
            return
        question = self._args.questions[qi]
        index = number - 1
        if index >= len(question.options):
            return
        if question.kind == "multi_select" and isinstance(focused, SelectionList):
            focused.toggle(index)
        elif question.kind == "single_select" and isinstance(focused, OptionList):
            focused.highlighted = index
            self._single_choice[qi] = index
        self._refresh_hint()

    # ------------------------------------------------------------------
    # Submit / decline
    # ------------------------------------------------------------------

    def action_submit(self) -> None:
        """Collect the answers; dismiss when complete, else flag the gap.

        The collected result is recorded on ``self.submitted`` BEFORE settling,
        so a submit landing in the same event-loop tick as the deadline is still
        readable by the dispatcher even if ``_expire`` wins the race to actually
        dismiss. Without that, an answer the user genuinely typed could be
        discarded by the expiry path — the api dispatcher already holds the
        matching law ("the user's answer always wins" over a withdraw), and the
        two dispatchers should not disagree about whose input outranks a clock.
        """
        result = self._collect()
        if result is None:
            self._flag_incomplete()
            return
        self.notes = self._read_notes()
        self.submitted = result
        self._settle(result)

    def action_decline(self) -> None:
        """Dismiss with ``None`` (esc / Decline — the user answers by message)."""
        self._settle(None)

    def _expire(self) -> None:
        """Timer callback: dismiss with the timeout sentinel.

        Runs on the App's own event loop (``set_timer`` wraps the callback in
        ``call_next``), so this is an ordinary Textual event handler — not a
        cross-thread call — exactly like ``action_submit``/``action_decline``.
        """
        self._settle(ASK_USER_EXPIRED)

    def _settle(self, result: list[QuestionAnswerItem] | AskUserExpired | None) -> None:
        """Cancel the timeout timer and dismiss exactly once.

        Guards against the narrow race where both a keybinding action and the
        timer's own callback land in the same tick — whichever runs first wins
        the dismiss, and the other becomes a harmless no-op instead of a double
        ``dismiss()`` call on an already-popped screen.
        """
        if self._resolved:
            return
        self._resolved = True
        if self._timeout_timer is not None:
            self._timeout_timer.stop()
        self.dismiss(result)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in a text field attempts a submit (a hint shows if incomplete)."""
        self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route the Submit / Decline buttons to their actions."""
        if event.button.id == "btn-submit":
            self.action_submit()
        else:
            self.action_decline()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect(self) -> list[QuestionAnswerItem] | None:
        """Read every question's widget state and fold via :func:`collect_answers`."""
        states: list[_QuestionInput] = []
        for qi, question in enumerate(self._args.questions):
            other = ""
            try:
                other = self.query_one(f"#other-{qi}", Input).value
            except Exception:  # noqa: BLE001 — a missing field reads as unanswered
                other = ""
            indexes: list[int] = []
            if question.kind == "multi_select":
                try:
                    selection_list = self.query_one(f"#opts-{qi}", SelectionList)
                    indexes = [int(value) for value in selection_list.selected]
                except Exception:  # noqa: BLE001
                    indexes = []
            elif question.kind == "single_select":
                choice = self._single_choice.get(qi)
                indexes = [choice] if choice is not None else []
            states.append(_QuestionInput(selected_indexes=indexes, other_text=other))
        return collect_answers(self._args.questions, states)

    def _read_notes(self) -> str | None:
        """Read the optional notes field; absent entirely when never composed."""
        if not self._args.notes_placeholder:
            return None
        try:
            raw = self.query_one("#ask-modal-notes", Input).value
        except Exception:  # noqa: BLE001 — a missing field reads as no notes
            return None
        return collect_notes(raw)

    @staticmethod
    def _qi_of(widget: object | None) -> int | None:
        """Return the question index a widget belongs to via its ``opts-<qi>`` id."""
        wid = getattr(widget, "id", None)
        if isinstance(wid, str) and wid.startswith("opts-"):
            try:
                return int(wid.split("-", 1)[1])
            except ValueError:
                return None
        return None

    @staticmethod
    def _option_label(option: QuestionOption) -> str:
        """Escaped option label with an optional dim description suffix."""
        label = markup_escape(option.label)
        if option.description:
            return f"{label}  [dim]— {markup_escape(option.description)}[/dim]"
        return label

    @staticmethod
    def _other_placeholder(question: UserQuestion) -> str:
        """Placeholder for the Other field (the sole input for a free-text question)."""
        if question.kind == "free_text":
            return "Type your answer…"
        return "Other (type a free-text answer instead)…"

    def _hint_text(self) -> str:
        """The base key-hint line shown under the questions.

        A bounded call gets one understated, static budget line appended —
        never a live countdown (that would need a timer widget this modal
        doesn't otherwise carry).
        """
        parts = [
            "arrow keys + digits [b]1-4[/b] to pick",
            "type in [b]Other[/b] for free text",
            "[b]ctrl+s[/b] submit",
            "[b]esc[/b] decline",
        ]
        if self._args.timeout_seconds is not None:
            parts.append(f"times out in {self._args.timeout_seconds}s")
        return f"[dim]{' · '.join(parts)}[/dim]"

    def _refresh_hint(self) -> None:
        """Reset the hint to its base text (clears an incomplete-warning flash)."""
        try:
            self.query_one("#ask-modal-hint", Static).update(self._hint_text())
        except Exception:  # noqa: BLE001 — hint is cosmetic
            pass

    def _flag_incomplete(self) -> None:
        """Flash a warning when a submit is attempted with unanswered questions."""
        try:
            self.query_one("#ask-modal-hint", Static).update(
                f"[{self._palette.warning}]Every question needs an answer[/] "
                "— pick an option or type in its Other box."
            )
        except Exception:  # noqa: BLE001 — hint is cosmetic
            pass


__all__ = ["ASK_USER_EXPIRED", "AskUserExpired", "AskUserModal", "collect_answers", "collect_notes"]
