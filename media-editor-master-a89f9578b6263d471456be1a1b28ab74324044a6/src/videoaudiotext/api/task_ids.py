"""Public task_id naming for Flow B API (maps internal job records)."""

from __future__ import annotations

import uuid
from typing import Any

_API_KEY_RENAMES: dict[str, str] = {
    "job_id": "task_id",
    "upstream_job_id": "upstream_task_id",
    "upstream_job_ids": "upstream_task_ids",
    "split_job_id": "split_task_id",
    "audio_job_id": "audio_task_id",
    "bind_job_id": "bind_task_id",
    "pipeline_job_id": "pipeline_task_id",
    "active_compose_job_id": "active_compose_task_id",
}


def new_task_id() -> str:
    """Return a standard UUID string (RFC 4122, lowercase with hyphens)."""
    return str(uuid.uuid4())


def is_uuid_task_id(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    try:
        parsed = uuid.UUID(text)
    except ValueError:
        return False
    return str(parsed) == text


def normalize_task_id(raw: str | None) -> str:
    return (raw or "").strip()


def require_uuid_task_id(raw: str) -> str:
    from videoaudiotext.api.errors import not_found

    task_id = normalize_task_id(raw)
    if not is_uuid_task_id(task_id):
        raise not_found("task not found")
    return task_id


def is_managed_task_directory(name: str) -> bool:
    if not name or name.startswith("_"):
        return False
    return is_uuid_task_id(name)


def to_public_api(data: Any) -> Any:
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            public_key = _API_KEY_RENAMES.get(key, key)
            out[public_key] = to_public_api(value)
        return out
    if isinstance(data, list):
        return [to_public_api(item) for item in data]
    return data
