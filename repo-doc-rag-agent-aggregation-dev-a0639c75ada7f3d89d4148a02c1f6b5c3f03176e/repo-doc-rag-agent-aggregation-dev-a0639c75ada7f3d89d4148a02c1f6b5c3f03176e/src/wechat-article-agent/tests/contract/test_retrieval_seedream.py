from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from app.config import AppSettings
from app.core.errors import AppError
from app.debug.trace import DebugTraceCollector, use_collector
from app.integrations.retrieval import RetrievalClient
from app.integrations.seedream import SeedreamClient

PROJECT = Path(__file__).parents[2]
RETRIEVAL_URL = "http://retrieval.example"
SEEDREAM_URL = "https://ark.example/api/v3/images/generations"


def _settings(**overrides: object) -> AppSettings:
    values: dict[str, object] = {}
    for raw_line in (PROJECT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    values.update(
        {
            "ARK_API_KEY": "test-key",
            "RETRIEVAL_BASE_URL": RETRIEVAL_URL,
            "SEEDREAM_RESPONSES_URL": SEEDREAM_URL,
            "RETRIEVAL_MAX_RETRIES": 0,
            "SEEDREAM_MAX_RETRIES": 0,
        }
    )
    values.update(overrides)
    return AppSettings.model_validate(values)


@pytest.mark.contract
@respx.mock
async def test_raw_unavailable_has_stable_error_and_retrieve_contract() -> None:
    raw_route = respx.post(f"{RETRIEVAL_URL}/rag/v1/documents/raw").mock(
        return_value=httpx.Response(422, json={"error": {"code": "RAW_UNAVAILABLE"}})
    )
    retrieve_route = respx.post(f"{RETRIEVAL_URL}/rag/v1/retrieve").mock(
        return_value=httpx.Response(
            200,
            json={
                "chunks": [{"chunk_id": "chunk-1", "content": "evidence"}],
                "coverage": {"complete": True},
            },
        )
    )
    client = RetrievalClient(_settings())
    collector = DebugTraceCollector(
        run_id="run-retrieval",
        response_id="resp-retrieval",
        request={},
        max_chars=100_000,
        max_bytes=1_000_000,
    )

    with use_collector(collector):
        with pytest.raises(AppError) as caught:
            await client.raw(user_id="user", kb_id="kb", doc_id="doc-1")
        result = await client.retrieve(
            user_id="user",
            kb_id="kb",
            session_id="session",
            doc_ids=["doc-1"],
            temp_doc_ids=[],
            queries=["详细总结这篇文档"],
            ensure_document_coverage=True,
        )
    await client.close()

    assert caught.value.code == "RAW_UNAVAILABLE"
    assert result["chunks"][0]["chunk_id"] == "chunk-1"
    assert raw_route.called and retrieve_route.called
    payload = retrieve_route.calls[0].request.content.decode()
    assert "详细总结这篇文档" in payload
    assert '"ensure_document_coverage":true' in payload
    assert len(collector.tool_calls) == 2
    assert collector.tool_calls[1]["kind"] == "tool"
    assert collector.tool_calls[1]["status"] == "completed"
    assert collector.tool_calls[1]["arguments"]["query"] == ["详细总结这篇文档"]
    assert collector.tool_calls[1]["result"]["chunks"][0]["chunk_id"] == "chunk-1"


@pytest.mark.contract
@respx.mock
async def test_seedream_retries_transient_failure_and_returns_only_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = respx.post(SEEDREAM_URL).mock(
        side_effect=[
            httpx.Response(503, json={"error": {"message": "busy"}}),
            httpx.Response(200, json={"data": [{"url": "https://images.example/result.png"}]}),
        ]
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    client = SeedreamClient(_settings(SEEDREAM_MAX_RETRIES=1, SEEDREAM_RETRY_BASE_SECONDS=0.001))
    collector = DebugTraceCollector(
        run_id="run-image",
        response_id="resp-image",
        request={},
        max_chars=100_000,
        max_bytes=1_000_000,
    )

    with use_collector(collector):
        result = await client.generate("一张银行年报分析配图")
    await client.close()

    assert result == "https://images.example/result.png"
    assert route.call_count == 2
    payload = json.loads(route.calls[1].request.content)
    assert payload["response_format"] == "url"
    assert payload["watermark"] is False
    assert payload["tools"] == [{"type": "web_search"}]
    assert collector.tool_calls[0]["tool"] == "seedream.generate"
    assert collector.tool_calls[0]["status"] == "completed"
    assert collector.tool_calls[0]["attempts"] == 2
    assert collector.tool_calls[0]["result"] == {"url": "https://images.example/result.png"}
    assert collector.tool_calls[0]["arguments"]["web_search_enabled"] is True


@pytest.mark.contract
@respx.mock
async def test_seedream_does_not_send_web_search_to_other_models() -> None:
    route = respx.post(SEEDREAM_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"url": "https://images.example/result.png"}]})
    )
    client = SeedreamClient(_settings(SEEDREAM_MODEL="another-seedream-model"))

    await client.generate("一张配图")
    await client.close()

    payload = json.loads(route.calls[0].request.content)
    assert "tools" not in payload


@pytest.mark.contract
@respx.mock
async def test_seedream_rejects_success_response_without_image_url() -> None:
    respx.post(SEEDREAM_URL).mock(return_value=httpx.Response(200, json={"data": [{}]}))
    client = SeedreamClient(_settings())
    collector = DebugTraceCollector(
        run_id="run-invalid-image",
        response_id="resp-invalid-image",
        request={},
        max_chars=100_000,
        max_bytes=1_000_000,
    )

    with use_collector(collector):
        with pytest.raises(AppError) as caught:
            await client.generate("prompt")
    await client.close()

    assert caught.value.code == "SEEDREAM_INVALID_RESPONSE"
    assert collector.tool_calls[0]["status"] == "failed"
    assert collector.tool_calls[0]["arguments"]["prompt"] == "prompt"
    assert collector.tool_calls[0]["error"]["code"] == "SEEDREAM_INVALID_RESPONSE"
