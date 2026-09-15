import pytest
from httpx import ASGITransport, AsyncClient

from app.llm.registry import clear_providers, register
from main import create_app

from .conftest import MockLLMProvider


@pytest.mark.asyncio
async def test_health_endpoint():
    clear_providers()
    register("doubao", MockLLMProvider())
    app = create_app(init_llm=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_sse_endpoint_returns_error_event_on_llm_failure():
    clear_providers()
    register("doubao", MockLLMProvider(fail=True))
    app = create_app(init_llm=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/agent/v1/chat/completions",
            json={"session_id": "s", "user_id": "user", "kb_id": "kb", "query": "什么是测试？"},
        )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "event: stream_end" in response.text


@pytest.mark.asyncio
async def test_chat_endpoint_normalizes_wrapped_current_outline(monkeypatch):
    clear_providers()
    register("doubao", MockLLMProvider())
    captured = {}

    async def fake_safe_run_graph(graph, state, emitter):
        captured["state"] = state
        return {"usage": {}}

    monkeypatch.setattr("app.api.routes.safe_run_graph", fake_safe_run_graph)
    app = create_app(init_llm=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/agent/v1/chat/completions",
            json={
                "session_id": "s",
                "user_id": "user",
                "kb_id": "kb",
                "query": "OK,开始写吧",
                "report_context": {
                    "current_outline": {
                        "outline": {
                            "title": "《马飞研究员个人介绍报告》",
                            "sections": [
                                {
                                    "index": 1,
                                    "title": "一、报告摘要",
                                    "subsections": [],
                                },
                                {
                                    "index": 2,
                                    "title": "二、个人基本概况",
                                    "subsections": [{"index": "2.1", "title": "基础身份信息"}],
                                },
                            ],
                        }
                    },
                    "outline_confirmed": True,
                },
            },
        )

    assert response.status_code == 200
    outline = captured["state"]["current_outline"]
    assert outline["title"] == "《马飞研究员个人介绍报告》"
    assert outline["sections"][0]["title"] == "一、报告摘要"
    assert outline["sections"][1]["subsections"][0]["title"] == "基础身份信息"
