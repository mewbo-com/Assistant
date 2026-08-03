"""The run's cooperative stop signal, expressed as something a wait can lose to.

A run is stopped by setting a flag the loop polls (``AgentContext.should_cancel``,
backed by a :class:`threading.Event` the run registry owns). Polling is the right
mechanism — nothing here changes it — but a poll only bounds the stop by whatever
the loop happens to be awaiting when the flag flips. A model generation and a tool
call are both unbounded from the loop's point of view, so a poll placed ONLY between
turns makes the observed stop latency the duration of a whole turn: the operator
clicks Stop and watches the run make another model call and finish another tool.

:class:`CancellationSignal` turns the same predicate into a barrier an in-flight
await can be RACED against, so a stop lands within one poll interval of the request
instead of one turn. The predicate is injected, so nothing in here reads a clock, an
event object or a registry — a test drives it with a plain callable over a list.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import ClassVar, TypeVar

_T = TypeVar("_T")


class RunCancelled(Exception):
    """Raised by :meth:`CancellationSignal.guard` when a stop was requested.

    Carries no payload: the only fact it reports is that the operator asked for the
    run to stop, and the loop's own terminal vocabulary (``done_reason="canceled"``)
    is where that becomes a status.
    """


class CancellationSignal:
    """A stop predicate, readable synchronously and awaitable asynchronously.

    Collaborator by injection: *predicate* is whatever the run's owner uses to say
    "stop" — ``threading.Event.is_set`` in production. ``None`` makes the signal
    permanently inert, which is what every caller that runs without a run registry
    (the CLI's direct ``Orchestrator`` use, most tests) gets for free, so no call
    site needs a None-check of its own.

    Cost: :attr:`requested` is one predicate call, `O(1)`. :meth:`guard` adds one
    task and one poll loop for the life of the awaitable it wraps.
    """

    #: How often :meth:`guard` re-reads the predicate. This is the stop's worst-case
    #: latency, so it is short; it is not a timeout and never bounds the work.
    POLL_INTERVAL_SECONDS: ClassVar[float] = 0.1

    def __init__(
        self,
        predicate: Callable[[], bool] | None,
        *,
        poll_interval: float | None = None,
    ) -> None:
        """Bind the stop *predicate*; ``None`` yields a permanently inert signal."""
        self._predicate = predicate
        self._poll_interval = (
            poll_interval if poll_interval is not None else self.POLL_INTERVAL_SECONDS
        )

    @property
    def inert(self) -> bool:
        """True when no predicate was injected, so this signal can never fire."""
        return self._predicate is None

    @property
    def requested(self) -> bool:
        """Whether a stop has been requested.

        A raising predicate reads as "not requested": the flag is a best-effort
        steering signal, and a fault reading it must not itself terminate the run.
        """
        if self._predicate is None:
            return False
        try:
            return bool(self._predicate())
        except Exception:
            return False

    async def _until_requested(self) -> None:
        """Return once :attr:`requested` is true, polling at the configured interval."""
        while not self.requested:
            await asyncio.sleep(self._poll_interval)

    async def guard(self, awaitable: Awaitable[_T]) -> _T:
        """Await *awaitable*, aborting it as soon as a stop is requested.

        Raises :class:`RunCancelled` instead of returning, having cancelled the
        wrapped awaitable and waited for it to unwind. An inert signal awaits
        *awaitable* directly, adding no task and no poll, so a caller without a
        registry pays nothing for the guard.

        The pre-check matters: a stop requested while the previous await was
        finishing must not buy the run one more unbounded call.
        """
        if self._predicate is None:
            return await awaitable
        if self.requested:
            await self._discard(awaitable)
            raise RunCancelled()

        work = asyncio.ensure_future(awaitable)
        watch = asyncio.ensure_future(self._until_requested())
        try:
            done, _ = await asyncio.wait(
                {work, watch}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # The whole run is being torn down; drop both legs and propagate.
            work.cancel()
            watch.cancel()
            raise
        finally:
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch

        if work in done:
            # Surfaces the wrapped awaitable's own exception unchanged, which is
            # what keeps every existing handler around a guarded call working.
            return work.result()

        work.cancel()
        with contextlib.suppress(BaseException):
            await work
        raise RunCancelled()

    @staticmethod
    async def _discard(awaitable: Awaitable[_T]) -> None:
        """Close an awaitable that will never be awaited, so it leaves no warning.

        A coroutine built at the call site is already constructed by the time the
        pre-check refuses it; dropping it unstarted emits a "never awaited" warning
        that reads as a defect. Closing it is the honest disposal.
        """
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
            return
        task = asyncio.ensure_future(awaitable)
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
