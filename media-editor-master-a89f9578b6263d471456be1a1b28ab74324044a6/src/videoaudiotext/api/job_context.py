"""Ephemeral job-scoped storage for stateless Flow B API."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from videoaudiotext.api.errors import ApiError, not_found
from videoaudiotext.api.job_common import load_job_by_prefix, new_job_id
from videoaudiotext.api.workspace.store import WorkspaceStore
from videoaudiotext.config import PROJECT_ROOT

INDEX_DIRNAME = "_index"
STAGE_PREFIX = {
    "split": "split",
    "audio_process": "audio_process",
    "media_bind": "media_bind",
    "visual_preview": "visual_preview",
    "compose": "compose",
}


def jobs_root() -> Path:
    raw = os.environ.get("JOBS_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (PROJECT_ROOT / "jobs").resolve()


def _index_dir() -> Path:
    path = jobs_root() / INDEX_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _index_path(job_id: str) -> Path:
    return _index_dir() / f"{job_id}.json"


def register_job(job_id: str, kind: str) -> None:
    prefix = STAGE_PREFIX[kind]
    payload = {
        "job_id": job_id,
        "kind": kind,
        "prefix": prefix,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _index_path(job_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_job_index(job_id: str) -> dict[str, Any] | None:
    path = _index_path(job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def is_managed_job_directory(name: str) -> bool:
    from videoaudiotext.api.task_ids import is_managed_task_directory

    return is_managed_task_directory(name)


def create_job_store(*, kind: str) -> WorkspaceStore:
    job_id = new_job_id()
    root = jobs_root() / job_id
    store = WorkspaceStore(
        workspace_id=job_id,
        root=root,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    store.ensure_tree()
    store.save_meta()
    register_job(job_id, kind)
    return store


def load_job_store(job_id: str) -> WorkspaceStore | None:
    root = jobs_root() / job_id
    if not root.is_dir():
        return None
    store = WorkspaceStore(
        workspace_id=job_id,
        root=root,
    )
    store.refresh_flags()
    meta_path = root / "workspace_meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            store.label = meta.get("label")
            store.created_at = meta.get("created_at") or ""
        except (json.JSONDecodeError, OSError):
            pass
    return store


def require_job_store(job_id: str) -> WorkspaceStore:
    store = load_job_store(job_id)
    if not store:
        raise ApiError(404, 40401, "job not found")
    return store


def load_job_record(job_id: str) -> dict[str, Any] | None:
    index = read_job_index(job_id)
    store = load_job_store(job_id)
    if store and index:
        prefix = str(index.get("prefix") or "")
        if prefix == "compose":
            from videoaudiotext.api.jobs import load_job

            job = load_job(store, job_id)
        else:
            job = load_job_by_prefix(store, prefix, job_id)
        if job:
            job.setdefault("kind", index.get("kind"))
            return job
    archived = None
    from videoaudiotext.api.job_archive import load_archived_job

    archived = load_archived_job(job_id)
    if archived:
        archived.setdefault("kind", (index or {}).get("kind"))
        return archived
    return None
