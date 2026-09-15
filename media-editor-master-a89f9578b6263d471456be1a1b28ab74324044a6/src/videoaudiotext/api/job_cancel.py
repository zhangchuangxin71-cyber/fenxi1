"""Shared cancel-task helpers: idempotency, terminal-state conflicts, race safety."""

from __future__ import annotations

from typing import Any

from videoaudiotext.api.errors import ApiError, conflict, not_found
from videoaudiotext.api.job_common import ACTIVE_JOB_STATUSES
from videoaudiotext.api.job_context import load_job_record, load_job_store, read_job_index
from videoaudiotext.api.workspace.store import WorkspaceStore

CANCELLED_STATUS = "cancelled"


def job_not_cancellable(status: str) -> ApiError:
    """409 Conflict when the task exists but is not in a cancellable state."""
    return conflict(40904, "job_not_cancellable", status=status)


def cancel_result(job_id: str, *, cleaned_paths: list[str] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"job_id": job_id, "status": CANCELLED_STATUS}
    if cleaned_paths:
        payload["cleaned_paths"] = cleaned_paths
    return payload


def is_cancelled(job: dict[str, Any] | None) -> bool:
    return bool(job and str(job.get("status") or "") == CANCELLED_STATUS)


def finish_cancel_if_already_terminal(job_id: str) -> dict[str, Any] | None:
    """Return idempotent cancel payload when archive shows the job was cancelled."""
    job = load_job_record(job_id)
    if is_cancelled(job):
        return cancel_result(job_id)
    return None


def resolve_task_for_cancel(
    task_id: str,
) -> tuple[dict[str, Any], dict[str, Any], WorkspaceStore | None]:
    index = read_job_index(task_id)
    if not index:
        raise not_found("task not found")
    job = load_job_record(task_id)
    if not job:
        raise not_found("task not found")
    store = load_job_store(task_id)
    return index, job, store


def is_job_cancelled(
    job_id: str,
    store: WorkspaceStore | None = None,
    prefix: str | None = None,
) -> bool:
    """True when the job is cancelled in live storage or in the archive."""
    if store is not None:
        if prefix:
            from videoaudiotext.api.job_common import load_job_by_prefix

            live = load_job_by_prefix(store, prefix, job_id)
        else:
            from videoaudiotext.api.jobs import load_job

            live = load_job(store, job_id)
        if is_cancelled(live):
            return True
    return is_cancelled(load_job_record(job_id))


def assert_cancellable_or_cancelled(job: dict[str, Any]) -> str | None:
    """Return ``cancelled`` when already cancelled; raise otherwise if not active."""
    status = str(job.get("status") or "")
    if status == CANCELLED_STATUS:
        return CANCELLED_STATUS
    if status not in ACTIVE_JOB_STATUSES:
        raise job_not_cancellable(status)
    return None
