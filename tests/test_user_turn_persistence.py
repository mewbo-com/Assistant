#!/usr/bin/env python3
"""The accepted turn is persisted once, by whoever accepted it.

``SessionRuntime.start_async`` returns as soon as it has handed the run to a
background worker, and that worker pays ``Orchestrator.__init__``'s heavy
synchronous setup (tool-registry build + project-instruction discovery) before
the orchestration body appends anything. If the ``user`` event — the record of
the text the operator typed — were written by that body, the transcript would
hold no record of the accepted turn for the whole cold-start window and every
client would render an empty session.

So the acceptance seam writes it, and threads ``user_turn_persisted`` down to
the body, which then skips its own append. Two properties have to hold together
and neither is provable from one side alone:

* ORDERING — the ``user`` event is durable before the body produces anything.
  Asserted through explicit gates, never a sleep: a timing-based version of this
  test passes on a fast machine no matter which seam did the write.
* CARDINALITY — exactly one ``user`` event per turn, on both routes. Suppression
  is by flag, never by content comparison: two identical consecutive queries are
  legitimate, so a content check would silently drop a real turn.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from mewbo_core.classes import OrchestrationState, TaskQueue
from mewbo_core.loop.orchestrator import Orchestrator
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.session.session_store import SessionStore

# Every wait in this module is on an explicit threading primitive, so this bound
# is a deadlock tripwire rather than a race window someone tuned.
_GATE_TIMEOUT = 10.0


def _runtime(tmp_path) -> tuple[SessionRuntime, SessionStore, str]:
    store = SessionStore(root_dir=str(tmp_path))
    runtime = SessionRuntime(session_store=store)
    return runtime, store, runtime.resolve_session()


def _types(store: SessionStore, session_id: str) -> list[str]:
    return [e.get("type") for e in store.load_transcript(session_id)]


def _user_events(store: SessionStore, session_id: str) -> list[dict]:
    return [e for e in store.load_transcript(session_id) if e.get("type") == "user"]


async def _completed_loop_run(*_args, **_kwargs):
    """Stand-in for ``ToolUseLoop.run`` that completes without an LLM call."""
    task_queue = TaskQueue(action_steps=[])
    task_queue.task_result = "Done"
    state = OrchestrationState(goal="test", session_id="s1")
    state.done = True
    state.done_reason = "completed"
    return task_queue, state


class TestAcceptanceSeamPersistsTheTurn:
    """``start_async`` writes the turn before the executor can run."""

    def test_user_event_precedes_anything_the_body_appends(self, tmp_path):
        """The turn is durable while the executor is still constructing.

        ``orchestrate_session`` is the seam where ``Orchestrator.__init__`` does
        its heavy synchronous setup, so entering it stands for "construction
        started". The fake holds the worker there — a deliberately slow
        construction, expressed as a gate rather than a sleep — and the main
        thread reads the store meanwhile: the ``user`` event must already be
        present, which is only possible if the ACCEPTOR wrote it.
        """
        import mewbo_core.loop.session_runtime as sr

        runtime, store, session_id = _runtime(tmp_path)

        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        seen_at_entry: dict[str, list[str]] = {}

        def fake_orchestrate_session(**kwargs):
            seen_at_entry["types"] = _types(store, kwargs["session_id"])
            entered.set()
            assert release.wait(_GATE_TIMEOUT)
            # The body's first output, appended only once construction is done.
            store.append_event(
                kwargs["session_id"], {"type": "assistant", "payload": {"text": "hi"}}
            )
            finished.set()
            return TaskQueue(action_steps=[])

        with patch.object(sr, "orchestrate_session", fake_orchestrate_session):
            run_id = runtime.start_async(session_id=session_id, user_query="ship the thing")
            assert run_id == f"{session_id}:r1"

            # Synchronously durable: no wait of any kind between the call
            # returning and this read.
            assert _types(store, session_id) == ["run_accepted", "user"]
            assert _user_events(store, session_id)[0]["payload"]["text"] == "ship the thing"

            assert entered.wait(_GATE_TIMEOUT)
            # What the executor saw the moment its construction began.
            assert seen_at_entry["types"] == ["run_accepted", "user"]
            release.set()
            assert finished.wait(_GATE_TIMEOUT)

        # Append order is the transcript's ground truth: the turn precedes the
        # body's own first output rather than merely existing alongside it.
        assert _types(store, session_id) == ["run_accepted", "user", "assistant"]

    def test_refused_start_writes_nothing(self, tmp_path):
        """A start refused because a run is live records neither event.

        The refusal must leave the transcript untouched — a ``user`` event for a
        turn that never ran would show the operator a message the engine never
        received, and would also inflate the ``user``-event count that
        ``_mint_run_id`` derives the next run's sequence from.
        """
        runtime, store, session_id = _runtime(tmp_path)

        running = threading.Event()
        release = threading.Event()

        def fake_run_sync(*, session_id, user_query, should_cancel=None, **_kwargs):
            running.set()
            assert release.wait(_GATE_TIMEOUT)

        runtime.run_sync = fake_run_sync
        try:
            assert runtime.start_async(session_id=session_id, user_query="first")
            assert running.wait(_GATE_TIMEOUT)
            before = _types(store, session_id)

            assert runtime.start_async(session_id=session_id, user_query="refused") == ""
            assert _types(store, session_id) == before
        finally:
            release.set()

        assert [e["payload"]["text"] for e in _user_events(store, session_id)] == ["first"]
        assert _types(store, session_id).count("run_accepted") == 1

    def test_attachments_ride_the_accepted_turn(self, tmp_path):
        """The acceptor's payload carries ``attachments`` — and only when present.

        ``UserPayload.attachments`` is ``NotRequired``, so a turn without
        attachments must produce the byte-identical payload it always did: the
        key absent, not present-and-empty.
        """
        runtime, store, session_id = _runtime(tmp_path)
        descriptors = [{"id": "a1", "filename": "note.txt"}]

        def fake_run_sync(**_kwargs):
            return None

        runtime.run_sync = fake_run_sync
        runtime.start_async(
            session_id=session_id, user_query="read this", attachments=descriptors
        )
        bare = runtime.resolve_session()
        runtime.start_async(session_id=bare, user_query="no files")

        with_attachments = _user_events(store, session_id)[0]["payload"]
        assert with_attachments["attachments"] == descriptors
        assert "attachments" not in _user_events(store, bare)[0]["payload"]


class TestExactlyOneUserEvent:
    """Cardinality across both routes into the orchestration body."""

    def test_start_async_run_writes_exactly_one(self, tmp_path):
        """A full ``start_async`` run leaves ONE ``user`` event.

        Deliberately drives the REAL chain — ``start_async`` → ``run_sync`` →
        ``orchestrate_session`` → ``orchestrate_session_async`` →
        ``Orchestrator.arun`` → the shared body — stubbing only the LLM-driven
        loop. A test that patched ``orchestrate_session`` instead could not see
        the flag being dropped at any of those hand-offs, which is precisely how
        a duplicate turn would reappear.
        """
        runtime, store, session_id = _runtime(tmp_path)

        with patch.object(ToolUseLoop, "run", _completed_loop_run):
            runtime.start_async(session_id=session_id, user_query="one turn only")
            _join(runtime, session_id)

        users = _user_events(store, session_id)
        assert [e["payload"]["text"] for e in users] == ["one turn only"]

    def test_direct_orchestrator_run_still_writes_its_own(self, tmp_path):
        """The default (``user_turn_persisted=False``) keeps the body's append.

        The contract every direct caller relies on — the CLI turn engine, the
        structured runners, an in-process test — none of which passes through
        the acceptance seam. If the body stopped writing unconditionally, their
        turns would vanish entirely rather than merely arrive late.
        """
        store = SessionStore(root_dir=str(tmp_path))
        orchestrator = Orchestrator(session_store=store)
        session_id = store.create_session()

        with patch.object(ToolUseLoop, "run", _completed_loop_run):
            orchestrator.run(user_query="direct drive", session_id=session_id, max_iters=1)

        users = _user_events(store, session_id)
        assert [e["payload"]["text"] for e in users] == ["direct drive"]

    def test_orchestrator_skips_its_append_when_told(self, tmp_path):
        """``user_turn_persisted=True`` suppresses the body's append.

        The other half of the pair above: asserted on a session whose transcript
        holds no ``user`` event at all, so a still-writing body shows up as a
        turn appearing from nowhere rather than as an ambiguous count.
        """
        store = SessionStore(root_dir=str(tmp_path))
        orchestrator = Orchestrator(session_store=store)
        session_id = store.create_session()

        with patch.object(ToolUseLoop, "run", _completed_loop_run):
            orchestrator.run(
                user_query="already recorded",
                session_id=session_id,
                max_iters=1,
                user_turn_persisted=True,
            )

        assert _user_events(store, session_id) == []


def _join(runtime: SessionRuntime, session_id: str, timeout: float = _GATE_TIMEOUT) -> None:
    """Block until the session's run thread has finished.

    Joins the run handle's own thread rather than polling ``is_running``, so the
    assertions that follow read a SETTLED transcript. No handle means the run
    already finished — the registry drops it in the worker's ``finally``, i.e.
    strictly after every append — so there is nothing left to wait for and the
    absent-handle branch is a completed run, never a skipped wait.
    """
    handle = runtime.active_run_handle(session_id)
    if handle is not None and handle.thread is not None:
        handle.thread.join(timeout)
        assert not handle.thread.is_alive()
