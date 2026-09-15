from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.chat.models import RetrievalResult, RouteDecision
from app.integrations.ark import AnswerStreamEvent, AnswerTextDelta


@dataclass
class FakeArk:
    decision: RouteDecision
    deltas: list[AnswerStreamEvent] = field(
        default_factory=lambda: [AnswerTextDelta(delta="测试"), AnswerTextDelta(delta="回答")]
    )
    fail_decision: Exception | None = None
    fail_stream: Exception | None = None
    decision_calls: list[list[dict[str, str]]] = field(default_factory=list)
    answer_calls: list[dict[str, Any]] = field(default_factory=list)

    async def decide(self, messages: list[dict[str, str]]) -> RouteDecision:
        self.decision_calls.append(messages)
        if self.fail_decision:
            raise self.fail_decision
        return self.decision

    async def stream_answer(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[AnswerStreamEvent]:
        self.answer_calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.fail_stream:
            raise self.fail_stream
        for delta in self.deltas:
            yield delta


@dataclass
class FakeRetrieval:
    result: RetrievalResult
    calls: list[dict[str, Any]] = field(default_factory=list)
    document_names_by_id: dict[str, str] = field(default_factory=dict)
    meta_calls: list[dict[str, Any]] = field(default_factory=list)

    async def retrieve(self, **kwargs: Any) -> RetrievalResult:
        self.calls.append(kwargs)
        return self.result

    async def get_document_names(self, **kwargs: Any) -> list[str]:
        self.meta_calls.append(kwargs)
        return [
            self.document_names_by_id.get(doc_id, f"{doc_id}.pdf") for doc_id in kwargs["requested_doc_ids"]
        ]


class AllowLimiter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def acquire(self, key: str, *, cost: int = 1) -> None:
        self.calls.extend([key] * cost)


@pytest.fixture
def base_payload() -> dict[str, Any]:
    return {
        "model": "rag-knowledge-chat",
        "messages": [{"role": "user", "content": "文档里的审批流程是什么？"}],
        "stream": True,
        "rag": {
            "user_id": "user-1",
            "kb_id": "kb-1",
            "doc_ids": ["doc-1"],
            "temp_doc_ids": [],
            "top_k": 5,
            "include_debug": False,
        },
    }
