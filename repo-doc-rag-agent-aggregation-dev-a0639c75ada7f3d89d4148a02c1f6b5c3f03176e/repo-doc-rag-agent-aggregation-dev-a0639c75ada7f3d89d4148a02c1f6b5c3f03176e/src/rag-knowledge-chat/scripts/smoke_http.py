from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx

from app.platform.settings import Settings
from scripts.smoke_live_pipeline import load_first_label


async def main() -> None:
    settings = Settings()
    inbound_api_key = settings.default_inbound_api_key
    if settings.auth_enabled and not inbound_api_key:
        raise SystemExit("knowledge-chat inbound API key is not configured")
    label = load_first_label()
    payload = {
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
    base_url = os.getenv("SMOKE_CHAT_URL", "http://127.0.0.1:8130").rstrip("/")
    started = time.monotonic()
    events: list[dict[str, Any]] = []
    done = False
    request_id = None
    async with httpx.AsyncClient(timeout=100) as client:
        async with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {inbound_api_key}"} if inbound_api_key else {},
            json=payload,
        ) as response:
            request_id = response.headers.get("X-Request-Id")
            if response.status_code != 200:
                body = await response.aread()
                print(
                    json.dumps(
                        {
                            "suite": "http_sse",
                            "passed": False,
                            "status_code": response.status_code,
                            "body_preview": body.decode("utf-8", errors="replace")[:200],
                        },
                        ensure_ascii=False,
                    )
                )
                raise SystemExit(1)
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line.removeprefix("data: ")
                if data == "[DONE]":
                    done = True
                    continue
                events.append(json.loads(data))
    stages = [event["rag"]["event"]["stage"] for event in events if event.get("rag", {}).get("event")]
    completions = [event["rag"] for event in events if event.get("rag", {}).get("answer_basis")]
    completion_ids = {event["id"] for event in events}
    content = "".join(event.get("choices", [{}])[0].get("delta", {}).get("content", "") for event in events)
    final = completions[-1] if completions else {}
    passed = (
        done
        and len(completion_ids) == 1
        and stages == ["route", "retrieval", "generation"]
        and final.get("answer_basis") == "knowledge_base"
        and bool(final.get("references"))
        and bool(content)
    )
    result = {
        "suite": "http_sse",
        "passed": passed,
        "request_id_present": bool(request_id),
        "completion_id_count": len(completion_ids),
        "status_stages": stages,
        "answer_basis": final.get("answer_basis"),
        "reference_count": len(final.get("references", [])),
        "chunk_count": len(final.get("chunks", [])),
        "debug_present": "debug" in final,
        "done_received": done,
        "answer_chars": len(content),
        "answer_preview": content[:180],
        "latency_ms": round((time.monotonic() - started) * 1000),
    }
    print(json.dumps(result, ensure_ascii=False))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
