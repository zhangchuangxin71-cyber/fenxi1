from __future__ import annotations

import time
from typing import Any


class TraceCollector:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self._started = time.monotonic()
        self._events: list[dict[str, Any]] | None = [] if enabled else None

    def record(self, stage: str, **data: Any) -> None:
        if self._events is None:
            return
        self._events.append(
            {
                "stage": stage,
                "elapsed_ms": round((time.monotonic() - self._started) * 1000, 3),
                **data,
            }
        )

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._events or [])
