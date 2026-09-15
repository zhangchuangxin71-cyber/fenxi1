from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    def __init__(
        self,
        *,
        enabled: bool,
        per_minute: int,
        burst: int,
        idle_ttl_seconds: float = 900.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.enabled = enabled
        self.per_minute = max(1, int(per_minute))
        self.burst = max(1, int(burst))
        self.idle_ttl_seconds = max(1.0, float(idle_ttl_seconds))
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    @property
    def bucket_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def allow(self, user_id: str) -> tuple[bool, float]:
        if not self.enabled:
            return True, 0.0
        now = self._clock()
        refill_rate = self.per_minute / 60.0
        with self._lock:
            self._buckets = {
                key: bucket
                for key, bucket in self._buckets.items()
                if now - bucket.updated_at <= self.idle_ttl_seconds
            }
            bucket = self._buckets.get(user_id)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.burst), updated_at=now)
                self._buckets[user_id] = bucket
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * refill_rate)
            bucket.updated_at = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            return False, max(0.1, (1.0 - bucket.tokens) / refill_rate)
