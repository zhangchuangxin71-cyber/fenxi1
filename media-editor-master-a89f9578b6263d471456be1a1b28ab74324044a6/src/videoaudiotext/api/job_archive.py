"""Persistent job JSON archives (small metadata; large files live on OSS)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JOB_ARCHIVE_DIRNAME = "_job_archive"
# Backward-compatible alias used by compose tests and docs.
COMPOSE_ARCHIVE_DIRNAME = JOB_ARCHIVE_DIRNAME


def _jobs_root() -> Path:
    from videoaudiotext.api.job_context import jobs_root

    return jobs_root()


def _archive_dir() -> Path:
    path = _jobs_root() / JOB_ARCHIVE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def archive_path(job_id: str) -> Path:
    return _archive_dir() / f"{job_id}.json"


def load_archived_job(job_id: str) -> dict[str, Any] | None:
    path = archive_path(job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def archive_job(job: dict[str, Any]) -> None:
    payload = dict(job)
    payload["archived_at"] = datetime.now(timezone.utc).isoformat()
    path = archive_path(str(job["job_id"]))
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_archived_compose_job(job_id: str) -> dict[str, Any] | None:
    return load_archived_job(job_id)

