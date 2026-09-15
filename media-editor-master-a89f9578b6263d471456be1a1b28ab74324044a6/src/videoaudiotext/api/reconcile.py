"""Startup reconciliation: orphan scratch cleanup and stale jobs."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from videoaudiotext.api.job_errors import job_error_interrupted
from videoaudiotext.api.stage_jobs import STAGE_JOB_PREFIXES
from videoaudiotext.api.job_context import is_managed_job_directory, jobs_root, load_job_store
from videoaudiotext.api.workspace.store import WorkspaceStore

ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
TERMINAL_JOB_STATUSES = frozenset({"done", "failed", "cancelled", "interrupted"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cleanup_compose_artifacts(
    store: WorkspaceStore,
    scratch: Path | None = None,
    *,
    output_mtime_before: float | None = None,
) -> list[str]:
    """Remove compose scratch and output touched by a failed/cancelled job.

    Preserves committed audio/subtitles and prior successful deliverables.
    """
    cleaned: list[str] = []
    if scratch and scratch.is_dir():
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            cleaned.append(scratch.relative_to(store.root).as_posix())
        except ValueError:
            cleaned.append(str(scratch))
    incomplete = store.output_mp4
    if incomplete.is_file():
        should_remove = output_mtime_before is None or incomplete.stat().st_mtime > (
            output_mtime_before + 1e-6
        )
        if should_remove:
            incomplete.unlink(missing_ok=True)
            cleaned.append("deliverables/output.mp4")
    return cleaned


def _clear_audio_process_ephemeral(store: WorkspaceStore) -> tuple[bool, int]:
    scratch = store.root / "intermediate" / "audio_process" / "_scratch"
    cleared = False
    if scratch.is_dir():
        shutil.rmtree(scratch, ignore_errors=True)
        cleared = True
    raw_removed = 0
    uploads = store.root / "uploads"
    if uploads.is_dir():
        for p in uploads.glob("audio_raw_*"):
            p.unlink(missing_ok=True)
            raw_removed += 1
    return cleared, raw_removed


def _reconcile_jobs_with_prefix(
    store: WorkspaceStore,
    prefix: str,
    *,
    startup: bool,
    scratch_resolver=None,
) -> dict[str, int]:
    stats = {"jobs_interrupted": 0, "scratch_cleared": 0}
    jobs_dir = store.root / "jobs"
    if not jobs_dir.is_dir():
        return stats
    for job_path in jobs_dir.glob(f"{prefix}_*.json"):
        job = store.read_json(job_path)
        if not job:
            continue
        status = str(job.get("status") or "")
        job_id = str(job.get("job_id") or "")

        if startup and status in ACTIVE_JOB_STATUSES:
            job["status"] = "interrupted"
            job["error"] = job_error_interrupted()
            job["updated_at"] = _now()
            store.write_json(job_path, job)
            stats["jobs_interrupted"] += 1
            status = "interrupted"

        if status in TERMINAL_JOB_STATUSES and job_id and scratch_resolver:
            scratch_dir = scratch_resolver(store, job_id)
            if scratch_dir and scratch_dir.is_dir():
                shutil.rmtree(scratch_dir, ignore_errors=True)
                stats["scratch_cleared"] += 1
    return stats


def _reconcile_compose_jobs(store: WorkspaceStore, *, startup: bool) -> dict[str, int]:
    stats = _reconcile_jobs_with_prefix(
        store,
        "compose",
        startup=startup,
        scratch_resolver=lambda s, job_id: s.root / "intermediate" / "compose" / job_id,
    )
    return {
        "jobs_interrupted": stats["jobs_interrupted"],
        "compose_scratch_cleared": stats["scratch_cleared"],
    }


def _reconcile_stage_jobs(store: WorkspaceStore, *, startup: bool) -> dict[str, int]:
    interrupted = 0
    for prefix in STAGE_JOB_PREFIXES:
        stats = _reconcile_jobs_with_prefix(store, prefix, startup=startup)
        interrupted += stats["jobs_interrupted"]
    return {"stage_jobs_interrupted": interrupted}


def reconcile_workspace(store: WorkspaceStore, *, startup: bool = False) -> dict[str, Any]:
    from videoaudiotext.api.job_ephemeral import purge_terminal_job_dir

    audio_scratch_cleared, audio_raw_removed = _clear_audio_process_ephemeral(store)
    job_stats = _reconcile_compose_jobs(store, startup=startup)
    stage_stats = _reconcile_stage_jobs(store, startup=startup)
    incomplete_removed = False
    incomplete = store.output_mp4
    if incomplete.is_file() and incomplete.stat().st_size < 1024:
        incomplete.unlink(missing_ok=True)
        incomplete_removed = True
    terminal_dir_purged = purge_terminal_job_dir(store)
    return {
        "workspace_id": store.workspace_id,
        "audio_scratch_cleared": audio_scratch_cleared,
        "audio_raw_removed": audio_raw_removed,
        "incomplete_output_removed": incomplete_removed,
        "terminal_dir_purged": terminal_dir_purged,
        **job_stats,
        **stage_stats,
    }


def reconcile_all_workspaces(*, startup: bool = True) -> dict[str, Any]:
    root = jobs_root()
    if not root.is_dir():
        return {"workspaces": 0, "results": []}
    results: list[dict[str, Any]] = []
    for ws_dir in sorted(root.iterdir()):
        if not ws_dir.is_dir() or not is_managed_job_directory(ws_dir.name):
            continue
        store = load_job_store(ws_dir.name)
        if not store:
            continue
        results.append(reconcile_workspace(store, startup=startup))
    return {"workspaces": len(results), "results": results}
