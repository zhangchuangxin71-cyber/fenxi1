from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from langgraph.config import get_stream_writer

from app.core.ids import prefixed_id


def emit(kind: str, data: dict[str, Any]) -> None:
    get_stream_writer()({"kind": kind, "data": data})


def activity(
    *,
    activity_id: str,
    kind: Literal["node", "tool", "skill"],
    name: str,
    label: str,
    status: Literal["running", "completed", "degraded", "cancelled", "failed"],
    node: str,
    input_summary: dict[str, Any] | None = None,
    output_summary: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    emit(
        "activity",
        {
            "id": activity_id,
            "kind": kind,
            "name": name,
            "label": label,
            "status": status,
            "node": node,
            "input_summary": input_summary,
            "output_summary": output_summary,
            "error": error,
        },
    )


def public_text(text: str) -> None:
    if text:
        emit("public_text", {"text": text})


def reasoning_delta(delta: str) -> None:
    if delta:
        emit("reasoning_delta", {"delta": delta})


def artifact(
    *,
    artifact_id: str,
    revision: int,
    stage: str,
    status: str,
    content: Any,
    summary: Any = None,
) -> None:
    emit(
        "artifact",
        {
            "id": artifact_id,
            "revision": revision,
            "stage": stage,
            "status": status,
            "content": content,
            "summary": summary,
        },
    )


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def new_activity_id(name: str) -> str:
    return prefixed_id(f"activity_{name}")
