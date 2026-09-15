from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.api.contracts import ChatCompletionRequest
from app.chat.events import CompleteEvent, ErrorEvent, TextDeltaEvent
from app.chat.models import RetrievalResult, RetrievalStatus, RouteDecision
from app.chat.orchestrator import ChatOrchestrator
from app.integrations.ark import AnswerStreamEvent, AnswerTextDelta
from app.platform.trace import TraceCollector


class AllowLimiter:
    async def acquire(self, key: str, *, cost: int = 1) -> None:
        return None


@dataclass
class FakeArk:
    decision: RouteDecision
    answer: str
    call_count: int = 0

    async def decide(self, messages: list[dict[str, str]]) -> RouteDecision:
        self.call_count += 1
        return self.decision

    async def stream_answer(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[AnswerStreamEvent]:
        self.call_count += 1
        midpoint = max(1, len(self.answer) // 2)
        for delta in (self.answer[:midpoint], self.answer[midpoint:]):
            if delta:
                yield AnswerTextDelta(delta=delta)


@dataclass
class FakeRetrieval:
    result: RetrievalResult
    call_count: int = 0

    async def retrieve(self, **_: Any) -> RetrievalResult:
        self.call_count += 1
        return self.result


def request(*, debug: bool = True) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {
            "model": "rag-knowledge-chat",
            "messages": [{"role": "user", "content": "请回答当前问题"}],
            "stream": True,
            "rag": {
                "user_id": "smoke-user",
                "kb_id": "smoke-kb",
                "doc_ids": ["doc-1"],
                "include_debug": debug,
            },
        }
    )


async def run_case(
    name: str,
    *,
    decision: RouteDecision,
    retrieval_result: RetrievalResult,
    answer: str,
) -> dict[str, Any]:
    ark = FakeArk(decision=decision, answer=answer)
    retrieval = FakeRetrieval(result=retrieval_result)
    orchestrator = ChatOrchestrator(
        ark=ark,
        retrieval=retrieval,
        ark_limiter=AllowLimiter(),
    )
    events = [
        event
        async for event in orchestrator.run(
            request=request(),
            request_id=f"fake-{name}",
            trace=TraceCollector(enabled=True),
        )
    ]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert not errors
    complete = next(event for event in events if isinstance(event, CompleteEvent))
    text = "".join(event.delta for event in events if isinstance(event, TextDeltaEvent))
    assert ark.call_count == 2
    assert retrieval.call_count == int(decision.needs_retrieval)
    return {
        "case": name,
        "answer_basis": complete.answer_basis.value,
        "ark_calls": ark.call_count,
        "retrieval_calls": retrieval.call_count,
        "reference_count": len(complete.references),
        "answer_chars": len(text),
        "passed": True,
    }


async def main() -> None:
    chunk = {
        "chunk_id": "c1",
        "document_id": "doc-1",
        "document_name": "制度.pdf",
        "path": "1",
        "content": "审批需要两级确认。",
        "score": 0.9,
        "source_type": "node",
    }
    results = [
        await run_case(
            "direct",
            decision=RouteDecision(needs_retrieval=False, query="", reason_code="identity"),
            retrieval_result=RetrievalResult(status=RetrievalStatus.NO_RESULT),
            answer="我是知识库问答助手。",
        ),
        await run_case(
            "grounded",
            decision=RouteDecision(
                needs_retrieval=True,
                query="审批流程",
                reason_code="knowledge_base",
            ),
            retrieval_result=RetrievalResult(status=RetrievalStatus.SUCCESS, chunks=[chunk]),
            answer="审批需要两级确认[1]。",
        ),
        await run_case(
            "no_result",
            decision=RouteDecision(needs_retrieval=True, query="问题", reason_code="knowledge_base"),
            retrieval_result=RetrievalResult(status=RetrievalStatus.NO_RESULT),
            answer="这里给出通识建议。",
        ),
        await run_case(
            "retrieval_error",
            decision=RouteDecision(needs_retrieval=True, query="问题", reason_code="knowledge_base"),
            retrieval_result=RetrievalResult(
                status=RetrievalStatus.ERROR,
                error={"code": "retrieval_http_error", "status_code": 401},
            ),
            answer="这里给出通识建议。",
        ),
    ]
    print(json.dumps({"suite": "fake", "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
