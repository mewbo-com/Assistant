"""Concurrency tests for the stream admission primitive.

A single-threaded test cannot fail on the defect this guards: an admission
counter without a lock can let more than the configured limit through when
several callers race ``try_acquire`` at once, and that only shows up under
real thread contention. Every acquire test here drives many real OS threads
through a ``threading.Barrier`` so they call ``try_acquire`` at the same
instant, not one after another.
"""

from __future__ import annotations

import threading

from mewbo_api.stream_capacity import LeasedStream, StreamCapacity


def test_try_acquire_admits_exactly_the_limit_under_real_contention():
    """N threads race for a bound of 3; exactly 3 succeed, however they race."""
    limit = 3
    attempts = 20
    capacity = StreamCapacity(lambda: limit)
    barrier = threading.Barrier(attempts)
    results: list[bool | None] = [None] * attempts

    def race(i: int) -> None:
        barrier.wait(timeout=5)
        results[i] = capacity.try_acquire()

    threads = [threading.Thread(target=race, args=(i,)) for i in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert results.count(True) == limit
    assert results.count(False) == attempts - limit
    assert capacity.active == limit


def test_release_returns_a_slot_for_a_later_acquire():
    capacity = StreamCapacity(lambda: 1)
    assert capacity.try_acquire() is True
    assert capacity.try_acquire() is False
    capacity.release()
    assert capacity.active == 0
    assert capacity.try_acquire() is True


def test_release_clamps_at_zero():
    """A stray extra release must not manufacture a slot nobody holds."""
    capacity = StreamCapacity(lambda: 5)
    capacity.release()
    capacity.release()
    assert capacity.active == 0
    # Every configured slot is still available; none was lost to the clamp.
    for _ in range(5):
        assert capacity.try_acquire() is True
    assert capacity.try_acquire() is False


def test_limit_of_zero_is_unbounded():
    capacity = StreamCapacity(lambda: 0)
    for _ in range(50):
        assert capacity.try_acquire() is True
    assert capacity.active == 50


def test_limit_is_read_fresh_on_every_acquire():
    """A raised config value takes effect without reconstructing the object."""
    limit = {"value": 1}
    capacity = StreamCapacity(lambda: limit["value"])
    assert capacity.try_acquire() is True
    assert capacity.try_acquire() is False
    limit["value"] = 2
    assert capacity.try_acquire() is True


def test_leased_stream_releases_exactly_once_across_repeated_close():
    capacity = StreamCapacity(lambda: 1)
    capacity.try_acquire()
    leased = LeasedStream(iter(["a", "b"]), capacity)
    leased.close()
    leased.close()
    assert capacity.active == 0
    # The freed slot is real, not merely reported as free.
    assert capacity.try_acquire() is True


def test_leased_stream_releases_on_full_iteration():
    capacity = StreamCapacity(lambda: 1)
    capacity.try_acquire()
    leased = LeasedStream(iter(["a", "b"]), capacity)
    assert list(leased) == ["a", "b"]
    assert capacity.active == 0


def test_leased_stream_closes_the_wrapped_body():
    """The wrapped generator's own ``finally`` must run, not just the wrapper's."""
    released = []

    def frames():
        try:
            yield "a"
            yield "b"
        finally:
            released.append("inner-finally-ran")

    capacity = StreamCapacity(lambda: 1)
    capacity.try_acquire()
    leased = LeasedStream(frames(), capacity)
    next(iter(leased))
    leased.close()

    assert released == ["inner-finally-ran"]
    assert capacity.active == 0
