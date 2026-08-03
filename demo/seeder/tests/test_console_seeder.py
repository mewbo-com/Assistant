#!/usr/bin/env python3
"""Unit tests for the console bundle's seed-event kinds (demo-as-code).

Covers :class:`WidgetReadyEvent`: its
``to_event()`` emits the exact FROZEN wire shape
(``mewbo_core.builtin_plugins.widget_builder.submit_widget.WidgetReadyPayload``
— ``files`` keyed by exactly ``app.py``/``data.json``, plus the now-shared
``session_id`` keyword), its ``data_json`` field validator fails fast on
malformed JSON at bundle-load, and the committed ``console-poc.json`` bundle
carries the seeded widget session end-to-end. The final test drives the real
``DemoSeeder.seed()`` against filesystem-backed stores (``SessionStore`` /
``JsonTriggerStore``) — no mongomock needed, since those drivers take an
explicit local path — asserting the CALLER's read seam (``load_transcript``)
sees the fully-inlined event, per the repo's "meaningful tests" principle.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mewbo_core.session.session_store import SessionStore
from mewbo_core.triggers.store import JsonTriggerStore
from mewbo_demo_seeder.models import (
    PlanDecisionEvent,
    PlanProposedEvent,
    SeedBundle,
    SeedSession,
    UserQuestionAnsweredEvent,
    UserQuestionEvent,
    WidgetReadyEvent,
)
from mewbo_demo_seeder.seeder import DemoSeeder
from pydantic import ValidationError

_BUNDLE_PATH = Path(__file__).resolve().parents[1] / "bundles" / "console-poc.json"
_T0 = datetime(2026, 7, 14, 9, 30, 0, tzinfo=timezone.utc)
_WIDGET_SESSION = "demo-trending-acme-repos-widget"


# ── to_event() emits the exact frozen WidgetReadyPayload shape ─────────────


def test_to_event_emits_frozen_widget_ready_payload() -> None:
    """``files`` carries EXACTLY ``app.py``/``data.json``; ``session_id`` is set."""
    event = WidgetReadyEvent(
        at_seconds=5,
        widget_id="widget_test",
        app_py="import streamlit as st\nst.write('hi')\n",
        data_json='{"a": 1}',
        requirements=["pandas"],
        summary="A test widget.",
    )
    doc = event.to_event(
        _T0, session_model="claude-sonnet-5", agent_id="agent123", session_id="sess-abc"
    )

    assert doc["type"] == "widget_ready"
    assert doc["ts"] == _T0.isoformat()
    payload = doc["payload"]
    assert set(payload["files"]) == {"app.py", "data.json"}
    assert payload["files"]["app.py"] == event.app_py
    assert payload["files"]["data.json"] == event.data_json
    assert payload["session_id"] == "sess-abc"
    assert payload["widget_id"] == "widget_test"
    assert payload["requirements"] == ["pandas"]
    assert payload["summary"] == "A test widget."


# ── data_json field validator (fail fast at bundle-load) ───────────────────


def test_data_json_rejects_non_json() -> None:
    """A malformed ``data_json`` string is a clean ``ValidationError`` at load."""
    with pytest.raises(ValidationError):
        WidgetReadyEvent(at_seconds=0, widget_id="w1", app_py="pass", data_json="not json")


def test_data_json_accepts_valid_json() -> None:
    """A well-formed JSON string validates and round-trips unchanged."""
    event = WidgetReadyEvent(
        at_seconds=0, widget_id="w1", app_py="pass", data_json='{"x": [1, 2]}'
    )
    assert event.data_json == '{"x": [1, 2]}'


# ── The committed bundle carries the widget session end-to-end ─────────────


@pytest.fixture
def bundle() -> SeedBundle:
    """The committed demo bundle, validated through the Pydantic trust boundary."""
    raw = json.loads(_BUNDLE_PATH.read_text(encoding="utf-8"))
    return SeedBundle.model_validate(raw)


def test_bundle_carries_the_widget_session(bundle: SeedBundle) -> None:
    """The real ``console-poc.json`` bundle seeds one widget with real content."""
    session = next(s for s in bundle.sessions if s.id == _WIDGET_SESSION)
    widget_events = [e for e in session.events if isinstance(e, WidgetReadyEvent)]
    assert len(widget_events) == 1
    widget = widget_events[0]
    assert widget.app_py.strip() != ""
    data = json.loads(widget.data_json)
    assert isinstance(data, dict)
    # Trimmed to 6 on purpose so the native 2x3 card grid fits the console's
    # ~60vh widget cap without an internal scroll (see demo/CLAUDE.md).
    assert len(data["repositories"]) == 6


# ── DemoSeeder round-trip (filesystem-backed stores, no mongomock needed) ──


def test_seed_writes_widget_ready_event_with_files_inlined(tmp_path: Path) -> None:
    """A minimal bundle with one widget session seeds a fully self-contained event."""
    minimal = SeedBundle.model_validate(
        {
            "sessions": [
                {
                    "id": "widget-sess",
                    "title": "A widget session",
                    "model": "claude-sonnet-5",
                    "offset_seconds": -60,
                    "events": [
                        {"kind": "user", "at_seconds": 0, "text": "Build me a widget."},
                        {
                            "kind": "widget_ready",
                            "at_seconds": 5,
                            "widget_id": "widget_demo",
                            "app_py": "import streamlit as st\nst.write('hi')\n",
                            "data_json": '{"n": 1}',
                            "summary": "A tiny widget.",
                        },
                        {
                            "kind": "completion",
                            "at_seconds": 10,
                            "task_result": "Built the widget.",
                        },
                    ],
                }
            ]
        }
    )
    session_store = SessionStore(root_dir=str(tmp_path / "sessions"))
    trigger_store = JsonTriggerStore(data_file=str(tmp_path / "triggers.json"))
    DemoSeeder(
        session_store=session_store, trigger_store=trigger_store, bundle=minimal, t0=_T0
    ).seed()

    events = session_store.load_transcript("widget-sess")
    widget_event = next(e for e in events if e["type"] == "widget_ready")
    assert widget_event["payload"]["session_id"] == "widget-sess"
    assert widget_event["payload"]["files"] == {
        "app.py": "import streamlit as st\nst.write('hi')\n",
        "data.json": '{"n": 1}',
    }


# ── Plan-mode seed kinds (shot 03) ─────────────────────────────────────────

_PLAN_SESSION = "demo-scoped-api-keys-plan"


def test_plan_decision_emits_the_typed_event_and_fold_key() -> None:
    """A decision emits ``plan_<decision>`` carrying the revision the console folds on."""
    event = PlanDecisionEvent(at_seconds=3, revision=1, decision="rejected")
    doc = event.to_event(
        _T0, session_model="claude-sonnet-5", agent_id="agent123", session_id="sess-abc"
    )
    assert doc["type"] == "plan_rejected"
    assert doc["payload"] == {"revision": 1}


def test_plan_proposed_omits_absent_optional_fields() -> None:
    """``summary``/``plan_path`` are absent rather than null when unset."""
    doc = PlanProposedEvent(at_seconds=2, revision=2, content="# Plan").to_event(
        _T0, session_model="m", agent_id="a", session_id="s"
    )
    assert doc["payload"] == {"revision": 2, "content": "# Plan"}


def test_decision_without_a_matching_proposal_is_rejected_at_load() -> None:
    """The console would silently leave the card pending — fail at bundle load instead."""
    with pytest.raises(ValidationError, match="before \\(or without\\) proposing it"):
        SeedSession.model_validate(
            {
                "id": "s1",
                "title": "t",
                "model": "m",
                "offset_seconds": -60,
                "events": [
                    {"kind": "plan_decision", "at_seconds": 0, "revision": 1,
                     "decision": "approved"},
                    {"kind": "completion", "at_seconds": 1, "task_result": "done"},
                ],
            }
        )


def test_bundle_plan_session_leaves_revision_2_pending(bundle: SeedBundle) -> None:
    """Revision 1 is decided, revision 2 is NOT — that pending card is the shot."""
    session = next(s for s in bundle.sessions if s.id == _PLAN_SESSION)
    proposed = {e.revision for e in session.events if isinstance(e, PlanProposedEvent)}
    decided = {e.revision for e in session.events if isinstance(e, PlanDecisionEvent)}
    assert proposed == {1, 2}
    assert decided == {1}


# ── Ask-user question seed kinds (the ask-user shot) ───────────────────────

_ASK_USER_SESSION = "demo-ingest-queue-cutover"


def test_user_question_emits_the_dispatcher_payload_shape() -> None:
    """All five keys, and the questions re-serialized through the core models.

    The api dispatcher dumps `AskUserQuestionArgs.questions`, so a group that
    omits `options`/`multi_select` in the bundle still reaches the console with
    both keys filled — which is what the timeline reducer reads.
    """
    event = UserQuestionEvent(
        at_seconds=6,
        call_id="call-1",
        call_token="demo-token",
        questions=[{"header": "Scope", "question": "Which one?"}],
    )
    doc = event.to_event(
        _T0, session_model="claude-sonnet-5", agent_id="agent123", session_id="sess-abc"
    )

    assert doc["type"] == "user_question"
    payload = doc["payload"]
    assert set(payload) == {
        "call_id",
        "call_token",
        "questions",
        "timeout_seconds",
        "notes_placeholder",
    }
    assert payload["questions"] == [
        {"header": "Scope", "question": "Which one?", "options": [], "multi_select": False}
    ]
    assert payload["timeout_seconds"] is None
    assert payload["notes_placeholder"] is None


def test_user_question_defers_every_rule_to_the_core_arg_model() -> None:
    """A one-option group is refused at bundle load, by the tool's own validator."""
    with pytest.raises(ValidationError, match="options must be empty or hold 2-4 choices"):
        UserQuestionEvent(
            at_seconds=0,
            call_id="call-1",
            call_token="demo-token",
            questions=[
                {"header": "Scope", "question": "Which one?", "options": [{"label": "Only"}]}
            ],
        )


def test_answered_event_carries_the_null_answer_fields() -> None:
    """A run-stopped outcome writes the same key set the api does, answers null."""
    doc = UserQuestionAnsweredEvent(
        at_seconds=9, call_id="call-1", outcome="timed_out"
    ).to_event(_T0, session_model="m", agent_id="a", session_id="s")
    assert doc["type"] == "user_question_answered"
    assert doc["payload"] == {
        "call_id": "call-1",
        "outcome": "timed_out",
        "answered_via": None,
        "answers": None,
        "notes": None,
        "delivery": None,
    }


def test_answered_without_a_matching_group_is_rejected_at_load() -> None:
    """The console's fold would silently leave the card pending — fail here instead."""
    with pytest.raises(ValidationError, match="before \\(or without\\) asking it"):
        SeedSession.model_validate(
            {
                "id": "s1",
                "title": "t",
                "model": "m",
                "offset_seconds": -60,
                "events": [
                    {
                        "kind": "user_question_answered",
                        "at_seconds": 0,
                        "call_id": "nope",
                        "outcome": "timed_out",
                    },
                    {"kind": "completion", "at_seconds": 1, "task_result": "done"},
                ],
            }
        )


def test_bundle_ask_user_session_covers_the_three_card_states(bundle: SeedBundle) -> None:
    """One timed-out group, one multi-select+notes group, one bounded pending group.

    These three states ARE the shot: a card whose run stopped waiting but stays
    answerable, the group-level notes box, and the bounded-wait hint (which only
    a still-pending group renders).
    """
    session = next(s for s in bundle.sessions if s.id == _ASK_USER_SESSION)
    asked = [e for e in session.events if isinstance(e, UserQuestionEvent)]
    resolved = [e for e in session.events if isinstance(e, UserQuestionAnsweredEvent)]
    assert len(asked) == 3
    assert [e.outcome for e in resolved] == ["timed_out"]

    # The timed-out group was BOUNDED and its hint is suppressed anyway once the
    # run stops waiting, so the pending bounded group is a separate one.
    timed_out = next(e for e in asked if e.call_id == resolved[0].call_id)
    assert timed_out.timeout_seconds is not None

    pending = [e for e in asked if e.call_id != resolved[0].call_id]
    assert any(e.notes_placeholder for e in pending)
    assert any(e.timeout_seconds for e in pending)
    assert any(q.get("multi_select") for e in pending for q in e.questions)


# ── Issued API keys (shot 02 — Security settings) ──────────────────────────


def test_bundle_api_keys_are_deterministic_and_invented(bundle: SeedBundle) -> None:
    """Ids/labels are explicit (not minted) so the issued-keys list is byte-stable."""
    assert [k.id for k in bundle.api_keys] == [
        "8c41d2f6a95b47e0b3d18f2a6c705e94",
        "b07e5a3c1d8f42a69e40cb27f5163d8a",
    ]
    assert all(k.created_at_offset < 0 for k in bundle.api_keys)


def test_api_keys_without_a_key_store_fail_loudly(tmp_path: Path) -> None:
    """A silent skip would render an EMPTY issued-keys list in the shot."""
    seeder = DemoSeeder(
        session_store=SessionStore(root_dir=str(tmp_path / "sessions")),
        trigger_store=JsonTriggerStore(data_file=str(tmp_path / "triggers.json")),
        bundle=SeedBundle.model_validate(json.loads(_BUNDLE_PATH.read_text())),
        t0=_T0,
    )
    with pytest.raises(ValueError, match="no key_store was injected"):
        seeder.seed()
