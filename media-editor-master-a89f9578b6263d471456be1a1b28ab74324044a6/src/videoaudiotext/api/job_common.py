"""Shared helpers for workspace stage jobs (split, audio, visual, compose)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from videoaudiotext.api.workspace.store import WorkspaceStore

ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_job_id() -> str:
    from videoaudiotext.api.task_ids import new_task_id

    return new_task_id()


def job_file_path(store: WorkspaceStore, prefix: str, job_id: str):
    return store.root / "jobs" / f"{prefix}_{job_id}.json"


def load_job_by_prefix(store: WorkspaceStore, prefix: str, job_id: str) -> dict[str, Any] | None:
    return store.read_json(job_file_path(store, prefix, job_id))


def save_job_by_prefix(
    store: WorkspaceStore,
    prefix: str,
    job_id: str,
    data: dict[str, Any],
) -> None:
    store.write_json(job_file_path(store, prefix, job_id), data)


def save_job_by_prefix_if_live(
    store: WorkspaceStore,
    prefix: str,
    job_id: str,
    data: dict[str, Any],
) -> bool:
    """Persist job JSON when the workspace dir still exists; tolerate cancel races."""
    if not store.root.is_dir():
        return False
    try:
        save_job_by_prefix(store, prefix, job_id, data)
        return True
    except OSError:
        return False


def find_active_job(store: WorkspaceStore, prefix: str) -> dict[str, Any] | None:
    jobs_dir = store.root / "jobs"
    if not jobs_dir.is_dir():
        return None
    for path in sorted(
        jobs_dir.glob(f"{prefix}_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        data = store.read_json(path)
        if data and data.get("status") in ACTIVE_JOB_STATUSES:
            return data
    return None


def job_is_cancelled(store: WorkspaceStore, prefix: str, job_id: str) -> bool:
    from videoaudiotext.api.job_cancel import is_job_cancelled

    return is_job_cancelled(job_id, store, prefix)


def make_cancel_check(store: WorkspaceStore, prefix: str, job_id: str):
    from videoaudiotext.api.job_errors import JobCancelled

    def _check() -> None:
        if job_is_cancelled(store, prefix, job_id):
            raise JobCancelled()

    return _check

