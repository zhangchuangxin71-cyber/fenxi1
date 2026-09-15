from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from openai import AsyncOpenAI

from app.platform.settings import Settings


async def main() -> None:
    settings = Settings()
    inbound_api_key = settings.default_inbound_api_key
    if settings.auth_enabled and not inbound_api_key:
        raise SystemExit("knowledge-chat inbound API key is not configured")
    service_url = os.getenv("SMOKE_CHAT_URL", "http://127.0.0.1:8130").rstrip("/")
    client = AsyncOpenAI(
        api_key=inbound_api_key or "auth-disabled",
        base_url=f"{service_url}/v1",
        timeout=100,
        max_retries=0,
    )
    content: list[str] = []
    rag_payloads: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        stream = await client.chat.completions.create(
            model="rag-knowledge-chat",
            messages=[{"role": "user", "content": "你是谁？由谁开发？"}],
            stream=True,
            max_tokens=300,
            extra_body={
                "rag": {
                    "user_id": "sdk-smoke-user",
                    "kb_id": "sdk-smoke-kb",
                    "doc_ids": [],
                    "include_debug": True,
                }
            },
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                content.append(chunk.choices[0].delta.content)
            extra = chunk.model_extra or {}
            if isinstance(extra.get("rag"), dict):
                rag_payloads.append(extra["rag"])
    finally:
        await client.close()
    final = next(
        (item for item in reversed(rag_payloads) if item.get("answer_basis")),
        {},
    )
    statuses = [item["event"]["stage"] for item in rag_payloads if isinstance(item.get("event"), dict)]
    answer = "".join(content)
    passed = (
        bool(answer)
        and statuses == ["route", "generation"]
        and final.get("answer_basis") == "general_no_retrieval"
    )
    print(
        json.dumps(
            {
                "suite": "openai_sdk",
                "passed": passed,
                "status_stages": statuses,
                "answer_basis": final.get("answer_basis"),
                "rag_extension_visible": bool(rag_payloads),
                "answer_chars": len(answer),
                "answer_preview": answer[:160],
                "latency_ms": round((time.monotonic() - started) * 1000),
            },
            ensure_ascii=False,
        )
    )
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
