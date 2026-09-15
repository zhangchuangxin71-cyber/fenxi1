from __future__ import annotations

import json

import httpx
import pytest

from app.chat.models import RetrievalResult, RetrievalStatus, RouteDecision
from app.integrations.ark import AnswerReasoningDelta, AnswerTextDelta, AnswerThinkingStarted
from app.main import create_app
from app.platform.errors import ApiError
from app.platform.settings import Settings
from tests.conftest import AllowLimiter, FakeArk, FakeRetrieval


def settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "APP_ENV": "test",
        "AUTH_ENABLED": True,
        "KNOWLEDGE_CHAT_INBOUND_API_KEYS": "test-client:rag-key",
        "DEBUG_ENABLED": True,
    }
    values.update(overrides)
    return Settings(**values)


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        if not block.startswith("data: "):
            continue
        data = block.removeprefix("data: ")
        if data != "[DONE]":
            events.append(json.loads(data))
    return events


def test_openapi_documents_the_streaming_success_payload() -> None:
    app = create_app(
        settings=settings(),
        ark=FakeArk(RouteDecision(needs_retrieval=False, query="", reason_code="identity")),
        retrieval=FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT)),
        request_limiter=AllowLimiter(),
        ark_limiter=AllowLimiter(),
    )

    schema = app.openapi()
    response = schema["paths"]["/v1/chat/completions"]["post"]["responses"]["200"]

    assert set(response["content"]) == {"text/event-stream"}
    event_schema = response["content"]["text/event-stream"]["schema"]
    assert event_schema["$ref"] == "#/components/schemas/ChatCompletionStreamChunk"
    chunk = schema["components"]["schemas"]["ChatCompletionStreamChunk"]
    assert {"id", "object", "created", "model", "choices"} <= set(chunk["properties"])
    delta_ref = schema["components"]["schemas"]["ChatCompletionStreamChoice"]["properties"]["delta"]["$ref"]
    assert delta_ref.endswith("/ChatCompletionStreamDelta")
    delta = schema["components"]["schemas"]["ChatCompletionStreamDelta"]["properties"]
    assert {"content", "reasoning_content"} <= set(delta)
    rag = schema["components"]["schemas"]["ChatCompletionRagPayload"]["properties"]
    assert {"event", "answer_basis", "retrieval", "references", "chunks", "debug", "error"} <= set(rag)


def test_create_app_passes_configured_timeout_to_retrieval_client(monkeypatch) -> None:
    captured = {}

    class CapturingRetrieval:
        def __init__(self, *, base_url, timeout_seconds, max_return_tokens):
            captured.update(
                base_url=base_url,
                timeout_seconds=timeout_seconds,
                max_return_tokens=max_return_tokens,
            )

        async def close(self):
            return None

    monkeypatch.setattr("app.main.RetrievalClient", CapturingRetrieval)

    app = create_app(
        settings=settings(
            RAG_RETRIEVAL_SERVICE_URL="http://retrieval:8120",
            RAG_RETRIEVAL_TIMEOUT_SECONDS=245,
            RAG_MAX_RETURN_TOKENS=180_000,
        ),
        ark=FakeArk(RouteDecision(needs_retrieval=False, query="", reason_code="identity")),
        request_limiter=AllowLimiter(),
        ark_limiter=AllowLimiter(),
    )

    assert captured == {
        "base_url": "http://retrieval:8120",
        "timeout_seconds": 245.0,
        "max_return_tokens": 180_000,
    }
    assert app.state.services.orchestrator._deadline_seconds == 320.0


@pytest.mark.asyncio
async def test_configured_return_token_limit_rejects_before_ark_and_sse(base_payload) -> None:
    base_payload["rag"]["max_return_tokens"] = 180_001
    ark = FakeArk(RouteDecision(needs_retrieval=False, query="", reason_code="identity"))
    retrieval = FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT))
    app = create_app(
        settings=settings(RAG_MAX_RETURN_TOKENS=180_000),
        ark=ark,
        retrieval=retrieval,
        request_limiter=AllowLimiter(),
        ark_limiter=AllowLimiter(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json=base_payload,
            headers={"Authorization": "Bearer rag-key"},
        )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["details"]["validation_errors"][0]["location"] == [
        "body",
        "rag",
        "max_return_tokens",
    ]
    assert ark.decision_calls == []
    assert retrieval.calls == []


@pytest.mark.asyncio
async def test_valid_request_streams_status_text_metadata_and_done(base_payload) -> None:
    base_payload["rag"]["include_debug"] = True
    ark = FakeArk(
        RouteDecision(needs_retrieval=False, query="", reason_code="identity"),
        deltas=[
            AnswerThinkingStarted(),
            AnswerReasoningDelta(delta="身份推理"),
            AnswerTextDelta(delta="身份回答"),
        ],
    )
    app = create_app(
        settings=settings(),
        ark=ark,
        retrieval=FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT)),
        request_limiter=AllowLimiter(),
        ark_limiter=AllowLimiter(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json=base_payload,
            headers={"Authorization": "Bearer rag-key"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.rstrip().endswith("data: [DONE]")
    events = parse_sse(response.text)
    assert events[0]["rag"]["event"]["stage"] == "route"
    thinking = next(
        event for event in events if event.get("rag", {}).get("event", {}).get("stage") == "thinking"
    )
    assert thinking["rag"]["event"]["message"] == "正在思考"
    assert thinking["choices"][0]["delta"] == {}
    reasoning = next(
        event for event in events if event.get("choices", [{}])[0].get("delta", {}).get("reasoning_content")
    )
    assert reasoning["choices"][0]["delta"]["reasoning_content"] == "身份推理"
    assert any(event.get("choices", [{}])[0].get("delta", {}).get("content") for event in events)
    assert events[-1]["rag"]["answer_basis"] == "general_no_retrieval"
    assert events[-1]["rag"]["debug"]["trace"][0]["stage"] == "request_validated"
    assert "X-Request-Id" in response.headers
    assert len(ark.decision_calls) == 1
    assert len(ark.answer_calls) == 1


@pytest.mark.asyncio
async def test_disabled_inbound_auth_does_not_forward_authorization(base_payload) -> None:
    retrieval = FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT))
    app = create_app(
        settings=settings(
            AUTH_ENABLED=False,
            KNOWLEDGE_CHAT_INBOUND_API_KEYS="",
        ),
        ark=FakeArk(RouteDecision(needs_retrieval=True, query="审批流程", reason_code="knowledge_base")),
        retrieval=retrieval,
        request_limiter=AllowLimiter(),
        ark_limiter=AllowLimiter(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", json=base_payload)

    assert response.status_code == 200
    assert "authorization" not in retrieval.calls[0]


@pytest.mark.asyncio
async def test_invalid_key_fails_before_any_ark_call(base_payload) -> None:
    ark = FakeArk(RouteDecision(needs_retrieval=False, query="", reason_code="identity"))
    app = create_app(
        settings=settings(),
        ark=ark,
        retrieval=FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT)),
        request_limiter=AllowLimiter(),
        ark_limiter=AllowLimiter(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json=base_payload,
            headers={"Authorization": "Bearer wrong"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert ark.decision_calls == []


class RejectLimiter:
    async def acquire(self, key: str, *, cost: int = 1) -> None:
        raise ApiError(429, "rate_limit_exceeded", "too many requests", retryable=True)


@pytest.mark.asyncio
async def test_first_ark_limit_is_checked_before_sse(base_payload) -> None:
    ark = FakeArk(RouteDecision(needs_retrieval=False, query="", reason_code="identity"))
    app = create_app(
        settings=settings(),
        ark=ark,
        retrieval=FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT)),
        request_limiter=AllowLimiter(),
        ark_limiter=RejectLimiter(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json=base_payload,
            headers={"Authorization": "Bearer rag-key"},
        )

    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/json")
    assert ark.decision_calls == []


@pytest.mark.asyncio
async def test_model_alias_is_validated_before_sse(base_payload) -> None:
    base_payload["model"] = "arbitrary-upstream-model"
    app = create_app(
        settings=settings(),
        ark=FakeArk(RouteDecision(needs_retrieval=False, query="", reason_code="identity")),
        retrieval=FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT)),
        request_limiter=AllowLimiter(),
        ark_limiter=AllowLimiter(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json=base_payload,
            headers={"Authorization": "Bearer rag-key"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_model"


@pytest.mark.asyncio
async def test_pydantic_errors_use_stable_400_shape(base_payload) -> None:
    base_payload["stream"] = False
    app = create_app(
        settings=settings(),
        ark=FakeArk(RouteDecision(needs_retrieval=False, query="", reason_code="identity")),
        retrieval=FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT)),
        request_limiter=AllowLimiter(),
        ark_limiter=AllowLimiter(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json=base_payload,
            headers={"Authorization": "Bearer rag-key"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_temporary_documents_require_session_id_before_sse(base_payload) -> None:
    base_payload["rag"].update({"doc_ids": [], "temp_doc_ids": ["temp-doc-1"]})
    ark = FakeArk(RouteDecision(needs_retrieval=True, query="临时文档", reason_code="knowledge_base"))
    retrieval = FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT))
    app = create_app(
        settings=settings(),
        ark=ark,
        retrieval=retrieval,
        request_limiter=AllowLimiter(),
        ark_limiter=AllowLimiter(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json=base_payload,
            headers={"Authorization": "Bearer rag-key"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert ark.decision_calls == []
    assert retrieval.calls == []
