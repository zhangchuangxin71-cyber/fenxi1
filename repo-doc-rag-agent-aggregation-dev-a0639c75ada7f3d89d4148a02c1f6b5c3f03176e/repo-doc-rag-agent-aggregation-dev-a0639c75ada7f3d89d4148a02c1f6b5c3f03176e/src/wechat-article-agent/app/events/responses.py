from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sse(event_type: str, payload: dict[str, Any]) -> str:
    body = {"type": event_type, **payload}
    return f"event: {event_type}\ndata: {json.dumps(body, ensure_ascii=False, separators=(',', ':'))}\n\n"


def response_snapshot(
    *,
    response_id: str,
    model: str,
    status: str,
    run_id: str,
    artifact_id: str,
    revision: int,
    workflow_status: str,
    pending_interrupt_id: str | None = None,
    error: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = {
        "id": response_id,
        "object": "response",
        "created_at": int(datetime.now(UTC).timestamp()),
        "model": model,
        "status": status,
        "output": [],
        "error": error,
        "metadata": {
            "run_id": run_id,
            "artifact_id": artifact_id,
            "revision": revision,
            "workflow_status": workflow_status,
            "pending_interrupt_id": pending_interrupt_id,
        },
    }
    if usage is not None:
        snapshot["usage"] = usage
    return snapshot


class ResponseEventBuilder:
    def __init__(self, *, response_id: str, run_id: str) -> None:
        self.response_id = response_id
        self.run_id = run_id
        self.message_item_id = f"msg_{response_id.removeprefix('resp_')}"
        self.reasoning_item_id = f"rs_{response_id.removeprefix('resp_')}"
        self.text_started = False
        self.reasoning_started = False
        self.text_output_index: int | None = None
        self.reasoning_output_index: int | None = None
        self.text = ""
        self.reasoning = ""

    def common(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "response_id": self.response_id,
            "timestamp": now_iso(),
        }

    def text_delta(self, delta: str) -> list[str]:
        events: list[str] = []
        if not self.text_started:
            self.text_started = True
            self.text_output_index = 1 if self.reasoning_started else 0
            events.extend(
                [
                    sse(
                        "response.output_item.added",
                        {
                            **self.common(),
                            "output_index": self.text_output_index,
                            "item": {
                                "id": self.message_item_id,
                                "type": "message",
                                "role": "assistant",
                                "status": "in_progress",
                                "content": [],
                            },
                        },
                    ),
                    sse(
                        "response.content_part.added",
                        {
                            **self.common(),
                            "item_id": self.message_item_id,
                            "output_index": self.text_output_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": ""},
                        },
                    ),
                ]
            )
        self.text += delta
        events.append(
            sse(
                "response.output_text.delta",
                {
                    **self.common(),
                    "item_id": self.message_item_id,
                    "output_index": self.text_output_index,
                    "content_index": 0,
                    "delta": delta,
                },
            )
        )
        return events

    def reasoning_delta(self, delta: str) -> list[str]:
        events: list[str] = []
        if not self.reasoning_started:
            self.reasoning_started = True
            self.reasoning_output_index = 1 if self.text_started else 0
            events.extend(
                [
                    sse(
                        "response.output_item.added",
                        {
                            **self.common(),
                            "output_index": self.reasoning_output_index,
                            "item": {
                                "id": self.reasoning_item_id,
                                "type": "reasoning",
                                "status": "in_progress",
                            },
                        },
                    ),
                    sse(
                        "response.reasoning_summary_part.added",
                        {
                            **self.common(),
                            "item_id": self.reasoning_item_id,
                            "output_index": self.reasoning_output_index,
                            "summary_index": 0,
                            "part": {"type": "summary_text"},
                        },
                    ),
                ]
            )
        self.reasoning += delta
        events.append(
            sse(
                "response.reasoning_summary_text.delta",
                {
                    **self.common(),
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_output_index,
                    "summary_index": 0,
                    "delta": delta,
                },
            )
        )
        return events

    def finish_content(self) -> list[str]:
        events: list[str] = []
        if self.reasoning_started:
            events.extend(
                [
                    sse(
                        "response.reasoning_summary_text.done",
                        {
                            **self.common(),
                            "item_id": self.reasoning_item_id,
                            "output_index": self.reasoning_output_index,
                            "summary_index": 0,
                            "text": self.reasoning,
                        },
                    ),
                    sse(
                        "response.reasoning_summary_part.done",
                        {
                            **self.common(),
                            "item_id": self.reasoning_item_id,
                            "output_index": self.reasoning_output_index,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": self.reasoning},
                        },
                    ),
                    sse(
                        "response.output_item.done",
                        {
                            **self.common(),
                            "output_index": self.reasoning_output_index,
                            "item": {
                                "id": self.reasoning_item_id,
                                "type": "reasoning",
                                "status": "completed",
                                "summary": [{"type": "summary_text", "text": self.reasoning}],
                            },
                        },
                    ),
                ]
            )
        if self.text_started:
            events.extend(
                [
                    sse(
                        "response.output_text.done",
                        {
                            **self.common(),
                            "item_id": self.message_item_id,
                            "output_index": self.text_output_index,
                            "content_index": 0,
                            "text": self.text,
                        },
                    ),
                    sse(
                        "response.content_part.done",
                        {
                            **self.common(),
                            "item_id": self.message_item_id,
                            "output_index": self.text_output_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": self.text},
                        },
                    ),
                    sse(
                        "response.output_item.done",
                        {
                            **self.common(),
                            "output_index": self.text_output_index,
                            "item": {
                                "id": self.message_item_id,
                                "type": "message",
                                "role": "assistant",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": self.text}],
                            },
                        },
                    ),
                ]
            )
        return events
