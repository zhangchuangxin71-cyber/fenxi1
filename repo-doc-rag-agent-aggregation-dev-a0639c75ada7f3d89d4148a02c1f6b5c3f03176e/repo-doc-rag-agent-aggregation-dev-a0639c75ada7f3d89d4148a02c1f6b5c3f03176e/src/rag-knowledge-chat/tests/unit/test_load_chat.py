from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from scripts.load_chat import (
    LoadConfig,
    LoadRun,
    RequestResult,
    percentile,
    run_load,
    summarize,
)


def _sse_body() -> str:
    text = {
        "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}],
    }
    complete = {
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "rag": {"answer_basis": "general_no_retrieval"},
    }
    return f"data: {json.dumps(text)}\n\ndata: {json.dumps(complete)}\n\ndata: [DONE]\n\n"


def test_percentile_uses_nearest_rank_without_mutating_input() -> None:
    values = [30.0, 10.0, 20.0]

    assert percentile(values, 0.50) == 20.0
    assert percentile(values, 0.95) == 30.0
    assert values == [30.0, 10.0, 20.0]


@pytest.mark.asyncio
async def test_run_load_respects_concurrency_and_consumes_sse() -> None:
    active = 0
    observed_max = 0
    lock = asyncio.Lock()

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal active, observed_max
        async with lock:
            active += 1
            observed_max = max(observed_max, active)
        await asyncio.sleep(0.01)
        async with lock:
            active -= 1
        return httpx.Response(
            200,
            headers={"X-Request-Id": "req-test", "Content-Type": "text/event-stream"},
            text=_sse_body(),
        )

    config = LoadConfig(
        base_url="http://test",
        api_key="",
        request_count=8,
        concurrency=3,
        timeout_seconds=2.0,
        payload={
            "model": "rag-knowledge-chat",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "rag": {"user_id": "load-user", "kb_id": "load-kb"},
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        run = await run_load(config, client=client)

    summary = summarize(run)
    assert observed_max == 3
    assert run.max_in_flight == 3
    assert summary["requests"] == 8
    assert summary["successful"] == 8
    assert summary["app_rate_limited"] == 0
    assert summary["upstream_errors"] == 0
    assert summary["done_received"] == 8


def test_summary_separates_local_rate_limit_from_upstream_errors() -> None:
    run = LoadRun(
        results=(
            RequestResult(index=0, status_code=429, total_ms=1.0, error_code="rate_limit_exceeded"),
            RequestResult(index=1, status_code=200, total_ms=2.0, error_code="ark_upstream_error"),
        ),
        max_in_flight=2,
        wall_time_ms=2.5,
    )

    summary = summarize(run)

    assert summary["app_rate_limited"] == 1
    assert summary["upstream_errors"] == 1
    assert summary["successful"] == 0
