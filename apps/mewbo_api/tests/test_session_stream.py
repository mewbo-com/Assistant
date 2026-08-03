"""Tests for the push-based SSE session stream generator.

The generator must:
- load the backlog exactly ONCE (no per-event full-transcript re-read),
- trim the replay leg by an optional ``after`` cursor, INCLUSIVE — an event
  whose ``ts`` equals the cursor is re-sent, never dropped, because several
  events can share one timestamp,
- emit each backlog event, then live events pushed via the SessionEventBus,
- de-duplicate the subscribe/backlog race-window overlap by content key,
- publish a ``session_state`` frame right after the replay and again right
  before ``stream_end``, both rendered from the SAME ``summarize_session``
  call so the polled and pushed transports never disagree,
- read run liveness BEFORE choosing the blocking timeout, so a not-running
  session polls the queue non-blockingly instead of pinning a heartbeat slot,
- emit ``stream_end`` when the run is no longer running and the queue drains.
"""

from __future__ import annotations

import json
import queue
import time
from types import SimpleNamespace

from mewbo_api.backend import SessionStateFrame, SessionStream
from mewbo_core.contracts.types import EventRecord
from mewbo_core.session.session_event_bus import SessionEventBus

# A summary an untroubled, idle, completed session would produce. Individual
# tests override only the keys they care about via ``_fake_runtime(summary=)``
# / ``_expected_state_frame(summary)``.
_DEFAULT_SUMMARY: dict[str, object] = {
    "running": False,
    "status": "completed",
    "done_reason": "task_complete",
    "title": "Test Session",
    "recoverable": False,
    "terminated": False,
    "terminated_at": None,
}


def _ev(text: str) -> EventRecord:
    return {"ts": f"2026-06-07T00:00:0{text}Z", "type": "user", "payload": {"text": text}}


def _fake_runtime(
    backlog: list[EventRecord],
    running_flags: list[bool],
    summary: dict[str, object] | None = None,
):
    """Build a fake runtime whose is_running() returns the flags in sequence.

    ``load_transcript`` records its call count so the test can assert it is
    invoked exactly once (proves the per-event re-read is gone).
    ``summarize_session`` returns a fixed dict (``_DEFAULT_SUMMARY`` merged
    with any override) and records its own call count, so a test can pin
    both HOW MANY times the ``session_state`` frame is (re)computed and WHAT
    it renders, without a real ``SessionRuntime``.
    """
    calls = {"load_transcript": 0, "summarize_session": 0}

    def load_transcript(session_id: str) -> list[EventRecord]:
        calls["load_transcript"] += 1
        return list(backlog)

    flags = iter(running_flags)

    def is_running(session_id: str) -> bool:
        try:
            return next(flags)
        except StopIteration:
            return False

    resolved_summary = {**_DEFAULT_SUMMARY, **(summary or {})}

    def summarize_session(session_id: str) -> dict[str, object]:
        calls["summarize_session"] += 1
        return dict(resolved_summary)

    store = SimpleNamespace(load_transcript=load_transcript)
    runtime = SimpleNamespace(
        session_store=store,
        is_running=is_running,
        summarize_session=summarize_session,
    )
    return runtime, calls


def _expected_state_frame(summary: dict[str, object] | None = None) -> str:
    """The exact ``session_state`` SSE frame ``_fake_runtime``'s summary renders to."""
    resolved_summary = {**_DEFAULT_SUMMARY, **(summary or {})}
    return SessionStateFrame.from_summary(resolved_summary).sse_frame()


def test_backlog_then_live_then_stream_end():
    """Backlog flushes, one live event arrives once, then stream_end on stop.

    A ``session_state`` frame now brackets the live segment: one right after
    the backlog replay (before the first blocking read) and one right before
    ``stream_end``.
    """
    bus = SessionEventBus()
    backlog = [_ev("0"), _ev("1")]
    runtime, calls = _fake_runtime(backlog, running_flags=[False])

    live = _ev("2")

    def gen():
        # Pre-seed the live event so the first queue.get returns it without a
        # heartbeat timeout, keeping the test fast and deterministic.
        sub = bus.subscribe("s1")
        sub.queue.put(live)
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=0.05, idle_close_s=0.2, _sub=sub
        )

    chunks = list(gen())
    state_frame = _expected_state_frame()
    assert chunks == [
        f"data: {_json(backlog[0])}\n\n",
        f"data: {_json(backlog[1])}\n\n",
        state_frame,
        f"data: {_json(live)}\n\n",
        state_frame,
        'data: {"type": "stream_end"}\n\n',
    ]
    # The critical assertion: backlog loaded exactly once.
    assert calls["load_transcript"] == 1
    # The state frame is published exactly twice: post-replay and pre-close.
    assert calls["summarize_session"] == 2


def test_pending_event_drained_before_stream_end():
    """A terminal event published in the close race is delivered, not dropped.

    Exercises the empty-timeout→not-running branch with a PENDING queue item:
    the run thread publishes its completion right before is_running flips False,
    so the loop must drain the queue (with backlog dedup) before stream_end.
    The first blocking get() must time out (queue momentarily empty) for the
    drain branch to be reached — a plain pre-seeded queue would short-circuit
    via the happy get() path and never exercise the race (the bug this guards).
    The post-replay ``session_state`` frame fires before any of this (the
    backlog is empty here, so it is the very first chunk); the pre-close one
    fires after the drained event, right before ``stream_end``.
    """
    bus = SessionEventBus()
    runtime, _ = _fake_runtime([], running_flags=[False])

    terminal = _ev("9")

    class _BlockingThenItemQueue:
        """A queue whose blocking get() times out once, then the item is drained.

        Models the real race: the first heartbeat get() finds nothing, the run
        finishes (is_running False), and the terminal event is already enqueued
        for the non-blocking drain loop to pick up.
        """

        def __init__(self) -> None:
            self._q: queue.Queue = queue.Queue()
            self._first_get = True

        def get(self, timeout=None):
            if self._first_get:
                self._first_get = False
                raise queue.Empty
            return self._q.get(timeout=timeout)

        def get_nowait(self):
            return self._q.get_nowait()

        def put(self, item) -> None:
            self._q.put(item)

    def gen():
        sub = bus.subscribe("s1")
        sub.queue = _BlockingThenItemQueue()
        sub.queue.put(terminal)
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=0.01, idle_close_s=5.0, _sub=sub
        )

    chunks = list(gen())
    state_frame = _expected_state_frame()
    # post-replay state frame, the drained terminal event, the pre-close
    # state frame, THEN stream_end.
    assert chunks == [
        state_frame,
        f"data: {_json(terminal)}\n\n",
        state_frame,
        'data: {"type": "stream_end"}\n\n',
    ]


def test_dedup_drops_backlog_overlap():
    """An event already in the backlog arriving on the queue is dropped once."""
    bus = SessionEventBus()
    dup = _ev("0")
    backlog = [dup]
    runtime, calls = _fake_runtime(backlog, running_flags=[False])

    def gen():
        sub = bus.subscribe("s1")
        # Simulate the race: the same event landed on the queue (publish fired
        # between subscribe and backlog load) AND is in the backlog.
        sub.queue.put(dup)
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=0.05, idle_close_s=0.2, _sub=sub
        )

    chunks = list(gen())
    state_frame = _expected_state_frame()
    # Backlog yields it once; the queued duplicate is skipped (no frame emitted
    # for it at all); a session_state frame brackets the empty live segment.
    assert chunks == [
        f"data: {_json(dup)}\n\n",
        state_frame,
        state_frame,
        'data: {"type": "stream_end"}\n\n',
    ]
    assert calls["load_transcript"] == 1


def test_heartbeat_emitted_while_running_and_idle():
    """While running with an empty queue, a heartbeat comment is emitted."""
    bus = SessionEventBus()
    # First is_running check (empty queue) -> True -> heartbeat; second -> False.
    runtime, _ = _fake_runtime([], running_flags=[True, False])

    def gen():
        sub = bus.subscribe("s1")
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=0.01, idle_close_s=5.0, _sub=sub
        )

    chunks = list(gen())
    state_frame = _expected_state_frame()
    assert chunks == [
        state_frame,
        ": heartbeat\n\n",
        state_frame,
        'data: {"type": "stream_end"}\n\n',
    ]


def test_idle_close_breaks_without_stream_end_when_still_running():
    """Idle past idle_close_s closes the stream even while nominally running."""
    bus = SessionEventBus()
    # Always running -> never emits stream_end -> must break on idle timeout.
    runtime, _ = _fake_runtime([], running_flags=[True] * 100)

    def gen():
        sub = bus.subscribe("s1")
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=0.01, idle_close_s=0.03, _sub=sub
        )

    chunks = list(gen())
    state_frame = _expected_state_frame()
    # The post-replay state frame always fires first, unconditionally, even
    # with an empty backlog. Everything after it is a heartbeat until the
    # idle-close timeout breaks the loop directly: no second state frame, no
    # stream_end — that pair belongs only to the not-running drain path.
    assert chunks[0] == state_frame
    assert chunks[1:]
    assert all(c == ": heartbeat\n\n" for c in chunks[1:])
    assert 'data: {"type": "stream_end"}\n\n' not in chunks


def test_after_trims_replay_to_events_at_or_after_cursor():
    """``after`` narrows the replay leg to events at-or-after the cursor.

    A reconnecting client passes the ``ts`` of the last event it already
    holds; only later events are worth re-sending. The live subscription
    (and everything past the replay) is untouched by the trim.
    """
    bus = SessionEventBus()
    backlog = [_ev("0"), _ev("1"), _ev("2")]
    runtime, calls = _fake_runtime(backlog, running_flags=[False])

    def gen():
        sub = bus.subscribe("s1")
        yield from SessionStream._stream_events(
            "s1",
            runtime,
            bus,
            heartbeat_s=0.05,
            idle_close_s=0.2,
            after=backlog[1]["ts"],
            _sub=sub,
        )

    chunks = list(gen())
    state_frame = _expected_state_frame()
    # backlog[0]'s ts is strictly before the cursor and is trimmed away.
    assert chunks == [
        f"data: {_json(backlog[1])}\n\n",
        f"data: {_json(backlog[2])}\n\n",
        state_frame,
        state_frame,
        'data: {"type": "stream_end"}\n\n',
    ]
    # ``after`` trims the in-memory replay; it does not push the filter down
    # to the store — the full backlog is still loaded exactly once.
    assert calls["load_transcript"] == 1


def test_after_cursor_is_inclusive_resends_same_timestamp_siblings():
    """The ``after`` bound is INCLUSIVE: an event AT the cursor is re-sent.

    Deliberate, not an off-by-one: several events can share one timestamp,
    so a strictly-greater cursor would silently drop the siblings of the
    event a client already saw. This pins ``>=`` so a future "optimise this
    to ``>``" fails loudly here instead of quietly losing transcript rows on
    every reconnect.
    """
    bus = SessionEventBus()
    earlier = {"ts": "2026-06-07T00:00:00Z", "type": "user", "payload": {"text": "earlier"}}
    cursor_ts = "2026-06-07T00:00:01Z"
    sibling_a = {"ts": cursor_ts, "type": "user", "payload": {"text": "sibling-a"}}
    sibling_b = {"ts": cursor_ts, "type": "user", "payload": {"text": "sibling-b"}}
    backlog = [earlier, sibling_a, sibling_b]
    runtime, _ = _fake_runtime(backlog, running_flags=[False])

    def gen():
        sub = bus.subscribe("s1")
        yield from SessionStream._stream_events(
            "s1",
            runtime,
            bus,
            heartbeat_s=0.05,
            idle_close_s=0.2,
            after=cursor_ts,
            _sub=sub,
        )

    chunks = list(gen())
    state_frame = _expected_state_frame()
    # ``earlier`` is strictly before the cursor and is trimmed; BOTH siblings
    # sitting AT the cursor's own timestamp survive — a strict `>` would have
    # dropped one of them.
    assert chunks == [
        f"data: {_json(sibling_a)}\n\n",
        f"data: {_json(sibling_b)}\n\n",
        state_frame,
        state_frame,
        'data: {"type": "stream_end"}\n\n',
    ]


def test_after_none_replays_everything():
    """``after=None`` is unchanged behaviour: the whole backlog replays.

    Existing clients that never pass ``after`` (e.g. a fresh, first-time
    connection) must keep getting the full transcript on connect.
    """
    bus = SessionEventBus()
    backlog = [_ev("0"), _ev("1")]
    runtime, calls = _fake_runtime(backlog, running_flags=[False])

    def gen():
        sub = bus.subscribe("s1")
        yield from SessionStream._stream_events(
            "s1",
            runtime,
            bus,
            heartbeat_s=0.05,
            idle_close_s=0.2,
            after=None,
            _sub=sub,
        )

    chunks = list(gen())
    state_frame = _expected_state_frame()
    assert chunks == [
        f"data: {_json(backlog[0])}\n\n",
        f"data: {_json(backlog[1])}\n\n",
        state_frame,
        state_frame,
        'data: {"type": "stream_end"}\n\n',
    ]
    assert calls["load_transcript"] == 1


def test_session_state_frame_follows_replay_and_precedes_stream_end():
    """The session_state frame sits right after the replay and right before
    stream_end, and its JSON is the exact summarize_session projection —
    not a recomputed or partial copy of it.
    """
    bus = SessionEventBus()
    backlog = [_ev("0")]
    summary = {
        "running": False,
        "status": "failed",
        "done_reason": "error",
        "title": "Investigate the flake",
        "recoverable": True,
        "terminated": False,
        "terminated_at": None,
    }
    runtime, calls = _fake_runtime(backlog, running_flags=[False], summary=summary)

    def gen():
        sub = bus.subscribe("s1")
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=0.05, idle_close_s=0.2, _sub=sub
        )

    chunks = list(gen())
    expected_frame = _expected_state_frame(summary)
    assert chunks[1] == expected_frame  # right after the one backlog event
    assert chunks[-1] == 'data: {"type": "stream_end"}\n\n'
    assert chunks[-2] == expected_frame  # right before stream_end
    # Both emissions read the SAME summarize_session call — the polled
    # /events body and this stream must never be able to disagree.
    assert calls["summarize_session"] == 2


def test_state_frame_blanks_done_reason_while_running():
    """``from_summary`` blanks ``done_reason`` while the session is running.

    ``summarize_session`` reports the LAST completion in the transcript,
    which is the PREVIOUS turn's once a new one starts. A frame published
    mid-run must not surface that stale prior reason as if it were current.
    """
    summary = {
        "running": True,
        "status": "running",
        "done_reason": "task_complete",  # stale: leftover from the prior turn
        "title": "Refactor the parser",
        "recoverable": False,
        "terminated": False,
        "terminated_at": None,
    }
    runtime, calls = _fake_runtime([], running_flags=[False], summary=summary)

    frame = SessionStream._state_frame("s1", runtime)
    payload = _frame_payload(frame)

    assert payload["type"] == "session_state"
    assert payload["running"] is True
    assert payload["status"] == "running"
    assert payload["done_reason"] == ""
    assert payload["title"] == "Refactor the parser"
    assert calls["summarize_session"] == 1


def test_idle_session_closes_promptly_without_blocking_heartbeat():
    """A not-running session must not block a full heartbeat before closing.

    Uses a deliberately large ``heartbeat_s`` (30s) with a non-running
    session: if liveness were still checked only on a queue timeout (the old
    order), this generator would block for 30 seconds. Reading liveness
    BEFORE choosing the timeout means it polls non-blockingly instead, so the
    whole generator must drain in well under a second — the property that
    keeps one idle console tab from pinning an API request slot.
    """
    bus = SessionEventBus()
    runtime, _ = _fake_runtime([], running_flags=[False])

    def gen():
        sub = bus.subscribe("s1")
        yield from SessionStream._stream_events(
            "s1", runtime, bus, heartbeat_s=30.0, idle_close_s=300.0, _sub=sub
        )

    start = time.monotonic()
    chunks = list(gen())
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert chunks[-1] == 'data: {"type": "stream_end"}\n\n'


def _json(ev: EventRecord) -> str:
    return json.dumps(ev)


def _frame_payload(frame: str) -> dict:
    """Parse a ``data: {...}\n\n`` SSE frame's JSON body."""
    return json.loads(frame.removeprefix("data: ").rstrip("\n"))
