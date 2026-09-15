"""Pipeline step and sub-operation timing helpers."""

from __future__ import annotations

import time
from typing import Callable, Optional

LogFn = Optional[Callable[[str], None]]


def format_elapsed(sec: float) -> str:
    """Human-readable wall-clock duration."""
    if sec < 0:
        return "0.0s"
    if sec < 60:
        return f"{sec:.1f}s"
    minutes = int(sec // 60)
    remainder = sec - minutes * 60
    return f"{minutes}m{remainder:.1f}s"


def now() -> float:
    return time.perf_counter()


def emit(msg: str, log_fn: LogFn = None) -> None:
    print(msg, flush=True)
    if log_fn:
        log_fn(msg)


class StepTimer:
    """Context manager: prints elapsed time when the step finishes."""

    def __init__(self, label: str, *, log_fn: LogFn = None):
        self.label = label
        self.log_fn = log_fn
        self.elapsed = 0.0
        self._t0 = 0.0

    def __enter__(self) -> StepTimer:
        self._t0 = now()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.elapsed = now() - self._t0
        emit(f"  [time] {self.label} · {format_elapsed(self.elapsed)}", self.log_fn)
        return False
