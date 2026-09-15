from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from time import monotonic
from typing import Literal


class CircuitOpenError(RuntimeError):
    error_category = "circuit_open"


class LLMCircuitBreaker:
    def __init__(
        self,
        *,
        enabled: bool,
        failure_threshold: int = 3,
        recovery_seconds: float = 30.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.enabled = enabled
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_seconds = max(0.1, float(recovery_seconds))
        self._clock = clock
        self._lock = Lock()
        self._state: Literal["closed", "open", "half_open"] = "closed"
        self._opened_at = 0.0
        self._consecutive_failures = 0

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        with self._lock:
            return self._state

    def before_call(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._state == "closed":
                return False
            if self._state == "open" and self._clock() - self._opened_at >= self.recovery_seconds:
                self._state = "half_open"
                return True
            raise CircuitOpenError("LLM circuit breaker is open")

    def record_failure(self, category: str) -> None:
        if not self.enabled or category == "invalid_response":
            return
        with self._lock:
            if self._state == "half_open":
                self._open_locked()
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._open_locked()

    def record_success(self, *, recovery_probe: bool) -> None:
        if not self.enabled:
            return
        with self._lock:
            if recovery_probe and self._state == "half_open":
                self._state = "closed"
            if self._state == "closed":
                self._consecutive_failures = 0

    def _open_locked(self) -> None:
        self._state = "open"
        self._opened_at = self._clock()
        self._consecutive_failures = self.failure_threshold
