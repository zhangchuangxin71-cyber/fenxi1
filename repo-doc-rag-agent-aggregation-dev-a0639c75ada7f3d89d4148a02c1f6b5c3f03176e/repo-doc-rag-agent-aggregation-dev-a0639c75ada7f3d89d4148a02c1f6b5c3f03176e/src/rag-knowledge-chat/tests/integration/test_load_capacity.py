from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from app.chat.models import RetrievalResult, RetrievalStatus, RouteDecision
from app.integrations.ark import AnswerStreamEvent, AnswerTextDelta
from app.main import create_app
from app.platform.settings import Settings
from scripts.load_chat import LoadConfig, run_load, summarize
from tests.conftest import FakeRetrieval


class TrackingArk:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.active = 0
        self.max_active = 0

    async def _wait(self) -> None:
        async with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        async with self._lock:
            self.active -= 1

    async def decide(self, _: list[dict[str, str]]) -> RouteDecision:
        await self._wait()
        return RouteDecision(
            needs_retrieval=False,
            query="",
            reason_code="general_knowledge",
        )

    async def stream_answer(
        self,
        _: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[AnswerStreamEvent]:
        del temperature, max_tokens
        await self._wait()
        yield AnswerTextDelta(delta="ok")


@pytest.mark.asyncio
async def test_chat_api_accepts_thirty_concurrent_streams() -> None:
    ark = TrackingArk()
    app = create_app(
        settings=Settings(
            _env_file=None,
            APP_ENV="test",
            AUTH_ENABLED=True,
            KNOWLEDGE_CHAT_INBOUND_API_KEYS="load-client:load-key",
            REQUEST_RPM_LIMIT=600,
            ARK_RPM_LIMIT=1800,
            ARK_MAX_CONCURRENCY=30,
        ),
        ark=ark,
        retrieval=FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT)),
    )
    config = LoadConfig(
        base_url="http://test",
        api_key="load-key",
        request_count=30,
        concurrency=30,
        timeout_seconds=5.0,
        payload={
            "model": "rag-knowledge-chat",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "max_tokens": 32,
            "rag": {
                "user_id": "load-user",
                "kb_id": "load-kb",
                "doc_ids": [],
                "temp_doc_ids": [],
                "top_k": 5,
                "include_debug": False,
            },
        },
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run = await run_load(config, client=client)

    summary = summarize(run)
    assert summary["successful"] == 30
    assert summary["app_rate_limited"] == 0
    assert summary["upstream_errors"] == 0
    assert run.max_in_flight == 30
    assert ark.max_active == 30
