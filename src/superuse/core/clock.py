"""Injectable monotonic clocks.

All control timing uses the monotonic clock (see docs/development/control-state-machine.md).
Tests inject :class:`VirtualClock` and advance time explicitly so races are
scheduled deterministically instead of relying on real sleeps.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from typing import Protocol


class Clock(Protocol):
    """Monotonic time source plus cancellable async sleep."""

    @property
    def now(self) -> float: ...

    def sleep(self, delay: float) -> asyncio.Future[None]:
        """Return a future resolved after ``delay`` monotonic seconds.

        The future can be cancelled by the caller to abort a wait; that is how
        report-level steps of long actions are cancelled.
        """


class MonotonicClock:
    """Real clock backed by ``time.monotonic`` and ``asyncio.sleep``."""

    @property
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, delay: float) -> asyncio.Future[None]:
        return asyncio.ensure_future(asyncio.sleep(max(0.0, delay)))


class VirtualClock:
    """Manually advanced clock for deterministic tests.

    ``sleep`` registers a timer; ``advance`` moves time forward, firing timers
    in deadline order and yielding to the event loop between firings so
    concurrent coroutines interleave exactly as they would in real time.
    """

    def __init__(self) -> None:
        self._now = 0.0
        self._seq = 0
        self._heap: list[tuple[float, int, asyncio.Future[None]]] = []

    @property
    def now(self) -> float:
        return self._now

    def sleep(self, delay: float) -> asyncio.Future[None]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        heapq.heappush(self._heap, (self._now + max(0.0, delay), self._seq, future))
        self._seq += 1
        return future

    async def advance(self, duration: float) -> None:
        target = self._now + max(0.0, duration)
        while self._heap and self._heap[0][0] <= target:
            deadline, _, future = heapq.heappop(self._heap)
            self._now = deadline
            if not future.done():
                future.set_result(None)
            # Let every runnable coroutine observe the tick before later
            # timers fire, keeping ordering deterministic.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self._now = target

    @property
    def pending_timers(self) -> int:
        return sum(1 for _, _, fut in self._heap if not fut.done())
