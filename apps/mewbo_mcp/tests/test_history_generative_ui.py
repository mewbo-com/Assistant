"""A presented UI panel must be READABLE to an agent that cannot render it.

``present_ui`` emits a ``generative_ui`` event whose ``spec`` is a component
tree only a React surface can draw, and whose ``alt_text`` is the same tree
rendered as prose precisely so every other surface has something to show. MCP is
the surface with no renderer at all, so it is the one that needs the alt text
most: the ``present_ui`` step it already sees is a bare receipt naming an id and
a node count, which tells a reading agent that a panel exists and nothing about
what it said.

These tests pin the degradation contract at the tier that carries it, the upsert
rule MCP shares with the console and the core assembler, and the two things the
projection must NOT do: inline the component tree, or let a malformed payload
reach the reader.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mewbo_mcp import tools
from mewbo_mcp.timeline import build_timeline


def run(coro):
    return asyncio.run(coro)


def _user(text: str, ts: str) -> dict[str, Any]:
    return {"type": "user", "ts": ts, "payload": {"text": text}}


def _assistant(text: str, ts: str) -> dict[str, Any]:
    return {"type": "assistant", "ts": ts, "payload": {"text": text}}


def _completion(reason: str, ts: str, **payload: Any) -> dict[str, Any]:
    return {"type": "completion", "ts": ts, "payload": {"done_reason": reason, **payload}}


def _tool_result(tool_id: str, ts: str, result: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "ts": ts,
        "payload": {"tool_id": tool_id, "operation": "set", "result": result, "success": True},
    }


def _generative_ui(ts: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ui_id": "gui-abc123",
        "session_id": "s1",
        "spec": {"root": [{"component": "Text", "props": {"text": "Deploys: 3 failed"}}]},
        "alt_text": "Deploys: 3 failed",
        "summary": "deploy status",
    }
    payload.update(overrides)
    return {"type": "generative_ui", "ts": ts, "payload": payload}


def _events(*events: dict[str, Any], **meta: Any) -> dict[str, Any]:
    return {"events": list(events), **meta}


def _panel_session(*extra: dict[str, Any]) -> dict[str, Any]:
    """A turn in which the agent presented one panel and then narrated."""
    return _events(
        _user("how are deploys?", "t0"),
        _tool_result("present_ui", "t1", "Presented UI gui-abc123 (1 nodes)."),
        _generative_ui("t2"),
        *extra,
        _assistant("Three deploys failed.", "t8"),
        _completion("completed", "t9"),
    )


# ---------------------------------------------------------------------------
# Turn reconstruction — the raw fact, before any tier projects it
# ---------------------------------------------------------------------------


def test_a_panel_is_a_card_row_not_a_step():
    """It carries no turn metadata, so it must not disturb turn reconstruction."""
    turns = build_timeline(_panel_session()["events"])

    assert len(turns) == 1
    assert turns[0].done_reason == "completed"
    # It is not a step — steps are ``tool_result`` events, and the panel's own
    # ``present_ui`` call is the step.
    assert turns[0].step_count == 1


def test_the_turn_carries_its_panels():
    turns = build_timeline(_panel_session()["events"])

    panels = turns[0].generative_ui
    assert [p["payload"]["ui_id"] for p in panels] == ["gui-abc123"]


def test_a_session_with_no_panel_carries_none():
    events = [_user("q", "t0"), _assistant("a", "t1"), _completion("completed", "t2")]

    assert build_timeline(events)[0].generative_ui == []


# ---------------------------------------------------------------------------
# The full tier — where a reader asks for a turn's content
# ---------------------------------------------------------------------------


def test_full_tier_surfaces_the_alt_text(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _panel_session())
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1))

    panel = out["generative_ui"][0]
    assert panel["ui_id"] == "gui-abc123"
    assert panel["alt_text"] == "Deploys: 3 failed"
    assert panel["summary"] == "deploy status"


def test_full_tier_drops_the_component_tree(fake_rest):
    """A caller with no renderer must not pay context for markup it can't use."""
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _panel_session())
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1))

    assert "spec" not in out["generative_ui"][0]


def test_full_tier_omits_the_key_when_no_panel_was_presented(fake_rest):
    """Append-when-present, like ``triggers``/``attachments`` beside it."""
    fake = fake_rest.on(
        "GET",
        "/api/sessions/s1/events",
        _events(_user("q", "t0"), _assistant("a", "t1"), _completion("completed", "t2")),
    )
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1))

    assert "generative_ui" not in out


def test_repeated_panels_are_all_surfaced_in_order(fake_rest):
    """``ui_id`` is an upsert key for a RENDERER; a transcript keeps every emit."""
    fake = fake_rest.on(
        "GET",
        "/api/sessions/s1/events",
        _panel_session(_generative_ui("t3", ui_id="gui-second", alt_text="second")),
    )
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1))

    assert [p["ui_id"] for p in out["generative_ui"]] == ["gui-abc123", "gui-second"]


def test_an_oversized_alt_text_is_capped(fake_rest):
    """A wide tree renders to proportionally wide prose — cap it like any field."""
    huge = "x" * (tools.SessionTools.STEP_FIELD_TRUNC + 500)
    fake = fake_rest.on(
        "GET",
        "/api/sessions/s1/events",
        _panel_session(_generative_ui("t3", ui_id="gui-big", alt_text=huge)),
    )
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1))

    capped = next(p for p in out["generative_ui"] if p["ui_id"] == "gui-big")["alt_text"]
    assert "truncated" in capped
    assert len(capped) < len(huge)


# --- the upsert contract (shared with the console, pinned by the core corpus) ---
#
# ``ui_id`` is the wire's DECLARED replace key: ``present_ui``'s own result tells
# the model to pass it back to replace a panel. So a refined panel is ONE panel,
# and every surface must agree. Reading the raw events here instead stacks
# near-copies, handing a reading agent a stale panel beside the current one with
# nothing marking it stale — the worst outcome for the one consumer that has no
# renderer to compare them in.


def test_a_refined_panel_upserts_rather_than_stacking(fake_rest):
    fake = fake_rest.on(
        "GET",
        "/api/sessions/s1/events",
        _panel_session(_generative_ui("t3", alt_text="Deploys: 3 failed")),  # same ui_id
    )
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1))

    assert len(out["generative_ui"]) == 1
    panel = out["generative_ui"][0]
    assert panel["alt_text"] == "Deploys: 3 failed"  # the LATEST content wins
    assert panel["ts"] == "t2"  # ...but the FIRST appearance's ts is kept


def test_a_panel_with_no_ui_id_is_dropped(fake_rest):
    """No key means nothing to upsert against — core and the console both drop it."""
    fake = fake_rest.on(
        "GET",
        "/api/sessions/s1/events",
        _events(
            _user("q", "t0"),
            {"type": "generative_ui", "ts": "t1", "payload": {"alt_text": "orphan"}},
            _completion("completed", "t2"),
        ),
    )
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1))

    assert "generative_ui" not in out


def test_a_malformed_payload_degrades_rather_than_raising(fake_rest):
    """A stored event is a trust boundary — a junk payload must not reach the reader."""
    fake = fake_rest.on(
        "GET",
        "/api/sessions/s1/events",
        _events(
            _user("q", "t0"),
            {"type": "generative_ui", "ts": "t1", "payload": None},
            _completion("completed", "t2"),
        ),
    )
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1))

    assert "generative_ui" not in out
