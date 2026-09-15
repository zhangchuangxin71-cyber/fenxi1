import pytest

from app.api.contracts import ChatCompletionRequest
from app.chat.events import CompleteEvent
from app.chat.models import RetrievalResult, RetrievalStatus, RouteDecision
from app.chat.orchestrator import ChatOrchestrator
from app.integrations.ark import AnswerTextDelta
from app.platform.trace import TraceCollector
from tests.conftest import AllowLimiter, FakeArk, FakeRetrieval


@pytest.mark.asyncio
async def test_chat_debug_off_still_forwards_and_returns_retrieval_debug(base_payload) -> None:
    base_payload["rag"]["include_debug"] = True
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(needs_retrieval=True, query="问题", reason_code="knowledge_base"),
        deltas=[AnswerTextDelta(delta="回答")],
    )
    retrieval = FakeRetrieval(
        RetrievalResult(
            status=RetrievalStatus.SUCCESS,
            usage={"latency_ms": 12},
            debug={"trace": [{"node": "classify_query", "status": "ok"}]},
            chunks=[
                {
                    "chunk_id": "c1",
                    "document_id": "d1",
                    "document_name": "文档",
                    "path": "1",
                    "content": "内容",
                    "score": 1,
                    "source_type": "node",
                }
            ],
        )
    )
    orchestrator = ChatOrchestrator(
        ark=ark,
        retrieval=retrieval,
        ark_limiter=AllowLimiter(),
        debug_enabled=False,
    )

    events = [
        event
        async for event in orchestrator.run(
            request=request,
            request_id="req",
            trace=TraceCollector(enabled=False),
        )
    ]

    complete = next(event for event in events if isinstance(event, CompleteEvent))
    assert retrieval.calls[0]["include_debug"] is True
    assert complete.debug == {
        "retrieval": {
            "usage": {"latency_ms": 12},
            "debug": {"trace": [{"node": "classify_query", "status": "ok"}]},
            "error": None,
        }
    }
