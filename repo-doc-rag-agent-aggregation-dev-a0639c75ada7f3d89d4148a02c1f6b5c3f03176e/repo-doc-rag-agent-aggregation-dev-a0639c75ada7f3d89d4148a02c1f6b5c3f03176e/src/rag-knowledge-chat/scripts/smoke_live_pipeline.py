from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from app.api.contracts import ChatCompletionRequest
from app.chat.events import CompleteEvent, ErrorEvent, TextDeltaEvent
from app.chat.orchestrator import ChatOrchestrator
from app.integrations.ark import ArkClient
from app.integrations.retrieval import RetrievalClient
from app.platform.settings import Settings
from app.platform.trace import TraceCollector


class AllowLimiter:
    async def acquire(self, key: str, *, cost: int = 1) -> None:
        return None


def load_first_label() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[2]
        / "rag-offline-eval"
        / "dataset"
        / "manifests"
        / "query_labels.jsonl"
    )
    first_line = next(line for line in path.read_text(encoding="utf-8").splitlines() if line)
    return json.loads(first_line)


async def main() -> None:
    settings = Settings()
    if not settings.ark_api_key:
        raise SystemExit("Ark API key is not configured")
    label = load_first_label()
    request = ChatCompletionRequest.model_validate(
        {
            "model": "rag-knowledge-chat",
            "messages": [{"role": "user", "content": label["query_text"]}],
            "stream": True,
            "temperature": 0.1,
            "max_tokens": 500,
            "rag": {
                "user_id": "eval-user",
                "kb_id": "eval-rag-kb",
                "doc_ids": [label["doc_id"]],
                "top_k": 5,
                "include_debug": True,
            },
        }
    )
    ark = ArkClient(
        api_key=settings.ark_api_key,
        base_url=settings.ark_base_url,
        model=settings.model_smart,
        thinking_type=settings.doubao_thinking_type,
    )
    retrieval = RetrievalClient(base_url=settings.retrieval_url)
    orchestrator = ChatOrchestrator(
        ark=ark,
        retrieval=retrieval,
        ark_limiter=AllowLimiter(),
        debug_enabled=True,
    )
    started = time.monotonic()
    try:
        events = [
            event
            async for event in orchestrator.run(
                request=request,
                request_id="live-smoke",
                trace=TraceCollector(enabled=True),
            )
        ]
    finally:
        await ark.close()
        await retrieval.close()
    elapsed_ms = round((time.monotonic() - started) * 1000)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    if errors:
        result = {
            "suite": "live_pipeline",
            "passed": False,
            "error_code": errors[-1].code,
            "latency_ms": elapsed_ms,
        }
    else:
        complete = next(event for event in events if isinstance(event, CompleteEvent))
        answer = "".join(event.delta for event in events if isinstance(event, TextDeltaEvent))
        result = {
            "suite": "live_pipeline",
            "passed": complete.answer_basis.value == "knowledge_base",
            "query_id": label["query_id"],
            "gold_doc_id": label["doc_id"],
            "answer_basis": complete.answer_basis.value,
            "retrieval_status": complete.retrieval["status"],
            "retrieval_request_id": complete.retrieval.get("request_id"),
            "returned_chunk_count": len(complete.chunks),
            "returned_doc_ids": sorted({chunk["document_id"] for chunk in complete.chunks}),
            "reference_count": len(complete.references),
            "answer_chars": len(answer),
            "answer_preview": answer[:180],
            "latency_ms": elapsed_ms,
        }
    print(json.dumps(result, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
