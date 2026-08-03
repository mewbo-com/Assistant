#!/usr/bin/env python3
"""First-party plugin implementation of the ``present_ui`` SessionTool.

The agent describes a small structured panel with the typed vocabulary in
:mod:`.nodes`; this tool validates it, degrades it to plain text server-side,
and emits ONE ``generative_ui`` event. The console renders the tree; every
other surface renders the precomputed ``alt_text`` and never has to learn the
component vocabulary.

Terminal-free, like ``submit_widget`` and ``update_todos``: the panel renders
off the emitted event, not off run termination, so presenting one is an
ordinary step and the agent still gets to write its closing message.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    field_validator,
)

from mewbo_core.builtin_plugins.generative_ui.nodes import GenerativeUISpec
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.tooling.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.contracts.types import Event

logging = get_logger(name="core.builtin_plugins.generative_ui")

PRESENT_UI_TOOL_ID = "present_ui"

# Transcript event kind. FROZEN — a console TS type mirrors it, and a Kotlin
# mirror is expected to follow.
GENERATIVE_UI_EVENT = "generative_ui"

# The client-advertised capability that opts a session into this tool. A plain
# string two sides agree on; there is deliberately no capability enum anywhere
# in the codebase (see ``packages/mewbo_core/CLAUDE.md``).
GENERATIVE_UI_CAPABILITY = "generative_ui"

# The upsert key's shape, exactly as the wire contract freezes it. Short enough
# to stay readable in a tool result the agent has to quote back, and random
# rather than sequential so two concurrent agents in one session cannot mint the
# same id and silently overwrite each other's panel.
UI_ID_PATTERN = r"^gui-[0-9a-f]{8}$"

UiId = Annotated[str, StringConstraints(pattern=UI_ID_PATTERN)]
Summary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class PresentUiArgs(BaseModel):
    """Render a structured UI panel in the conversation.

    Use this to SHOW structured information — a status board, a comparison
    table, a set of results — instead of describing it in prose. Compose the
    panel from the listed components; each one carries its own typed fields.
    Keep it small: a panel is a summary a reader takes in at a glance, not a
    document.

    Non-visual clients receive an automatic plain-text rendering, so never
    repeat the panel's contents in your reply. Say what it shows and move on.
    """

    model_config = ConfigDict(extra="forbid")

    spec: GenerativeUISpec = Field(description="The panel's component tree.")
    summary: Summary = Field(
        description="One short line naming what the panel shows, e.g. 'CI status for main'."
    )
    ui_id: UiId | None = Field(
        default=None,
        description=(
            "Omit to create a new panel. Pass the id returned by an earlier "
            "present_ui call to REPLACE that panel in place instead of adding "
            "another one below it."
        ),
    )


# Derived from the args model — the same seam ``submit_widget`` and the core
# spawn schema use, so the model-facing schema can never drift from validation.
PRESENT_UI_SCHEMA: dict[str, object] = pydantic_to_openai_tool(
    PresentUiArgs, name=PRESENT_UI_TOOL_ID
)


class GenerativeUIPayload(BaseModel):
    """Typed payload for the ``generative_ui`` event.

    Owned here rather than as an arm on ``mewbo_core.contracts.types.EventPayload`` — that
    union is generic infrastructure shared by every event kind, and a
    plugin-specific arm there would re-couple core to this vocabulary (the
    ``WidgetReadyPayload`` precedent). The snake_case wire shape is FROZEN: a
    console TS type mirrors it, so changing a key or dropping ``extra="forbid"``
    breaks the client.
    """

    model_config = ConfigDict(extra="forbid")

    ui_id: UiId
    session_id: str
    # The RENDERER-facing tree, always produced by ``GenerativeUISpec.to_wire``
    # — never hand-assembled. ``JsonValue`` is what keeps that promise checkable
    # at the boundary: a non-JSON value smuggled in here would otherwise only
    # fail much later, at the store or the SSE encoder.
    spec: dict[str, JsonValue]
    # Derived from the tree, not model-authored, so it inherits the tree's size
    # ceiling and needs no cap of its own.
    alt_text: str
    summary: str

    @field_validator("spec")
    @classmethod
    def _exact_wire_shape(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Require the frozen ``{"root": [...]}`` shape."""
        if set(value) != {"root"} or not isinstance(value["root"], list):
            raise ValueError(f"spec must be exactly {{'root': [...]}}, got {sorted(value)}")
        return value


class PresentUiTool:
    """Handles ``present_ui`` tool calls.

    Validates the tree through :class:`PresentUiArgs`, mints or reuses the
    ``ui_id`` upsert key, computes ``alt_text`` server-side from the tree's own
    ``to_text`` and emits one ``generative_ui`` event.
    """

    # Class-level attributes satisfy the ``SessionTool`` Protocol natively.
    tool_id: str = PRESENT_UI_TOOL_ID
    schema: dict[str, object] = PRESENT_UI_SCHEMA
    modes: frozenset[str] = DEFAULT_SESSION_TOOL_MODES

    # Sized for the FAILURE case, which is the only large result this tool
    # produces: a success is one line, but a rejected tree returns the Pydantic
    # error the model needs in order to fix it. The discriminated union keeps
    # that error to the matched variant rather than all eleven, so this is
    # generous rather than tight — and the registry's 2000-char default, which a
    # SessionTool inherits when it declares nothing, would truncate the
    # correction mid-sentence.
    max_result_chars: int = 8_000

    def __init__(
        self,
        *,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialize the handler.

        Args:
            session_id: Session identifier stamped on the emitted event.
            event_logger: Callback for emitting ``generative_ui`` events;
                usually ``agent_context.event_logger``.
        """
        self._session_id = session_id
        self._event_logger = event_logger

    def should_terminate_run(self) -> bool:
        """Never terminate — the panel renders off the event, not off the run ending."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); default parity with the Protocol.

        Defined explicitly because ``SessionTool`` is a structural Protocol: its
        default method bodies are NOT inherited by a standalone implementer, so
        a tool missing this crashes the loop's terminal poll with an
        ``AttributeError`` — which is exactly how ``submit_widget`` broke every
        call it served.
        """
        return "awaiting_approval"

    def _emit(self, event: Event) -> None:
        if self._event_logger is None:
            return
        try:
            self._event_logger(event)
        except Exception as exc:
            logging.warning("present_ui event emit failed: {}", exc)

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``present_ui`` tool call."""
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = PresentUiArgs.model_validate(raw)
        except ValidationError as exc:
            # Narrow on purpose, even though the tree is recursive and a
            # pathologically nested input looks like it could exhaust the stack
            # outside this arm: pydantic-core carries its own recursion guard
            # and reports the overflow AS a validation error (verified against a
            # 3,000-deep tree), so nothing is left uncovered. A broader catch
            # would only buy the chance to report a genuine bug in this module
            # to the model as if it were its own malformed arguments.
            return MockSpeaker(content=f"ERROR: invalid present_ui args: {exc}")

        ui_id = args.ui_id or f"gui-{uuid.uuid4().hex[:8]}"
        payload = GenerativeUIPayload(
            ui_id=ui_id,
            session_id=self._session_id,
            spec=args.spec.to_wire(),
            alt_text=args.spec.to_text(),
            summary=args.summary,
        )
        self._emit({"type": GENERATIVE_UI_EVENT, "payload": payload.model_dump()})

        # The result is a receipt, not an echo: the model just authored the tree
        # and re-reading it back would spend context on what it already knows.
        # The id is here because it is the ONE fact the model does not have.
        _, nodes = args.spec.measure()
        return MockSpeaker(
            content=(
                f"Presented UI {ui_id} ({nodes} nodes). "
                f'Pass ui_id="{ui_id}" to replace this panel instead of adding another.'
            )
        )


__all__ = [
    "GENERATIVE_UI_CAPABILITY",
    "GENERATIVE_UI_EVENT",
    "PRESENT_UI_SCHEMA",
    "PRESENT_UI_TOOL_ID",
    "UI_ID_PATTERN",
    "GenerativeUIPayload",
    "PresentUiArgs",
    "PresentUiTool",
]
