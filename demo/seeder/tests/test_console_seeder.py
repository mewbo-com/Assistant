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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock
import pytest
from mewbo_core.session.session_store import SessionStore
from mewbo_core.triggers.store import JsonTriggerStore
from mewbo_core.workspaces.project_store import MongoProjectStore
from mewbo_demo_seeder.models import (
    PlanDecisionEvent,
    PlanProposedEvent,
    SeedBundle,
    SeedManagedProject,
    SeedSession,
    SeedWorktree,
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


# ── Managed projects (the Workspace settings facet) ────────────────────────

_LEDGER_SYNC = "4f8b2c17-63ae-4d90-9c15-2a7e08b3d641"
_SHIPMENT_ROUTER = "a1c94d5e-70f2-4b83-8e6a-1d40c9b752fe"
_WORKTREE_BRANCH = "mewbo/main-9f2c1a"


def _project_bundle(*projects: dict) -> dict:
    """A minimal but complete bundle carrying only the given project rows."""
    return {
        "sessions": [
            {
                "id": "sess-1",
                "title": "A session",
                "model": "claude-sonnet-5",
                "offset_seconds": -60,
                "events": [
                    {"kind": "user", "at_seconds": 0, "text": "hi"},
                    {"kind": "completion", "at_seconds": 1, "task_result": "done"},
                ],
            }
        ],
        "projects": list(projects),
    }


def test_managed_project_builds_the_stores_own_record() -> None:
    """``to_project`` returns core's ``VirtualProject`` with rebased ISO stamps."""
    project = SeedManagedProject(
        id=_LEDGER_SYNC,
        name="ledger-sync",
        description="Nightly reconciliation.",
        path="/workspaces/acme/ledger-sync",
        created_at_offset=-86400,
        updated_at_offset=-3600,
    )
    record = project.to_project(_T0, parent=None)

    assert record.project_id == _LEDGER_SYNC
    assert record.path == "/workspaces/acme/ledger-sync"
    assert record.created_at == (_T0 - timedelta(seconds=86400)).isoformat()
    assert record.updated_at == (_T0 - timedelta(seconds=3600)).isoformat()
    # Fixed, not authorable: the pane's payload carries neither, so no shot
    # could ever disagree with them.
    assert record.path_source == "provided"
    assert record.folder_created is True
    assert record.is_worktree is False
    assert record.parent_project_id is None


def test_an_unedited_project_reports_updated_equal_to_created() -> None:
    """Omitting ``updated_at_offset`` means "never edited", not "edited at T0"."""
    record = SeedManagedProject(
        id=_LEDGER_SYNC, name="n", path="/p", created_at_offset=-600
    ).to_project(_T0, parent=None)
    assert record.updated_at == record.created_at


def test_worktree_derives_id_path_and_prose_from_its_parent() -> None:
    """Every worktree field except the branch comes from core's own rules."""
    parent = SeedManagedProject(
        id=_SHIPMENT_ROUTER,
        name="shipment-router",
        path="/workspaces/acme/shipment-router",
        created_at_offset=-86400,
    ).to_project(_T0, parent=None)
    worktree = SeedWorktree(
        parent=_SHIPMENT_ROUTER, branch=_WORKTREE_BRANCH, created_at_offset=-3600
    )
    record = worktree.to_project(_T0, parent=parent)

    assert record.project_id == f"wt:{_SHIPMENT_ROUTER}:mewbo-main-9f2c1a"
    assert record.project_id == worktree.project_id
    assert record.path == (
        "/workspaces/acme/shipment-router/.mewbo/worktrees/mewbo-main-9f2c1a"
    )
    # The phrasing ``ProjectStoreBase._persist_worktree`` writes, not a bundle
    # string — a row the product cannot emit is a wrong screenshot.
    assert record.name == _WORKTREE_BRANCH
    assert record.description == f"Worktree on branch '{_WORKTREE_BRANCH}'"
    assert record.is_worktree is True
    assert record.branch == _WORKTREE_BRANCH
    assert record.parent_project_id == _SHIPMENT_ROUTER


def test_worktree_without_its_parent_record_fails_loudly() -> None:
    """There is no honest path for a worktree whose parent record is missing."""
    worktree = SeedWorktree(
        parent=_SHIPMENT_ROUTER, branch=_WORKTREE_BRANCH, created_at_offset=-60
    )
    with pytest.raises(ValueError, match="needs its parent project record"):
        worktree.to_project(_T0, parent=None)


def test_worktree_with_an_unknown_parent_is_rejected_at_load() -> None:
    """An orphan worktree renders a bare id where the parent's name belongs."""
    with pytest.raises(ValidationError, match="references unknown parent project"):
        SeedBundle.model_validate(
            _project_bundle(
                {
                    "kind": "worktree",
                    "parent": "nope",
                    "branch": _WORKTREE_BRANCH,
                    "created_at_offset": -60,
                }
            )
        )


def test_a_branch_with_no_slug_safe_characters_is_rejected_at_load() -> None:
    """``slugify_branch`` names the directory AND half the id — fail here, not mid-seed."""
    with pytest.raises(ValidationError):
        SeedWorktree(parent=_SHIPMENT_ROUTER, branch="///", created_at_offset=-60)


def test_an_edit_cannot_predate_the_creation_it_edits() -> None:
    """A backwards ``updated_at`` would render a row edited before it existed."""
    with pytest.raises(ValidationError, match="precedes"):
        SeedManagedProject(
            id=_LEDGER_SYNC,
            name="n",
            path="/p",
            created_at_offset=-600,
            updated_at_offset=-6000,
        )


def test_write_order_puts_parents_first_whatever_the_bundle_order() -> None:
    """A worktree authored beside its parent must still be written after it."""
    bundle = SeedBundle.model_validate(
        _project_bundle(
            {
                "kind": "worktree",
                "parent": _SHIPMENT_ROUTER,
                "branch": _WORKTREE_BRANCH,
                "created_at_offset": -60,
            },
            {
                "kind": "project",
                "id": _SHIPMENT_ROUTER,
                "name": "shipment-router",
                "path": "/workspaces/acme/shipment-router",
                "created_at_offset": -600,
            },
        )
    )
    assert [p.parent_id for p in bundle.projects_in_write_order] == [
        None,
        _SHIPMENT_ROUTER,
    ]


def test_bundle_carries_four_workspaces_and_one_worktree(bundle: SeedBundle) -> None:
    """The committed bundle populates the Workspace facet with a worktree row.

    A worktree exercises the console's ``ProjectLabel`` worktree path (parent
    repo name + branch), which four identical rows never would.
    """
    tops = [p for p in bundle.projects if isinstance(p, SeedManagedProject)]
    worktrees = [p for p in bundle.projects if isinstance(p, SeedWorktree)]
    assert len(tops) == 4
    assert len(worktrees) == 1
    assert worktrees[0].parent == _SHIPMENT_ROUTER
    # Wholly fictional, and NOT a relabelled copy of a wiki-gallery project.
    assert [p.name for p in tops] == [
        "ledger-sync",
        "shipment-router",
        "storefront-checkout",
        "depot-telemetry",
    ]
    assert all(p.path.startswith("/workspaces/acme/") for p in tops)
    assert all(p.description for p in tops)


# ── DemoSeeder round-trip for projects (mongomock-backed project store) ────


@pytest.fixture
def project_store(monkeypatch: pytest.MonkeyPatch) -> MongoProjectStore:
    """A real ``MongoProjectStore`` over mongomock.

    Unlike the wiki/search stores this one builds its own ``MongoClient`` from a
    uri, so the injection point is ``pymongo.MongoClient`` itself — the same
    monkeypatch the session/trigger stores need.
    """
    monkeypatch.setattr("pymongo.MongoClient", mongomock.MongoClient)
    return MongoProjectStore("mongodb://demo-seeder-test", "test_projects")


def _seed_projects(
    tmp_path: Path, store: MongoProjectStore, raw: dict
) -> None:
    """Drive the REAL ``DemoSeeder.seed()`` for a project-carrying bundle.

    Drops ``api_keys`` so these cases exercise the project leg alone — the
    seeder refuses a key-carrying bundle with no key store, which
    ``test_api_keys_without_a_key_store_fail_loudly`` already pins.
    """
    raw = {k: v for k, v in raw.items() if k != "api_keys"}
    DemoSeeder(
        session_store=SessionStore(root_dir=str(tmp_path / "sessions")),
        trigger_store=JsonTriggerStore(data_file=str(tmp_path / "triggers.json")),
        bundle=SeedBundle.model_validate(raw),
        t0=_T0,
        project_store=store,
    ).seed()


def test_seed_is_readable_through_the_stores_own_list_projects(
    tmp_path: Path, project_store: MongoProjectStore
) -> None:
    """The caller's read seam sees every seeded row, worktree linkage included."""
    _seed_projects(
        tmp_path,
        project_store,
        json.loads(_BUNDLE_PATH.read_text(encoding="utf-8")),
    )

    projects = project_store.list_projects()
    assert len(projects) == 5
    by_id = {p.project_id: p for p in projects}
    ledger = by_id[_LEDGER_SYNC]
    assert ledger.name == "ledger-sync"
    assert ledger.path == "/workspaces/acme/ledger-sync"
    assert ledger.created_at == (_T0 - timedelta(seconds=5184000)).isoformat()

    # The worktree resolves through the store's own worktree seam, which is
    # what ``GET /api/projects`` and the console's ProjectLabel both read.
    worktrees = project_store.list_worktrees(_SHIPMENT_ROUTER)
    assert [w.branch for w in worktrees] == [_WORKTREE_BRANCH]
    assert worktrees[0].parent_project_id == _SHIPMENT_ROUTER
    assert project_store.get_project(worktrees[0].project_id) is not None


def test_reseeding_projects_is_byte_identical(
    tmp_path: Path, project_store: MongoProjectStore
) -> None:
    """A re-seed replaces by id rather than duplicating — the zero-diff contract."""
    raw = json.loads(_BUNDLE_PATH.read_text(encoding="utf-8"))
    _seed_projects(tmp_path, project_store, raw)
    first = [p.__dict__ for p in project_store.list_projects()]
    _seed_projects(tmp_path, project_store, raw)
    assert [p.__dict__ for p in project_store.list_projects()] == first


def test_projects_without_a_project_store_fail_loudly(tmp_path: Path) -> None:
    """A silent skip would render the Workspace facet's EMPTY state in the shot."""
    seeder = DemoSeeder(
        session_store=SessionStore(root_dir=str(tmp_path / "sessions")),
        trigger_store=JsonTriggerStore(data_file=str(tmp_path / "triggers.json")),
        bundle=SeedBundle.model_validate(
            _project_bundle(
                {
                    "kind": "project",
                    "id": _LEDGER_SYNC,
                    "name": "ledger-sync",
                    "path": "/workspaces/acme/ledger-sync",
                    "created_at_offset": -600,
                }
            )
        ),
        t0=_T0,
    )
    with pytest.raises(ValueError, match="no project_store was injected"):
        seeder.seed()
