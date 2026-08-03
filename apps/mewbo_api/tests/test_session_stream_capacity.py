"""Regression tests for stream admission on the real HTTP route.

``test_stream_capacity.py`` proves the admission primitive is race-safe in
isolation; this file proves ``GET /api/sessions/{id}/stream`` actually wires
it in. Real threads open the route concurrently — a single-threaded test
cannot exercise the failure this guards, since the whole point is that
several callers reaching the gate at once must not all be admitted.

The Flask test client makes this reachable without a live socket: driving a
streaming ``Resource.get`` through it synchronously runs the admission check
and primes the generator for its first chunk before the call returns, so by
the time ``client.get(...)`` comes back, the slot is either held or the
request was refused — no separate "start consuming the body" step is needed
to observe either outcome.
"""

from __future__ import annotations

import threading

import pytest
from mewbo_api import backend
from mewbo_api.stream_capacity import StreamCapacity

_SESSION_ID = "stream-capacity-http-test-session"
_LIMIT = 3


@pytest.fixture()
def bounded_capacity():
    """An isolated, small capacity bound over an always-open test session.

    ``is_running`` is pinned True so a held stream never closes on its own —
    only the test's own ``.close()`` calls free a slot, which is what lets
    the test control exactly when admission is available again. Heartbeat
    timing is shortened via the generator's own keyword-only defaults so the
    handful of frames this test does read are fast.
    """
    original_capacity = backend.SessionStream.capacity
    original_is_running = backend.runtime.is_running
    stream_events = backend.SessionStream._stream_events
    original_kwdefaults = dict(stream_events.__kwdefaults__)

    test_capacity = StreamCapacity(lambda: _LIMIT)
    backend.SessionStream.capacity = test_capacity
    backend.runtime.is_running = lambda session_id: True
    stream_events.__kwdefaults__["heartbeat_s"] = 0.02
    stream_events.__kwdefaults__["idle_close_s"] = 1000.0

    try:
        yield test_capacity
    finally:
        backend.SessionStream.capacity = original_capacity
        backend.runtime.is_running = original_is_running
        stream_events.__kwdefaults__.update(original_kwdefaults)


def _open_stream(client, headers):
    return client.get(f"/api/sessions/{_SESSION_ID}/stream", headers=headers)


def test_admission_refuses_beyond_the_bound_under_real_concurrency(
    client, auth_headers, bounded_capacity
):
    """Twice the limit of threads race the route at once; only the limit gets in.

    An admission `threading.Barrier` forces every attempt to reach
    `try_acquire` at the same instant rather than one after another, so this
    can only pass if the bound is enforced under genuine contention, not
    merely under sequential calls. A second barrier holds every admitted
    stream open until the assertions below have inspected the shared state,
    then lets each thread close the response IT opened — a streamed response
    carries a Flask request context pushed onto that thread's own stack, so
    closing it from any other thread is a test-harness hazard the production
    code never faces (each real connection is served by, and closed from,
    exactly one thread).
    """
    attempts = _LIMIT * 2
    admit = threading.Barrier(attempts)
    ready = threading.Barrier(attempts + 1)
    release = threading.Barrier(attempts + 1)
    responses: list = [None] * attempts

    def attempt(i: int) -> None:
        admit.wait(timeout=5)
        responses[i] = _open_stream(client, auth_headers)
        ready.wait(timeout=5)
        release.wait(timeout=5)
        responses[i].close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(attempts)]
    for t in threads:
        t.start()

    # Every thread has its response (200 or 503) before any of them close.
    ready.wait(timeout=5)

    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == _LIMIT
    assert statuses.count(503) == attempts - _LIMIT
    assert bounded_capacity.active == _LIMIT

    release.wait(timeout=5)
    for t in threads:
        t.join(timeout=5)
    assert bounded_capacity.active == 0

    # Every slot freed on close; a fresh stream is admitted again.
    followup = _open_stream(client, auth_headers)
    try:
        assert followup.status_code == 200
    finally:
        followup.close()


def test_refusal_names_the_limit_and_sets_retry_after(client, auth_headers, bounded_capacity):
    holders = [_open_stream(client, auth_headers) for _ in range(_LIMIT)]
    try:
        refusal = _open_stream(client, auth_headers)
        try:
            assert refusal.status_code == 503
            assert refusal.headers.get("Retry-After")
            body = refusal.get_json()
            assert body["error"]["code"] == "stream_capacity_exhausted"
            assert body["error"]["retryable"] is True
            assert str(_LIMIT) in body["error"]["reason"]
        finally:
            refusal.close()
    finally:
        # Reverse of open order: opening several streams on ONE thread nests
        # Flask's per-thread request-context stack, so closing must unwind it
        # LIFO. Production never nests these — each connection gets its own
        # thread and pops its own single context — this ordering is purely to
        # keep the test's single-thread shortcut honest with that stack.
        for holder in reversed(holders):
            holder.close()


def test_route_forwards_the_after_cursor_and_still_leases_its_slot(
    client, auth_headers, bounded_capacity, monkeypatch
):
    """The route's ONE stream expression owes two unrelated things at once.

    It must forward ``?after=`` into the generator (the replay cursor, which
    is what lets an idle stream close and re-subscribe cheaply) AND wrap the
    body so the admission slot comes back. Both live in a single nested call,
    so dropping either one still compiles and still satisfies every test that
    checks only its own half — the cursor is otherwise exercised by calling
    the generator directly, never through the route. This pins the seam where
    the two meet.
    """
    seen: list[str | None] = []
    original = backend.SessionStream._stream_events

    def spy(session_id, session_runtime, bus, **kwargs):
        seen.append(kwargs.get("after"))
        return original(session_id, session_runtime, bus, **kwargs)

    monkeypatch.setattr(backend.SessionStream, "_stream_events", staticmethod(spy))

    cursor = "2026-06-07T00:00:01Z"
    response = client.get(
        f"/api/sessions/{_SESSION_ID}/stream?after={cursor}", headers=auth_headers
    )
    try:
        assert response.status_code == 200
        assert seen == [cursor]
        assert bounded_capacity.active == 1
    finally:
        response.close()
    assert bounded_capacity.active == 0


def test_a_refusal_holds_no_slot(client, auth_headers, bounded_capacity):
    """A refused request must not itself count against the bound it enforces."""
    holders = [_open_stream(client, auth_headers) for _ in range(_LIMIT)]
    try:
        for _ in range(5):
            refusal = _open_stream(client, auth_headers)
            refusal.close()
        assert bounded_capacity.active == _LIMIT
    finally:
        for holder in reversed(holders):
            holder.close()
