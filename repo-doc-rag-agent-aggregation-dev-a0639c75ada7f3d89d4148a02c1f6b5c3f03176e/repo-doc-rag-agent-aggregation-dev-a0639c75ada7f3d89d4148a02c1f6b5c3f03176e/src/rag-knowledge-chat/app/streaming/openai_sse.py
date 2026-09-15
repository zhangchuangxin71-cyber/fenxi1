from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.chat.events import (
    CompleteEvent,
    ErrorEvent,
    ReasoningDeltaEvent,
    StatusEvent,
    TextDeltaEvent,
)


@dataclass(frozen=True, slots=True)
class StreamContext:
    completion_id: str
    created: int
    model: str
    request_id: str | None = None


def _base(context: StreamContext) -> dict[str, Any]:
    return {
        "id": context.completion_id,
        "object": "chat.completion.chunk",
        "created": context.created,
        "model": context.model,
    }


def encode_event(
    event: StatusEvent | ReasoningDeltaEvent | TextDeltaEvent | CompleteEvent | ErrorEvent,
    context: StreamContext,
) -> str:
    payload = _base(context)
    if isinstance(event, StatusEvent):
        payload["choices"] = [{"index": 0, "delta": {}, "finish_reason": None}]
        payload["rag"] = {
            "event": {
                "type": "status",
                "stage": event.stage,
                "message": event.message,
            }
        }
    elif isinstance(event, TextDeltaEvent):
        payload["choices"] = [{"index": 0, "delta": {"content": event.delta}, "finish_reason": None}]
    elif isinstance(event, ReasoningDeltaEvent):
        payload["choices"] = [
            {"index": 0, "delta": {"reasoning_content": event.delta}, "finish_reason": None}
        ]
    elif isinstance(event, CompleteEvent):
        payload["choices"] = [{"index": 0, "delta": {}, "finish_reason": event.finish_reason}]
        rag: dict[str, Any] = {
            "answer_basis": event.answer_basis.value,
            "retrieval": event.retrieval,
            "references": event.references,
            "chunks": event.chunks,
        }
        if event.debug is not None:
            rag["debug"] = event.debug
        payload["rag"] = rag
    else:
        payload["choices"] = [{"index": 0, "delta": {}, "finish_reason": None}]
        payload["rag"] = {
            "error": {
                "code": event.code,
                "message": event.message,
                "retryable": event.retryable,
                "request_id": context.request_id or context.completion_id,
            }
        }
        if event.debug is not None:
            payload["rag"]["debug"] = event.debug
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def encode_done() -> str:
    return "data: [DONE]\n\n"
