#!/usr/bin/env python3
"""The `/events` poll: its cursor must narrow the WORK, and must fail closed.

`GET /api/sessions/{id}/events` is polled once a second per open client, and it
had two defects that a correctness test cannot see because neither made an
answer wrong.

**The cursor narrowed the response and not the work.** Both halves of the
handler read the entire transcript: `load_events` materialised it and filtered
in Python, and `summarize_session` re-read it to derive authoritative status. On
the largest live session (10,266 events) a cursor matching ZERO events returned
397 B and cost 0.470 s — against 0.010 s for the same call on a 15-event
session. The response was empty and the cost tracked the record, so measuring
the payload proved nothing.

**The filter failed open.** An `after` value that did not parse returned the
WHOLE transcript with a 200. The live trigger is this API's own timestamps: they
contain `+`, so a client echoing one back unencoded sends a space, the parse
fails, and it silently re-downloads everything on every poll.

Cost here is asserted as READ COUNTS and events-examined, never elapsed time —
this suite runs on a loaded machine, and a timing assertion measures the machine.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.session.event_cursor import EventCursor
from mewbo_core.session.session_store import SessionStore

FUTURE = "2099-01-01T00:00:00+00:00"
API_KEY = "test-master-token-351"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Backend test client bound to a temp SessionStore runtime + known token.

    Same shape as the ingest suite's fixture: the real Flask app against a real
    store, with only auth and the storage root swapped, so the route tests drive
    production's own read path. Monkeypatched so the rebind cannot leak into the
    shared suite (see tests/CLAUDE.md).
    """
    from mewbo_core.session.session_event_bus import reset_session_event_bus_for_tests

    reset_session_event_bus_for_tests()

    import mewbo_api.backend as backend

    monkeypatch.setattr(backend, "MASTER_API_TOKEN", API_KEY, raising=False)
    store = SessionStore(root_dir=str(tmp_path / "sessions"))
    rt = SessionRuntime(session_store=store)
    monkeypatch.setattr(backend, "runtime", rt, raising=False)
    backend.app.config["TESTING"] = True
    return backend.app.test_client(), rt


def _headers() -> dict:
    return {"X-Api-Key": API_KEY}


class _CountingStore(SessionStore):
    """Real JSON store that tallies transcript reads and events examined."""

    def __init__(self, root_dir: str) -> None:
        super().__init__(root_dir=root_dir)
        self.calls: dict[str, int] = {}
        self.examined = 0

    def load_transcript(self, session_id: str) -> list:
        self.calls["load_transcript"] = self.calls.get("load_transcript", 0) + 1
        return super().load_transcript(session_id)

    def load_events_after(self, session_id, cursor):
        self.calls["load_events_after"] = self.calls.get("load_events_after", 0) + 1
        return super().load_events_after(session_id, cursor)

    def session_digest(self, session_id: str):
        self.calls["session_digest"] = self.calls.get("session_digest", 0) + 1
        digest = super().session_digest(session_id)
        self.examined += len(digest.events)
        return digest


def _seed(store: SessionStore, noise: int) -> str:
    """One session with a user turn, a terminal, and *noise* filler events."""
    session_id = store.create_session()
    store.append_user_turn(session_id, "go")
    for step in range(noise):
        store.append_event(
            session_id,
            {"type": "llm_call_start", "payload": {"model": "m", "step": step}},
        )
    store.append_event(
        session_id,
        {"type": "completion", "payload": {"done": True, "done_reason": "completed"}},
    )
    return session_id


class TestPollWorkDoesNotTrackTranscriptSize:
    @pytest.mark.parametrize("noise", [2, 120])
    def test_status_derivation_examines_only_the_digest(self, tmp_path, noise):
        """A poll's status read must not grow with the session's history.

        `noise` varies 60x. The events the fold examines are pinned to an exact
        count, not a ceiling: an inequality would still pass if the projection
        quietly started admitting the filler back.
        """
        store = _CountingStore(str(tmp_path / f"n{noise}"))
        session_id = _seed(store, noise=noise)
        store.calls.clear()
        store.examined = 0

        SessionRuntime(session_store=store).summarize_session(session_id)

        assert store.calls.get("load_transcript") is None
        assert store.calls.get("session_digest") == 1
        # The user turn and the completion — never the filler. (The user event is
        # also the session's first, so the digest's always-keep-the-first rule
        # adds nothing here rather than duplicating it.)
        assert store.examined == 2

    def test_a_zero_match_cursor_still_goes_through_the_narrowing_seam(self, tmp_path):
        """A cursor matching nothing must not be served by a full read.

        Asserting the RESPONSE is empty is exactly the assertion that let the
        defect ship — it was already empty. What has to hold is that the request
        reaches the store as a cursor the store can narrow with, rather than as a
        transcript the caller filters afterwards.
        """
        store = _CountingStore(str(tmp_path))
        session_id = _seed(store, noise=40)
        store.calls.clear()

        events = SessionRuntime(session_store=store).load_events(session_id, FUTURE)

        assert events == []
        assert store.calls.get("load_events_after") == 1
        assert store.calls.get("load_transcript") is None


class TestCursorSemanticsUnchanged:
    """What the cursor returns must not move — only what it costs."""

    def _runtime(self, tmp_path):
        store = SessionStore(root_dir=str(tmp_path))
        return SessionRuntime(session_store=store), store

    def test_no_cursor_returns_everything(self, tmp_path):
        runtime, store = self._runtime(tmp_path)
        session_id = _seed(store, noise=3)
        assert len(runtime.load_events(session_id)) == len(
            store.load_transcript(session_id)
        )

    def test_cursor_returns_strictly_newer_events(self, tmp_path):
        runtime, store = self._runtime(tmp_path)
        session_id = _seed(store, noise=3)
        all_events = store.load_transcript(session_id)
        cutoff = all_events[2]["ts"]

        newer = runtime.load_events(session_id, cutoff)

        assert newer == all_events[3:]
        # Strictly after: the anchor event itself is never re-sent, which is what
        # keeps a poll from replaying its own last row forever.
        assert all(event["ts"] > cutoff for event in newer)

    def test_an_unparseable_cursor_widens_in_process(self, tmp_path):
        """In-process widening is the DEFENSIVE default, not the policy.

        A caller holding no HTTP connection has no channel to be refused on, and
        most of them pass no cursor at all — so the runtime keeps its
        behaviour. Nothing may RELY on it: the route that accepted the value from
        a client refuses it, which is what `TestRouteRefusesAnUnapplicableFilter`
        pins. Asserted here so the two halves of that split are visible together
        rather than one of them looking like an oversight.
        """
        runtime, store = self._runtime(tmp_path)
        session_id = _seed(store, noise=3)

        assert runtime.load_events(session_id, "not-a-timestamp") == (
            store.load_transcript(session_id)
        )

    def test_an_event_with_no_timestamp_is_not_newer(self, tmp_path):
        """A malformed record is dropped from the window, never raised on.

        An offset-naive stored timestamp makes this comparison raise
        `TypeError` outright — a 500 on a path polled once a second, from one
        bad record.
        """
        runtime, store = self._runtime(tmp_path)
        session_id = store.create_session()
        store.append_user_turn(session_id, "anchor")
        anchor = store.load_transcript(session_id)[-1]["ts"]
        store._write_event(session_id, {"type": "user", "ts": "not-a-timestamp"})

        assert runtime.load_events(session_id, anchor) == []


class TestEventCursor:
    """The parsing rule the store, the runtime and the route all share."""

    def test_unparseable_is_none_and_absent_is_the_callers_problem(self):
        """`None` means UNPARSEABLE — the two cases must stay distinguishable.

        Collapsing "no cursor" and "bad cursor" into one return value is exactly
        how the fail-open defect existed: they demand opposite behaviour.
        """
        assert EventCursor.parse("not-a-timestamp") is None
        assert EventCursor.parse("2026-07-26T12:00:00+00:00") is not None
        # A space is what an unencoded `+` decodes to — the live footgun.
        assert EventCursor.parse("2026-07-26T12:00:00 00:00") is None

    def test_store_floor_never_excludes_an_event_the_cursor_admits(self):
        """The pushdown bound must be a SUPERSET of the exact comparison.

        The store compares these timestamps as TEXT while the cursor compares
        instants, so the bound is deliberately coarser. A bound that ever sorted
        above a matching event would drop it from a poll with nothing raised
        anywhere.
        """
        cursor = EventCursor.parse("2026-07-26T12:00:00.500000+00:00")
        assert cursor is not None
        for later in (
            "2026-07-26T12:00:00.500001+00:00",
            "2026-07-26T12:00:01+00:00",
            "2026-07-26T12:00:01.000000+00:00",
            "2026-07-27T00:00:00.000000+00:00",
        ):
            assert cursor.matches({"ts": later}), later
            assert later > cursor.store_floor, later

    def test_a_foreign_offset_is_normalised_at_the_append_seam(self):
        """Text ordering is only sound because one spelling reaches the store.

        An event whose timestamp carries a different UTC offset orders one way
        as text and another as an instant — so it is canonicalised on the way in
        rather than trusted. The INSTANT is preserved; only the spelling moves.
        """
        stamped = SessionStore.stamp({"type": "user", "ts": "2026-07-26T07:00:01-05:00"})
        assert stamped["ts"] == "2026-07-26T12:00:01.000000+00:00"

    def test_a_record_already_in_the_stores_spelling_is_untouched(self):
        """Mirror fidelity: a client echoing our own format stores byte-identical."""
        original = "2026-07-01T10:00:00.000000+00:00"
        assert SessionStore.stamp({"type": "user", "ts": original})["ts"] == original

    def test_an_unparseable_timestamp_is_stored_as_sent(self):
        """A value nobody can parse is evidence; a fabricated one is not."""
        stamped = SessionStore.stamp({"type": "user", "ts": "whenever"})
        assert stamped["ts"] == "whenever"


class TestRouteRefusesAnUnapplicableFilter:
    """The 400 that replaces a silent full-transcript download."""

    def test_malformed_cursor_is_refused(self, client):
        c, rt = client
        sid = rt.session_store.create_session()
        rt.session_store.append_user_turn(sid, "hello")

        resp = c.get(f"/api/sessions/{sid}/events?after=not-a-timestamp", headers=_headers())

        assert resp.status_code == 400
        # The failure must not be describable as a small successful answer.
        assert "events" not in resp.get_json()

    def test_unencoded_plus_is_refused_rather_than_widened(self, client):
        """The live footgun, end to end.

        A client echoing back a `ts` without percent-encoding sends `+` as a
        space. Parse-failing open would return the entire transcript with a
        200, so the client accepts the data and the server logs a success.
        """
        c, rt = client
        sid = rt.session_store.create_session()
        rt.session_store.append_user_turn(sid, "hello")
        ts = rt.session_store.load_transcript(sid)[-1]["ts"]

        resp = c.get(f"/api/sessions/{sid}/events?after={ts}", headers=_headers())

        # Assert the scenario is real before asserting the outcome: if the
        # stored format ever stopped carrying `+`, this test would otherwise
        # keep passing while testing nothing.
        assert "+" in ts
        assert resp.status_code == 400

    def test_encoded_cursor_is_accepted(self, client):
        """The same timestamp, correctly encoded, still narrows normally."""
        c, rt = client
        sid = rt.session_store.create_session()
        rt.session_store.append_user_turn(sid, "hello")
        ts = rt.session_store.load_transcript(sid)[-1]["ts"]

        resp = c.get(
            f"/api/sessions/{sid}/events?after={quote(ts, safe='')}", headers=_headers()
        )

        assert resp.status_code == 200
        assert resp.get_json()["events"] == []

    def test_empty_cursor_still_returns_everything(self, client):
        """`?after=` is NO cursor, not a bad one — a first poll sends it."""
        c, rt = client
        sid = rt.session_store.create_session()
        rt.session_store.append_user_turn(sid, "hello")

        resp = c.get(f"/api/sessions/{sid}/events?after=", headers=_headers())

        assert resp.status_code == 200
        assert len(resp.get_json()["events"]) == len(rt.session_store.load_transcript(sid))

    def test_status_still_describes_the_whole_session_not_the_window(self, client):
        """The cursor narrows events ONLY.

        Deriving status from the window would make it look cheap and read
        `idle` for a session that had already completed — the number would
        improve and the answer would be wrong.
        """
        c, rt = client
        sid = rt.session_store.create_session()
        rt.session_store.append_user_turn(sid, "hello")
        rt.session_store.append_event(
            sid,
            {"type": "completion", "payload": {"done": True, "done_reason": "completed"}},
        )

        # Encoded, because `FUTURE` carries a `+` — sending it raw is the very
        # bug the sibling test pins, and it would 400 here.
        resp = c.get(
            f"/api/sessions/{sid}/events?after={quote(FUTURE, safe='')}",
            headers=_headers(),
        )

        body = resp.get_json()
        assert body["events"] == []
        assert body["status"] == "completed"
        assert body["title"] == "hello"
