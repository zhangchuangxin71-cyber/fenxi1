from __future__ import annotations

from app.observability.trace import TraceCollector


def test_trace_collector_returns_counts_without_page_content() -> None:
    collector = TraceCollector(enabled=True, request_id="req-1")
    with collector.span(
        node="classification",
        phase="llm",
        input_counts={"groups": 1},
        group_refs=["g0001"],
    ) as span:
        span.finish(output_counts={"groups": 2}, status="ok")

    events = collector.events

    assert len(events) == 1
    assert events[0].sequence == 1
    assert events[0].duration_ms is not None
    assert events[0].input_counts == {"groups": 1}
    assert "content" not in events[0].model_dump_json()
