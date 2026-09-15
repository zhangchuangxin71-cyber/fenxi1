from __future__ import annotations

import asyncio
from time import monotonic

from app.core.errors import AppError


class CircuitBreaker:
    """Small process-local closed/open/half-open circuit breaker."""

    def __init__(
        self,
        *,
        enabled: bool,
        threshold: int,
        recovery_seconds: float,
        error_code: str,
        provider_name: str,
    ) -> None:
        self.enabled = enabled
        self.threshold = max(1, threshold)
        self.recovery_seconds = recovery_seconds
        self.error_code = error_code
        self.provider_name = provider_name
        self.failures = 0
        self.opened_at: float | None = None
        self.half_open_in_flight = False
        self._lock = asyncio.Lock()

    async def before_call(self) -> bool:
        if not self.enabled:
            return False
        async with self._lock:
            if self.opened_at is None:
                return False
            if monotonic() - self.opened_at < self.recovery_seconds:
                raise self._open_error("circuit breaker is open")
            if self.half_open_in_flight:
                raise self._open_error("recovery probe is already running")
            self.half_open_in_flight = True
            return True

    async def success(self, recovery_probe: bool) -> None:
        if not self.enabled:
            return
        async with self._lock:
            if recovery_probe:
                self.half_open_in_flight = False
                self.opened_at = None
            self.failures = 0

    async def failure(self, *, counts: bool, recovery_probe: bool) -> None:
        if not self.enabled:
            return
        async with self._lock:
            if recovery_probe:
                self.half_open_in_flight = False
            if not counts:
                return
            self.failures += 1
            if recovery_probe or self.failures >= self.threshold:
                self.opened_at = monotonic()

    def _open_error(self, detail: str) -> AppError:
        return AppError(
            503,
            self.error_code,
            f"{self.provider_name} {detail}.",
            True,
        )
