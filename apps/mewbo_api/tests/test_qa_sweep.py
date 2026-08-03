#!/usr/bin/env python3
"""Contract tests for the startup orphaned-QA-answer sweep.

Real ``JsonWikiStore`` + real ``SessionStore``/``SessionRuntime`` (no LLM, no
store mocks), an injected clock — never a patched wall clock. The sweep's
job: an answer left at ``status: "running"`` by a session that is confirmed
NOT running, whose current turn never even started emitting, gets closed as
an honest error. Everything else — a genuinely live session, a partial
emit, an answer outside the recency window, a missing session binding, or an
already-terminal answer — is left untouched, and the pass is idempotent
within one process.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mewbo_api.wiki.qa_sweep import QaAnswerSweeper
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.session.session_store import SessionStore
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import QaAnswer


def _now_fixed() -> datetime:
    """A stable 'now' well after any just-appended event (in-window)."""
    return datetime.now(timezone.utc)


def _runtime(tmp_path, wiki_store: JsonWikiStore) -> SessionRuntime:
    """A real SessionRuntime with the wiki store stapled on, mirroring init_wiki."""
    session_store = SessionStore(root_dir=str(tmp_path / "sessions"))
    runtime = SessionRuntime(session_store=session_store)
    runtime.wiki_store = wiki_store  # type: ignore[attr-defined]
    return runtime


def _touch_session(runtime: SessionRuntime, session_id: str) -> None:
    """Give the backing session at least one recent event (a real answer's
    session always has one — the QA playbook's context/user turn)."""
    runtime.session_store.append_event(
        session_id, {"type": "user", "payload": {"text": "question"}}
    )


def _qa_answer(
    wiki_store: JsonWikiStore, answer_id: str, session_id: str, status: str = "running"
) -> None:
    wiki_store.save_qa(QaAnswer(
        answer_id=answer_id, slug="org/repo", from_page_id="overview",
        summary_sources=[], model="gpt-oss-120b", blocks=[], status=status,
    ))
    wiki_store.attach_qa_session(answer_id, session_id)
    wiki_store.append_qa_event(answer_id, {"type": "meta", "answerId": answer_id})


def _sweeper(runtime, *, now=None, window=None) -> QaAnswerSweeper:
    kwargs: dict = {"now": now or _now_fixed}
    if window is not None:
        kwargs["window"] = window
    return QaAnswerSweeper(runtime, **kwargs)


def test_orphaned_silent_answer_settled_to_error(tmp_path):
    """A running answer whose session is confirmed not-running, zero blocks → closed error."""
    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = _runtime(tmp_path, wiki_store)
    session_id = runtime.session_store.create_session()
    _touch_session(runtime, session_id)
    _qa_answer(wiki_store, "a1", session_id)

    assert not runtime.is_running(session_id)  # nothing ever started a run

    settled = _sweeper(runtime).sweep()

    assert settled == 1
    snap = wiki_store.get_qa("a1")
    assert snap.status == "error"
    assert wiki_store.load_qa_events("a1")[-1]["type"] == "error"


def test_running_session_never_clobbered(tmp_path, monkeypatch):
    """A genuinely in-flight answer (its session reports running) is left alone."""
    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = _runtime(tmp_path, wiki_store)
    session_id = runtime.session_store.create_session()
    _touch_session(runtime, session_id)
    _qa_answer(wiki_store, "a1", session_id)
    monkeypatch.setattr(runtime, "is_running", lambda sid: True)

    settled = _sweeper(runtime).sweep()

    assert settled == 0
    assert wiki_store.get_qa("a1").status == "running"


def test_partial_emit_left_alone(tmp_path):
    """An answer with block_open events (a partial emit) is out of this sweep's scope."""
    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = _runtime(tmp_path, wiki_store)
    session_id = runtime.session_store.create_session()
    _touch_session(runtime, session_id)
    _qa_answer(wiki_store, "a1", session_id)
    wiki_store.append_qa_event(
        "a1",
        {"type": "block_open", "index": 0, "block": {"kind": "p", "text": "partial"}},
    )

    settled = _sweeper(runtime).sweep()

    assert settled == 0
    assert wiki_store.get_qa("a1").status == "running"


def test_answer_outside_recency_window_left_alone(tmp_path):
    """An answer whose backing session's last activity is outside the window is skipped."""
    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = _runtime(tmp_path, wiki_store)
    session_id = runtime.session_store.create_session()
    _touch_session(runtime, session_id)
    _qa_answer(wiki_store, "a1", session_id)

    def _far_future() -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=30)

    settled = _sweeper(runtime, now=_far_future, window=timedelta(days=7)).sweep()

    assert settled == 0
    assert wiki_store.get_qa("a1").status == "running"


def test_no_session_binding_left_alone(tmp_path):
    """An answer with no attached session is skipped, never crashes."""
    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = _runtime(tmp_path, wiki_store)
    wiki_store.save_qa(QaAnswer(
        answer_id="a1", slug="org/repo", from_page_id="overview",
        summary_sources=[], model="gpt-oss-120b", blocks=[],
    ))
    # Deliberately no attach_qa_session call.

    settled = _sweeper(runtime).sweep()

    assert settled == 0


def test_only_running_status_is_swept(tmp_path):
    """A complete/error/cancelled answer is never re-touched by the sweep."""
    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = _runtime(tmp_path, wiki_store)
    session_id = runtime.session_store.create_session()
    _touch_session(runtime, session_id)
    _qa_answer(wiki_store, "done1", session_id, status="complete")

    settled = _sweeper(runtime).sweep()

    assert settled == 0
    assert wiki_store.get_qa("done1").status == "complete"


def test_sweep_is_idempotent_within_one_process(tmp_path):
    """A second sweep() call in the same process settles nothing (the _done guard)."""
    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = _runtime(tmp_path, wiki_store)
    session_id = runtime.session_store.create_session()
    _touch_session(runtime, session_id)
    _qa_answer(wiki_store, "a1", session_id)

    sweeper = _sweeper(runtime)
    assert sweeper.sweep() == 1
    assert sweeper.sweep() == 0


def test_sweep_never_raises_on_a_broken_answer(tmp_path, monkeypatch):
    """A single answer's failure is logged and swallowed — others still settle."""
    wiki_store = JsonWikiStore(root_dir=tmp_path / "wiki")
    runtime = _runtime(tmp_path, wiki_store)
    session_id_bad = runtime.session_store.create_session()
    session_id_good = runtime.session_store.create_session()
    _touch_session(runtime, session_id_bad)
    _touch_session(runtime, session_id_good)
    _qa_answer(wiki_store, "bad", session_id_bad)
    _qa_answer(wiki_store, "good", session_id_good)

    real_get_qa_session = wiki_store.get_qa_session

    def _boom(answer_id: str):
        if answer_id == "bad":
            raise RuntimeError("store hiccup")
        return real_get_qa_session(answer_id)

    monkeypatch.setattr(wiki_store, "get_qa_session", _boom)

    settled = _sweeper(runtime).sweep()

    assert settled == 1
    assert wiki_store.get_qa("good").status == "error"
    assert wiki_store.get_qa("bad").status == "running"  # untouched, not crashed
