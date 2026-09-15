from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from pydantic import BaseModel

from app.events.internal import emit


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _bounded(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "...[truncated]"
    if isinstance(value, BaseModel):
        return _bounded(value.model_dump(), max_chars)
    if isinstance(value, dict):
        return {str(key): _bounded(item, max_chars) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bounded(item, max_chars) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return repr(value)


@dataclass(slots=True)
class DebugTraceCollector:
    run_id: str
    response_id: str
    request: dict[str, Any]
    max_chars: int
    max_bytes: int
    nodes: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=monotonic)
    truncated: bool = False

    def add(self, section: str, value: dict[str, Any]) -> None:
        target = getattr(self, section)
        target.append(_bounded(value, self.max_chars))

    def snapshot(self) -> dict[str, Any]:
        trace = {
            "request": _bounded(self.request, self.max_chars),
            "nodes": self.nodes,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "fallbacks": self.fallbacks,
            "errors": self.errors,
            "timings": {"elapsed_ms": round((monotonic() - self.started) * 1000, 3)},
            "truncated": self.truncated,
        }
        import json

        encoded = json.dumps(trace, ensure_ascii=False, default=str).encode()
        if len(encoded) <= self.max_bytes:
            return trace
        self.truncated = True
        trace["truncated"] = True
        for section in ("tool_calls", "llm_calls", "nodes"):
            items = trace[section]
            while items and len(json.dumps(trace, ensure_ascii=False, default=str).encode()) > self.max_bytes:
                items.pop(0)
        return trace


_current: contextvars.ContextVar[DebugTraceCollector | None] = contextvars.ContextVar(
    "wechat_article_debug_trace", default=None
)
_registry: dict[tuple[str, str], DebugTraceCollector] = {}


def get_or_create(
    *,
    enabled: bool,
    run_id: str,
    response_id: str,
    request: dict[str, Any],
    max_chars: int,
    max_bytes: int,
) -> DebugTraceCollector | None:
    if not enabled:
        return None
    key = (run_id, response_id)
    collector = _registry.get(key)
    if collector is None:
        collector = DebugTraceCollector(
            run_id=run_id,
            response_id=response_id,
            request=request,
            max_chars=max_chars,
            max_bytes=max_bytes,
        )
        _registry[key] = collector
    return collector


@contextmanager
def use_collector(collector: DebugTraceCollector | None) -> Any:
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


def current() -> DebugTraceCollector | None:
    return _current.get()


def record(section: str, value: dict[str, Any]) -> None:
    collector = current()
    if collector is not None:
        collector.add(section, {"timestamp": _now(), **value})


def emit_snapshot(collector: DebugTraceCollector | None = None, *, release: bool = False) -> None:
    selected = collector or current()
    if selected is not None:
        emit(
            "debug_snapshot",
            {
                "schema_version": "1",
                "trace": selected.snapshot(),
            },
        )
        if release:
            _registry.pop((selected.run_id, selected.response_id), None)
