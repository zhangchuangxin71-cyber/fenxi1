from __future__ import annotations

import asyncio
from time import monotonic

from app.core.errors import AppError


class AsyncTokenBucket:
    def __init__(
        self,
        *,
        per_minute: int,
        burst: int,
        error_code: str,
        message: str,
    ) -> None:
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.refill_per_second = per_minute / 60.0
        self.error_code = error_code
        self.message = message
        self.updated_at = monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            now = monotonic()
            self.tokens = min(
                self.capacity,
                self.tokens + (now - self.updated_at) * self.refill_per_second,
            )
            self.updated_at = now
            if self.tokens < 1:
                retry_after = max(1, int((1 - self.tokens) / self.refill_per_second) + 1)
                raise AppError(
                    429,
                    self.error_code,
                    self.message,
                    True,
                    details={"retry_after": retry_after},
                )
            self.tokens -= 1
