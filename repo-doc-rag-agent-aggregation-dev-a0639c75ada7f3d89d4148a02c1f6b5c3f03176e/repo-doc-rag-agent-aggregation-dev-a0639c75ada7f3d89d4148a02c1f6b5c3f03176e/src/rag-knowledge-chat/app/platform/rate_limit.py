from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable

from app.platform.errors import ApiError


class InMemoryRateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        period_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._period = period_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, *, cost: int = 1) -> None:
        async with self._lock:
            now = self._clock()
            events = self._events[key]
            cutoff = now - self._period
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) + cost > self._limit:
                raise ApiError(
                    429,
                    "rate_limit_exceeded",
                    "request rate limit exceeded",
                    retryable=True,
                )
            events.extend([now] * cost)
