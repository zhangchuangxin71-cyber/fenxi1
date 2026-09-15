from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any

from app.api.schemas import SafeLLMCallSummary, TraceEvent


@dataclass
class _Span:
    collector: TraceCollector
    node: str
    phase: str
    input_counts: dict[str, int]
    group_refs: list[str]
    document_ids: list[str]
    started: float
    finished: bool = False

    def finish(
        self,
        *,
        output_counts: dict[str, int] | None = None,
        status: str = "ok",
        fallback_used: bool = False,
        error_category: str | None = None,
    ) -> None:
        if self.finished:
            return
        self.finished = True
        self.collector.record(
            node=self.node,
            phase=self.phase,
            status=status,
            duration_ms=int((monotonic() - self.started) * 1000),
            input_counts=self.input_counts,
            output_counts=output_counts or {},
            group_refs=self.group_refs,
            document_ids=self.document_ids,
            fallback_used=fallback_used,
            error_category=error_category,
        )


class TraceCollector:
    def __init__(self, *, enabled: bool, request_id: str) -> None:
        self.enabled = enabled
        self.request_id = request_id
        self._events: list[TraceEvent] = []
        self._sequence = 0
        self._details: list[dict[str, Any]] = []
        self._detail_sequence = 0

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    @property
    def details(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._details)

    def record_detail(
        self,
        *,
        node: str,
        kind: str,
        payload: dict[str, Any],
        group_ref: str | None = None,
        document_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        self._detail_sequence += 1
        self._details.append(
            {
                "sequence": self._detail_sequence,
                "node": node,
                "kind": kind,
                "group_ref": group_ref,
                "document_id": document_id,
                "payload": payload,
            }
        )

    @contextmanager
    def span(
        self,
        *,
        node: str,
        phase: str,
        input_counts: dict[str, int] | None = None,
        group_refs: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> Iterator[_Span]:
        span = _Span(
            collector=self,
            node=node,
            phase=phase,
            input_counts=input_counts or {},
            group_refs=group_refs or [],
            document_ids=document_ids or [],
            started=monotonic(),
        )
        try:
            yield span
        except Exception as exc:
            span.finish(status="failed", error_category=getattr(exc, "error_category", "internal"))
            raise
        finally:
            if not span.finished:
                span.finish()

    def record(
        self,
        *,
        node: str,
        phase: str,
        status: str,
        duration_ms: int,
        input_counts: dict[str, int],
        output_counts: dict[str, int],
        group_refs: list[str],
        document_ids: list[str],
        fallback_used: bool = False,
        error_category: str | None = None,
        llm: SafeLLMCallSummary | None = None,
    ) -> None:
        if not self.enabled:
            return
        self._sequence += 1
        self._events.append(
            TraceEvent(
                sequence=self._sequence,
                node=node,
                phase=phase,
                status=status,  # type: ignore[arg-type]
                duration_ms=duration_ms,
                input_counts=dict(input_counts),
                output_counts=dict(output_counts),
                group_refs=list(group_refs),
                document_ids=list(document_ids),
                fallback_used=fallback_used,
                error_category=error_category,
                llm=llm,
            )
        )
