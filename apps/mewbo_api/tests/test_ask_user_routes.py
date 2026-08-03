"""Route + dispatcher contract tests for ask-user questions.

Drives the real Flask backend test client against a temp-store runtime (same
shape as ``test_device_tools_routes.py``). Covers:

1. The ``ask_user`` capability in ``context.client_capabilities`` binds an
   ``AskUserQuestionTool`` into ``extra_session_tools`` at ``/query`` AND at
   the ``/message`` re-engage re-derivation; absence binds nothing.
2. ``QuestionPendingCalls`` semantics: answered-once, token check, semantic
   answer validation against the stored questions, withdraw-vs-resolve race.
3. A full dispatch round trip through ``ApiQuestionDispatcher`` and the
   ``POST .../questions/<call_id>/answer`` route, without any LLM — plus the
   steer-supersede / interrupt / cancel / timeout wake paths.
4. ``QuestionAnswerRouter``: a question stays answerable after its waiter
   departed, and the late answer reaches the model as a new user turn (steered
   into a live run, or starting one on an idle session).
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time

import pytest
from mewbo_core.loop.session_runtime import RunHandle
from mewbo_core.tooling.ask_user import AskUserQuestionArgs, QuestionAnswerItem

API_KEY = "test-master-token-ask-user"

_QUESTIONS_CONTEXT = {"client_capabilities": ["ask_user"]}

_QUESTIONS = [
    {
        "header": "Scope",
        "question": "Which scope?",
        "options": [{"label": "Root only"}, {"label": "All agents"}],
    },
    {"header": "Rollout", "question": "Anything else?"},
]

_ARGS = AskUserQuestionArgs.model_validate({"questions": _QUESTIONS})

_TIMED_ARGS = AskUserQuestionArgs.model_validate(
    {
        "questions": _QUESTIONS,
        "timeout_seconds": 5,
        "notes_placeholder": "Anything else about the deploy?",
    }
)


class _StepClock:
    """Monotonic stub that jumps *step* seconds on every read.

    An injected clock is what lets a deadline test assert expiry without a
    single ``sleep`` — ``reads`` is the proof: two reads means the loop set its
    deadline, found it spent, and never polled.
    """

    def __init__(self, step: float = 1000.0) -> None:
        self.reads = 0
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        self.reads += 1
        now = self._now
        self._now += self._step
        return now


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from mewbo_core.loop.session_runtime import SessionRuntime
    from mewbo_core.session.session_event_bus import reset_session_event_bus_for_tests
    from mewbo_core.session.session_store import SessionStore

    reset_session_event_bus_for_tests()

    import mewbo_api.backend as backend
    from mewbo_api.ask_user import reset_pending_questions_for_tests

    reset_pending_questions_for_tests()
    monkeypatch.setattr(backend, "MASTER_API_TOKEN", API_KEY, raising=False)
    store = SessionStore(root_dir=str(tmp_path / "sessions"))
    rt = SessionRuntime(session_store=store)
    monkeypatch.setattr(backend, "runtime", rt, raising=False)
    backend.app.config["TESTING"] = True
    return backend.app.test_client(), rt, backend


def _headers(**extra) -> dict:
    return {"X-API-KEY": API_KEY, **extra}


def _wait_for_event(rt, session_id: str, kind: str, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in rt.load_events(session_id):
            if evt.get("type") == kind:
                return evt["payload"]
        time.sleep(0.02)
    raise AssertionError(f"{kind} event never appeared")


def _expire_question(rt, session_id: str, args: AskUserQuestionArgs = _TIMED_ARGS) -> dict:
    """Drive a bounded question to expiry; return its durable `user_question` payload.

    The realistic way to reach the late-answer path: the waiter is gone, the
    event is not.
    """
    from mewbo_api.ask_user import ApiQuestionDispatcher

    dispatcher = ApiQuestionDispatcher(runtime=rt, monotonic=_StepClock())
    result = asyncio.run(dispatcher.dispatch(session_id, args))
    assert result.outcome == "timed_out"
    return _wait_for_event(rt, session_id, "user_question")


def _idle_handle(**overrides) -> RunHandle:
    fields = {
        "cancel_event": threading.Event(),
        "started_at": "2026-07-18T00:00:00+00:00",
        "message_queue": queue.Queue(),
        "interrupt_step": threading.Event(),
    }
    fields.update(overrides)
    return RunHandle(**fields)


# ---------------------------------------------------------------------------
# Startup registration + capability-gated binding
# ---------------------------------------------------------------------------


def test_question_dispatcher_registered_at_api_startup():
    from mewbo_core.tooling.ask_user import QuestionDispatcher

    assert QuestionDispatcher.available() is True


def test_query_with_ask_user_capability_binds_tool(client, monkeypatch):
    c, rt, backend = client
    from mewbo_core.tooling.ask_user import AskUserQuestionTool

    captured: dict = {}
    monkeypatch.setattr(
        rt, "start_async", lambda **kw: captured.update(kw) or "sid:r1"
    )

    sid = c.post("/api/sessions", json={}, headers=_headers()).get_json()["session_id"]
    resp = c.post(
        f"/api/sessions/{sid}/query",
        json={"query": "hi", "context": dict(_QUESTIONS_CONTEXT)},
        headers=_headers(),
    )
    assert resp.status_code == 202
    tools = captured.get("extra_session_tools")
    assert tools is not None and len(tools) == 1
    assert isinstance(tools[0], AskUserQuestionTool)


def test_query_without_capability_binds_nothing(client, monkeypatch):
    c, rt, backend = client
    captured: dict = {}
    monkeypatch.setattr(
        rt, "start_async", lambda **kw: captured.update(kw) or "sid:r1"
    )

    sid = c.post("/api/sessions", json={}, headers=_headers()).get_json()["session_id"]
    resp = c.post(
        f"/api/sessions/{sid}/query",
        json={"query": "hi", "context": {"client_capabilities": ["wiki"]}},
        headers=_headers(),
    )
    assert resp.status_code == 202
    assert captured.get("extra_session_tools") == []


def test_message_reengage_rebuilds_tool_from_persisted_context(client, monkeypatch):
    c, rt, backend = client
    from mewbo_core.tooling.ask_user import AskUserQuestionTool

    captured: dict = {}
    monkeypatch.setattr(
        rt, "start_async", lambda **kw: captured.update(kw) or f"{kw['session_id']}:r1"
    )

    sid = c.post(
        "/api/sessions",
        json={"context": dict(_QUESTIONS_CONTEXT)},
        headers=_headers(),
    ).get_json()["session_id"]
    resp = c.post(
        f"/api/sessions/{sid}/message", json={"text": "again"}, headers=_headers()
    )
    assert resp.status_code == 200
    tools = captured.get("extra_session_tools")
    assert tools is not None and len(tools) == 1
    assert isinstance(tools[0], AskUserQuestionTool)


# ---------------------------------------------------------------------------
# QuestionPendingCalls — registry semantics
# ---------------------------------------------------------------------------


class TestQuestionPendingCalls:
    def _registry(self):
        from mewbo_api.ask_user import QuestionPendingCalls

        return QuestionPendingCalls()

    def test_resolve_happy_path_sets_event(self):
        reg = self._registry()
        event = reg.create("s", "c", "tok", _ARGS)
        outcome, _ = reg.resolve(
            "s",
            "c",
            "tok",
            [QuestionAnswerItem(selected_indexes=[0]), QuestionAnswerItem(text="no")],
            answered_via="console",
        )
        assert outcome == "ok"
        assert event.is_set()
        entry = reg.take("s", "c")
        assert entry is not None
        assert entry.answered_via == "console"
        assert entry.answers[0].selected_indexes == [0]

    def test_unknown_and_bad_token(self):
        reg = self._registry()
        reg.create("s", "c", "tok", _ARGS)
        assert reg.resolve("s", "nope", "tok", [], answered_via=None)[0] == "not_found"
        assert (
            reg.resolve("s", "c", "wrong", [QuestionAnswerItem(text="x")], answered_via=None)[0]
            == "bad_token"
        )

    def test_double_resolve_conflicts(self):
        reg = self._registry()
        reg.create("s", "c", "tok", _ARGS)
        answers = [QuestionAnswerItem(text="a"), QuestionAnswerItem(text="b")]
        assert reg.resolve("s", "c", "tok", answers, answered_via=None)[0] == "ok"
        assert reg.resolve("s", "c", "tok", answers, answered_via=None)[0] == "conflict"

    @pytest.mark.parametrize(
        "bad_answers",
        [
            [QuestionAnswerItem(text="only one")],  # count mismatch
            [QuestionAnswerItem(selected_indexes=[5]), QuestionAnswerItem(text="x")],  # OOB
            [QuestionAnswerItem(selected_indexes=[0, 1]), QuestionAnswerItem(text="x")],  # arity
            [QuestionAnswerItem(text="x"), QuestionAnswerItem(selected_indexes=[0])],  # free-text q
        ],
    )
    def test_semantically_invalid_answers_do_not_consume(self, bad_answers):
        reg = self._registry()
        event = reg.create("s", "c", "tok", _ARGS)
        outcome, detail = reg.resolve("s", "c", "tok", bad_answers, answered_via=None)
        assert outcome == "invalid" and detail
        assert not event.is_set()
        # Still answerable with a correct body afterwards.
        good = [QuestionAnswerItem(selected_indexes=[1]), QuestionAnswerItem(text="ok")]
        assert reg.resolve("s", "c", "tok", good, answered_via=None)[0] == "ok"

    def test_withdraw_unanswered_removes_entry(self):
        reg = self._registry()
        reg.create("s", "c", "tok", _ARGS)
        assert reg.withdraw("s", "c") is True
        assert (
            reg.resolve("s", "c", "tok", [QuestionAnswerItem(text="x")], answered_via=None)[0]
            == "not_found"
        )

    def test_withdraw_after_resolve_loses_the_race(self):
        """The race guard: an answer that landed first must WIN — withdraw
        returns False and the entry (with the user's answers) survives for
        the dispatcher's read."""
        reg = self._registry()
        reg.create("s", "c", "tok", _ARGS)
        answers = [QuestionAnswerItem(text="a"), QuestionAnswerItem(text="b")]
        assert reg.resolve("s", "c", "tok", answers, answered_via="aura")[0] == "ok"
        assert reg.withdraw("s", "c") is False
        entry = reg.take("s", "c")
        assert entry is not None and entry.answered_via == "aura"

    def test_take_unconsumed_returns_none(self):
        reg = self._registry()
        reg.create("s", "c", "tok", _ARGS)
        assert reg.take("s", "c") is None


# ---------------------------------------------------------------------------
# Full dispatch round trip — no LLM
# ---------------------------------------------------------------------------


def test_full_answer_round_trip_via_route(client):
    c, rt, backend = client
    from mewbo_api.ask_user import ApiQuestionDispatcher

    session_id = rt.resolve_session()
    dispatcher = ApiQuestionDispatcher(runtime=rt)

    result_box: dict = {}

    def _run_dispatch():
        result_box["result"] = asyncio.run(dispatcher.dispatch(session_id, _ARGS))

    thread = threading.Thread(target=_run_dispatch)
    thread.start()
    try:
        payload = _wait_for_event(rt, session_id, "user_question")
        assert payload["questions"][0]["header"] == "Scope"
        assert payload["questions"][0]["options"][0]["label"] == "Root only"
        call_id, token = payload["call_id"], payload["call_token"]
        url = f"/api/sessions/{session_id}/questions/{call_id}/answer"

        # Wrong token → 403; unknown call → 404; malformed body → 400.
        resp = c.post(
            url,
            json={"call_token": "wrong", "answers": [{"text": "x"}, {"text": "y"}]},
            headers=_headers(),
        )
        assert resp.status_code == 403
        resp = c.post(
            f"/api/sessions/{session_id}/questions/unknown/answer",
            json={"call_token": token, "answers": [{"text": "x"}, {"text": "y"}]},
            headers=_headers(),
        )
        assert resp.status_code == 404
        resp = c.post(url, json={"call_token": token}, headers=_headers())
        assert resp.status_code == 400
        resp = c.post(
            url,
            json={"call_token": token, "answers": [{"text": "x", "selected_indexes": [0]}]},
            headers=_headers(),
        )
        assert resp.status_code == 400  # XOR violated → item-level 400

        # Semantically wrong (count mismatch) → 422 with an actionable message.
        resp = c.post(
            url,
            json={"call_token": token, "answers": [{"text": "only one"}]},
            headers=_headers(),
        )
        assert resp.status_code == 422
        assert "expected 2 answer" in resp.get_json()["message"]

        # Correct → 200; the blocked dispatch resolves with the answers.
        resp = c.post(
            url,
            json={
                "call_token": token,
                "answers": [{"selected_indexes": [1]}, {"text": "ship it"}],
            },
            headers=_headers(**{"X-Mewbo-Surface": "console"}),
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"resolved": True, "delivery": "run"}

        thread.join(timeout=5)
        assert not thread.is_alive()
        result = result_box["result"]
        assert result.outcome == "answered"
        assert result.answered_via == "console"
        assert result.answers[0].selected_indexes == [1]
        assert result.answers[1].text == "ship it"

        answered = _wait_for_event(rt, session_id, "user_question_answered")
        assert answered["call_id"] == call_id
        assert answered["outcome"] == "answered"
        assert answered["answered_via"] == "console"
        assert answered["answers"] == [
            {"selected_indexes": [1], "text": None},
            {"selected_indexes": None, "text": "ship it"},
        ]

        # The entry is gone once the dispatcher read it, but the ANSWER is
        # durable — so a duplicate POST reads 409 rather than 404, on
        # this path and after a restart alike.
        resp = c.post(
            url,
            json={"call_token": token, "answers": [{"text": "x"}, {"text": "y"}]},
            headers=_headers(),
        )
        assert resp.status_code == 409
    finally:
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("handle_setup", "expected_outcome"),
    [
        (lambda h: h.message_queue.put("typed instead"), "declined"),
        (lambda h: h.interrupt_step.set(), "interrupted"),
        (lambda h: h.cancel_event.set(), "cancelled"),
    ],
)
def test_steering_signals_supersede_a_pending_question(
    client, monkeypatch, handle_setup, expected_outcome
):
    c, rt, backend = client
    from mewbo_api.ask_user import ApiQuestionDispatcher

    session_id = rt.resolve_session()
    handle = _idle_handle()
    handle_setup(handle)
    monkeypatch.setattr(rt, "active_run_handle", lambda sid: handle)

    dispatcher = ApiQuestionDispatcher(runtime=rt)
    result = asyncio.run(dispatcher.dispatch(session_id, _ARGS))
    assert result.outcome == expected_outcome
    assert result.answers == ()

    answered = _wait_for_event(rt, session_id, "user_question_answered")
    assert answered["outcome"] == expected_outcome
    assert answered["answers"] is None
    assert answered["delivery"] is None

    # A supersede stops the RUN waiting; it does NOT close the question. The
    # late answer is still accepted and lands as a new user turn.
    monkeypatch.setattr(rt, "start_async", lambda **kw: f"{kw['session_id']}:r9")
    question = _wait_for_event(rt, session_id, "user_question")
    resp = c.post(
        f"/api/sessions/{session_id}/questions/{question['call_id']}/answer",
        json={
            "call_token": question["call_token"],
            "answers": [{"selected_indexes": [0]}, {"text": "late"}],
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"resolved": True, "delivery": "message"}


def test_steer_message_stays_queued_for_the_loop(client, monkeypatch):
    """Supersede must not EAT the steer — the message stays on the queue for
    the loop's own turn-top drain; the question result only tells the model
    the user answered by message."""
    c, rt, backend = client
    from mewbo_api.ask_user import ApiQuestionDispatcher

    session_id = rt.resolve_session()
    handle = _idle_handle()
    handle.message_queue.put("typed instead")
    monkeypatch.setattr(rt, "active_run_handle", lambda sid: handle)

    result = asyncio.run(ApiQuestionDispatcher(runtime=rt).dispatch(session_id, _ARGS))
    assert result.outcome == "declined"
    assert handle.message_queue.get_nowait() == "typed instead"


def test_notes_round_trip_to_the_waiter_and_the_event(client):
    """Notes ride the answer to the blocked call AND onto the settle event."""
    c, rt, backend = client
    from mewbo_api.ask_user import ApiQuestionDispatcher

    session_id = rt.resolve_session()
    dispatcher = ApiQuestionDispatcher(runtime=rt)
    result_box: dict = {}

    thread = threading.Thread(
        target=lambda: result_box.update(
            result=asyncio.run(dispatcher.dispatch(session_id, _TIMED_ARGS))
        )
    )
    thread.start()
    try:
        question = _wait_for_event(rt, session_id, "user_question")
        # The bounded call's knobs reach every surface on the durable event.
        assert question["timeout_seconds"] == 5
        assert question["notes_placeholder"] == "Anything else about the deploy?"

        resp = c.post(
            f"/api/sessions/{session_id}/questions/{question['call_id']}/answer",
            json={
                "call_token": question["call_token"],
                "answers": [{"selected_indexes": [0]}, {"text": "no"}],
                "notes": "Staging is mid-migration.",
            },
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"resolved": True, "delivery": "run"}

        thread.join(timeout=5)
        assert result_box["result"].notes == "Staging is mid-migration."
        answered = _wait_for_event(rt, session_id, "user_question_answered")
        assert answered["notes"] == "Staging is mid-migration."
        assert answered["delivery"] == "run"
    finally:
        thread.join(timeout=5)


def test_oversized_notes_are_refused_at_the_wire(client):
    c, rt, backend = client
    from mewbo_core.tooling.ask_user import MAX_QUESTION_NOTES_CHARS

    session_id = rt.resolve_session()
    question = _expire_question(rt, session_id)
    resp = c.post(
        f"/api/sessions/{session_id}/questions/{question['call_id']}/answer",
        json={
            "call_token": question["call_token"],
            "answers": [{"text": "a"}, {"text": "b"}],
            "notes": "x" * (MAX_QUESTION_NOTES_CHARS + 1),
        },
        headers=_headers(),
    )
    assert resp.status_code == 400


def test_unknown_body_key_is_refused(client):
    c, rt, backend = client

    session_id = rt.resolve_session()
    question = _expire_question(rt, session_id)
    resp = c.post(
        f"/api/sessions/{session_id}/questions/{question['call_id']}/answer",
        json={
            "call_token": question["call_token"],
            "answers": [{"text": "a"}, {"text": "b"}],
            "outcome": "answered",
        },
        headers=_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Bounded wait — the dispatcher's own deadline
# ---------------------------------------------------------------------------


def test_timeout_expiry_resolves_timed_out_without_sleeping(client):
    c, rt, backend = client
    from mewbo_api.ask_user import ApiQuestionDispatcher, get_pending_questions

    session_id = rt.resolve_session()
    clock = _StepClock()
    dispatcher = ApiQuestionDispatcher(runtime=rt, monotonic=clock)

    result = asyncio.run(dispatcher.dispatch(session_id, _TIMED_ARGS))
    assert result.outcome == "timed_out"
    assert result.answers == ()
    # One read to set the deadline, one to find it spent — no poll iteration.
    assert clock.reads == 2

    answered = _wait_for_event(rt, session_id, "user_question_answered")
    assert answered["outcome"] == "timed_out"
    assert answered["answers"] is None
    # The waiter is withdrawn, so the live registry no longer holds the entry.
    question = _wait_for_event(rt, session_id, "user_question")
    assert (
        get_pending_questions().resolve(
            session_id,
            question["call_id"],
            question["call_token"],
            [QuestionAnswerItem(text="a"), QuestionAnswerItem(text="b")],
            answered_via=None,
        )[0]
        == "not_found"
    )


def test_absent_timeout_never_reads_the_clock(client, monkeypatch):
    """An unbounded call has no deadline at all."""
    c, rt, backend = client
    from mewbo_api.ask_user import ApiQuestionDispatcher

    session_id = rt.resolve_session()
    clock = _StepClock()
    handle = _idle_handle()
    handle.cancel_event.set()
    monkeypatch.setattr(rt, "active_run_handle", lambda sid: handle)

    result = asyncio.run(
        ApiQuestionDispatcher(runtime=rt, monotonic=clock).dispatch(session_id, _ARGS)
    )
    assert result.outcome == "cancelled"
    assert clock.reads == 0

    question = _wait_for_event(rt, session_id, "user_question")
    assert question["timeout_seconds"] is None
    assert question["notes_placeholder"] is None


# ---------------------------------------------------------------------------
# QuestionAnswerRouter — an answer always reaches the model
# ---------------------------------------------------------------------------


def test_late_answer_on_an_idle_session_starts_a_run(client, monkeypatch):
    c, rt, backend = client

    session_id = rt.resolve_session()
    question = _expire_question(rt, session_id)
    captured: dict = {}
    monkeypatch.setattr(
        rt, "start_async", lambda **kw: captured.update(kw) or f"{kw['session_id']}:r4"
    )

    resp = c.post(
        f"/api/sessions/{session_id}/questions/{question['call_id']}/answer",
        json={
            "call_token": question["call_token"],
            "answers": [{"selected_indexes": [1]}, {"text": "ship it"}],
            "notes": "after the migration",
        },
        headers=_headers(**{"X-Mewbo-Surface": "console"}),
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"resolved": True, "delivery": "message"}

    # The turn reads as an answer to the earlier question, so the model can act.
    query = captured["user_query"]
    assert "answers the question you asked earlier" in query
    assert "Scope: All agents" in query
    assert "Rollout: ship it" in query
    assert "after the migration" in query

    # Every surface settles its card off the answered event, not off the POST.
    answered = [
        e["payload"]
        for e in rt.load_events(session_id)
        if e.get("type") == "user_question_answered"
    ]
    assert answered[-1]["outcome"] == "answered"
    assert answered[-1]["delivery"] == "message"
    assert answered[-1]["answered_via"] == "console"
    assert answered[-1]["notes"] == "after the migration"


def test_late_answer_rides_the_steer_queue_of_a_live_run(client, monkeypatch):
    """The run moved on but is still alive ⇒ the answer steers it, no new run."""
    c, rt, backend = client

    session_id = rt.resolve_session()
    question = _expire_question(rt, session_id)
    steered: list[str] = []
    monkeypatch.setattr(
        rt, "enqueue_message", lambda sid, text: bool(steered.append(text)) or True
    )
    monkeypatch.setattr(
        rt,
        "start_async",
        lambda **kw: pytest.fail("a live run must not be re-started"),
    )

    resp = c.post(
        f"/api/sessions/{session_id}/questions/{question['call_id']}/answer",
        json={
            "call_token": question["call_token"],
            "answers": [{"selected_indexes": [0]}, {"text": "yes"}],
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"resolved": True, "delivery": "message"}
    assert len(steered) == 1
    assert "Scope: Root only" in steered[0]


def test_late_answer_conflicts_only_after_a_genuine_answer(client, monkeypatch):
    c, rt, backend = client

    session_id = rt.resolve_session()
    question = _expire_question(rt, session_id)
    monkeypatch.setattr(rt, "start_async", lambda **kw: f"{kw['session_id']}:r4")
    url = f"/api/sessions/{session_id}/questions/{question['call_id']}/answer"
    body = {
        "call_token": question["call_token"],
        "answers": [{"text": "a"}, {"text": "b"}],
    }

    assert c.post(url, json=body, headers=_headers()).status_code == 200
    # Second delivery: now there IS a prior answer, so it is refused.
    second = c.post(url, json=body, headers=_headers())
    assert second.status_code == 409


def test_late_answer_unknown_call_id_and_token_mismatch(client):
    c, rt, backend = client

    session_id = rt.resolve_session()
    question = _expire_question(rt, session_id)
    body = {
        "call_token": question["call_token"],
        "answers": [{"text": "a"}, {"text": "b"}],
    }

    resp = c.post(
        f"/api/sessions/{session_id}/questions/nosuchcall/answer",
        json=body,
        headers=_headers(),
    )
    assert resp.status_code == 404

    resp = c.post(
        f"/api/sessions/{session_id}/questions/{question['call_id']}/answer",
        json={**body, "call_token": "wrong-token"},
        headers=_headers(),
    )
    assert resp.status_code == 403


def test_late_answer_validates_against_the_recovered_questions(client, monkeypatch):
    """Semantics are re-checked against the RECOVERED args, not re-implemented."""
    c, rt, backend = client

    session_id = rt.resolve_session()
    question = _expire_question(rt, session_id)
    monkeypatch.setattr(
        rt, "start_async", lambda **kw: pytest.fail("a bad shape must not start a run")
    )
    url = f"/api/sessions/{session_id}/questions/{question['call_id']}/answer"

    resp = c.post(
        url,
        json={"call_token": question["call_token"], "answers": [{"text": "only one"}]},
        headers=_headers(),
    )
    assert resp.status_code == 422
    assert "expected 2 answer" in resp.get_json()["message"]

    resp = c.post(
        url,
        json={
            "call_token": question["call_token"],
            "answers": [{"selected_indexes": [7]}, {"text": "b"}],
        },
        headers=_headers(),
    )
    assert resp.status_code == 422


def test_late_answer_on_a_terminated_session_is_refused(client):
    c, rt, backend = client

    session_id = rt.resolve_session()
    question = _expire_question(rt, session_id)
    rt.terminate_session(session_id)

    resp = c.post(
        f"/api/sessions/{session_id}/questions/{question['call_id']}/answer",
        json={
            "call_token": question["call_token"],
            "answers": [{"text": "a"}, {"text": "b"}],
        },
        headers=_headers(),
    )
    assert resp.status_code == 410


def test_late_answer_survives_a_lost_registry(client, monkeypatch):
    """The process-restart case: nothing in memory, everything in the transcript."""
    c, rt, backend = client
    from mewbo_api.ask_user import reset_pending_questions_for_tests

    session_id = rt.resolve_session()
    question = _expire_question(rt, session_id)
    reset_pending_questions_for_tests()  # a fresh process holds no waiters
    captured: dict = {}
    monkeypatch.setattr(
        rt, "start_async", lambda **kw: captured.update(kw) or f"{kw['session_id']}:r1"
    )

    resp = c.post(
        f"/api/sessions/{session_id}/questions/{question['call_id']}/answer",
        json={
            "call_token": question["call_token"],
            "answers": [{"selected_indexes": [0]}, {"text": "still relevant"}],
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["delivery"] == "message"
    assert "Rollout: still relevant" in captured["user_query"]


def test_delivery_refusal_reports_conflict_not_success(client, monkeypatch):
    """A run that starts between the two attempts must not read as answered."""
    c, rt, backend = client

    session_id = rt.resolve_session()
    question = _expire_question(rt, session_id)
    monkeypatch.setattr(rt, "enqueue_message", lambda sid, text: False)
    monkeypatch.setattr(rt, "start_async", lambda **kw: "")

    resp = c.post(
        f"/api/sessions/{session_id}/questions/{question['call_id']}/answer",
        json={
            "call_token": question["call_token"],
            "answers": [{"text": "a"}, {"text": "b"}],
        },
        headers=_headers(),
    )
    assert resp.status_code == 409
    # Nothing was recorded as answered, so the card stays answerable.
    answered = [
        e["payload"]
        for e in rt.load_events(session_id)
        if e.get("type") == "user_question_answered"
    ]
    assert all(p["outcome"] != "answered" for p in answered)


# ---------------------------------------------------------------------------
# The shared delivery seam (`/message` behaviour must be unchanged)
# ---------------------------------------------------------------------------


def test_message_steer_and_reengage_still_map_to_202_and_200(client, monkeypatch):
    c, rt, backend = client

    sid = c.post("/api/sessions", json={}, headers=_headers()).get_json()["session_id"]

    monkeypatch.setattr(rt, "enqueue_message", lambda session_id, text: True)
    resp = c.post(f"/api/sessions/{sid}/message", json={"text": "steer"}, headers=_headers())
    assert resp.status_code == 202
    assert resp.get_json() == {"session_id": sid, "enqueued": True}

    monkeypatch.setattr(rt, "enqueue_message", lambda session_id, text: False)
    monkeypatch.setattr(rt, "start_async", lambda **kw: f"{kw['session_id']}:r1")
    resp = c.post(f"/api/sessions/{sid}/message", json={"text": "again"}, headers=_headers())
    assert resp.status_code == 200
    assert resp.get_json() == {"session_id": sid, "enqueued": True, "run_id": f"{sid}:r1"}

    monkeypatch.setattr(rt, "start_async", lambda **kw: "")
    resp = c.post(f"/api/sessions/{sid}/message", json={"text": "busy"}, headers=_headers())
    assert resp.status_code == 409
    assert resp.get_json() == {"message": "Session is already running."}
