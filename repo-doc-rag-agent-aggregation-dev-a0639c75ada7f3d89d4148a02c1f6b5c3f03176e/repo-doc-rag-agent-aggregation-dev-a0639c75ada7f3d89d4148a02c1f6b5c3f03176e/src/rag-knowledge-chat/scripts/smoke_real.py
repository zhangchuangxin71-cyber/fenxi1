from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from app.api.contracts import ChatCompletionRequest
from app.chat.events import CompleteEvent, ErrorEvent, StatusEvent, TextDeltaEvent
from app.chat.models import RetrievalResult, RetrievalStatus
from app.chat.orchestrator import ChatOrchestrator
from app.integrations.ark import ArkClient
from app.platform.settings import Settings
from app.platform.trace import TraceCollector


class AllowLimiter:
    async def acquire(self, key: str, *, cost: int = 1) -> None:
        return None


@dataclass
class StaticRetrieval:
    result: RetrievalResult
    calls: int = 0

    async def retrieve(self, **_: Any) -> RetrievalResult:
        self.calls += 1
        return self.result


def build_request(query: str) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {
            "model": "rag-knowledge-chat",
            "messages": [{"role": "user", "content": query}],
            "stream": True,
            "temperature": 0.1,
            "max_tokens": 500,
            "rag": {
                "user_id": "real-smoke-user",
                "kb_id": "real-smoke-kb",
                "doc_ids": ["synthetic-doc"],
                "include_debug": True,
            },
        }
    )


async def run_case(
    name: str,
    *,
    ark: ArkClient,
    query: str,
    retrieval: StaticRetrieval,
    expect_retrieval: bool,
) -> dict[str, Any]:
    orchestrator = ChatOrchestrator(
        ark=ark,
        retrieval=retrieval,
        ark_limiter=AllowLimiter(),
        debug_enabled=True,
    )
    started = time.monotonic()
    events = [
        event
        async for event in orchestrator.run(
            request=build_request(query),
            request_id=f"real-{name}",
            trace=TraceCollector(enabled=True),
        )
    ]
    latency_ms = round((time.monotonic() - started) * 1000)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    if errors:
        error = errors[-1]
        return {
            "case": name,
            "passed": False,
            "error_code": error.code,
            "latency_ms": latency_ms,
        }
    complete = next(event for event in events if isinstance(event, CompleteEvent))
    answer = "".join(event.delta for event in events if isinstance(event, TextDeltaEvent))
    status_stages = [event.stage for event in events if isinstance(event, StatusEvent)]
    route = complete.debug["route"] if complete.debug else {}
    actual_retrieval = retrieval.calls > 0
    passed = actual_retrieval is expect_retrieval and len(answer) > 0
    return {
        "case": name,
        "passed": passed,
        "expected_retrieval": expect_retrieval,
        "actual_retrieval": actual_retrieval,
        "route": route,
        "answer_basis": complete.answer_basis.value,
        "reference_count": len(complete.references),
        "answer_chars": len(answer),
        "answer_preview": answer[:160],
        "status_stages": status_stages,
        "latency_ms": latency_ms,
    }


async def main() -> None:
    settings = Settings()
    if not settings.ark_api_key:
        raise SystemExit("ARK_API_KEY is not configured")
    ark = ArkClient(
        api_key=settings.ark_api_key,
        base_url=settings.ark_base_url,
        model=settings.model_smart,
        thinking_type=settings.doubao_thinking_type,
    )
    try:
        identity = await run_case(
            "identity",
            ark=ark,
            query="你是谁？由谁开发？",
            retrieval=StaticRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT)),
            expect_retrieval=False,
        )
        grounded = await run_case(
            "grounded",
            ark=ark,
            query="请根据我提供的制度文档说明审批流程。",
            retrieval=StaticRetrieval(
                RetrievalResult(
                    status=RetrievalStatus.SUCCESS,
                    request_id="synthetic-retrieval",
                    chunks=[
                        {
                            "chunk_id": "synthetic-c1",
                            "document_id": "synthetic-doc",
                            "document_name": "审批制度.pdf",
                            "path": "第 3 节",
                            "content": "审批流程需要业务负责人初审，再由部门负责人终审。",
                            "score": 1.0,
                            "source_type": "node",
                        }
                    ],
                )
            ),
            expect_retrieval=True,
        )
    finally:
        await ark.close()
    results = [identity, grounded]
    print(
        json.dumps(
            {
                "suite": "real_ark",
                "model_configured": bool(settings.model_smart),
                "all_passed": all(item["passed"] for item in results),
                "results": results,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
