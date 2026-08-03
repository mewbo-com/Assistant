#!/usr/bin/env python3
"""The bound that stops long-lived streams from spending the whole request pool.

Every request this API serves comes out of ONE process-wide pool of worker
threads, and a Server-Sent Events response holds its thread for the life of the
STREAM rather than for the work it does — a stream over a running session sits
blocked on its subscription queue until that run ends. So enough concurrent
streams exhaust the pool, and the exhaustion is **global**: session creation,
the session list, auth and health all queue behind streams that are doing
nothing but waiting. Measured on a pool of eight, holding eight streams open
took a trivial unrelated endpoint from tens of milliseconds to over ten
seconds. That reads as "the server is down", not as "the stream limit was
reached", which is the actual defect — the composition of an in-memory run
registry (one worker), a modest thread count, and a blocking generator turned a
stream limit into an availability cliff with no signal on it.

This module makes that failure explicit and local instead. Streams are admitted
up to a configured bound and refused past it, so an over-subscribed server
rejects a *stream* — a refusal a client can see, log and retry — while every
other endpoint keeps answering. It raises no ceiling and removes no ceiling; it
decides which request pays for one being reached.

Nothing here touches HTTP. The bound is arithmetic over a counter, so the route
owns minting the refusal and this stays testable without a request context.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import final


@final
class StreamCapacity:
    """How many long-lived stream responses may be open at once.

    The limit is read through an injected callable rather than captured at
    construction. An operator who raises it through the config API therefore
    does not have to restart the server for it to take effect, and a test binds
    a fixed bound by injecting one instead of reaching into the process-wide
    config cache.

    A limit of ``0`` (or less) means unbounded. Slots are still counted in that
    mode, so :attr:`active` reports the truth either way and a later change of
    the limit does not have to reconcile a counter that stopped tracking.
    """

    def __init__(self, limit_reader: Callable[[], int]) -> None:
        """Bind the *limit_reader* consulted on every admission decision."""
        self._limit_reader = limit_reader
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        """How many slots are held right now."""
        with self._lock:
            return self._active

    def limit(self) -> int:
        """The configured bound; ``0`` or less means unbounded."""
        return self._limit_reader()

    def try_acquire(self) -> bool:
        """Claim a slot, returning ``False`` when the bound is already met.

        Never blocks. A caller that waited for a slot would spend the wait
        holding the very thread the bound exists to protect, which is the
        failure this class was written to prevent rather than to reshape.
        """
        limit = self.limit()
        with self._lock:
            if 0 < limit <= self._active:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        """Return a slot. Clamped at zero, so a double release cannot mint one."""
        with self._lock:
            self._active = max(0, self._active - 1)


@final
class LeasedStream:
    """A stream body that returns its capacity slot exactly once.

    Releasing from the streaming generator's own ``finally`` is NOT sufficient,
    and the gap is the reason this class exists. A client that disconnects
    before the body is ever iterated leaves a generator whose body never
    started, and closing such a generator runs no ``finally`` at all — the slot
    would then be held until the process restarts, so the bound would leak
    itself shut under exactly the churn it is meant to survive. WSGI guarantees
    ``close()`` on the response iterable whether or not it was consumed, so the
    slot is returned from there and the iteration path funnels into the same
    method to keep the release single.

    Closing the wrapped body is part of that contract rather than tidiness: the
    server closes THIS object, never the generator inside it, and the wrapped
    body may hold a pushed request context that only its own close releases.
    """

    def __init__(self, frames: Iterator[str], capacity: StreamCapacity) -> None:
        """Wrap *frames*, owing one already-acquired slot back to *capacity*."""
        self._frames = frames
        self._capacity = capacity
        self._released = False

    def __iter__(self) -> Iterator[str]:
        """Yield the wrapped frames, returning the slot however iteration ends."""
        try:
            yield from self._frames
        finally:
            self.close()

    def close(self) -> None:
        """Close the wrapped body and return the slot, at most once.

        The release runs even if closing the wrapped body raises: the slot is
        the resource this class exists to guarantee, and a server pushing a
        request context around the body (``flask.stream_with_context``) can
        raise from that close on a context-stack mismatch. That is a defect
        in tracking the request context, not in whether the slot is free —
        one must never hide the other.
        """
        if self._released:
            return
        self._released = True
        try:
            closer = getattr(self._frames, "close", None)
            if closer is not None:
                closer()
        finally:
            self._capacity.release()
