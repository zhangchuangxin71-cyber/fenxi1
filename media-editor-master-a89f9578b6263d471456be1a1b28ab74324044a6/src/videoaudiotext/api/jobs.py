"""Compose job runner with in-memory registry + workspace JSON persistence."""

from __future__ import annotations

import logging
import shutil
import threading

from videoaudiotext.concurrency.pools import submit_api_job
from videoaudiotext.concurrency.coordination import workspace_stage_claim_lock
from videoaudiotext.api.job_common import find_active_job
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from videoaudiotext.api.api_logging import log_error, log_info
from videoaudiotext.api.errors import ApiError, bad_request, conflict, not_found, process_failed
from videoaudiotext.api.job_cancel import (
    cancel_result,
    finish_cancel_if_already_terminal,
    job_not_cancellable,
)
from videoaudiotext.api.job_errors import (
    JobCancelled,
    job_error_cancelled,
    job_error_from_api,
    job_error_from_exception,
)
from videoaudiotext.api.job_ephemeral import finalize_terminal_job
from videoaudiotext.api.services.compose import run_flow_b_compose
from videoaudiotext.api.job_archive import archive_job, load_archived_job
from videoaudiotext.api.job_context import jobs_root, load_job_store
from videoaudiotext.api.workspace.store import WorkspaceStore
from videoaudiotext.storage.oss import (
    build_compose_object_keys,
    oss_configured,
    oss_public_url,
    upload_file_oss,
)

_lock = threading.Lock()
_active: dict[str, str] = {}  # workspace_id -> job_id

COMPOSE_LOG_DIRNAME = "_compose_logs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compose_log_dir() -> Path:
    path = jobs_root() / COMPOSE_LOG_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_path(store: WorkspaceStore, job_id: str) -> Path:
    return store.root / "jobs" / f"compose_{job_id}.json"


def load_archived_compose_job(job_id: str) -> dict[str, Any] | None:
    return load_archived_job(job_id)


def archive_compose_job(job: dict[str, Any]) -> None:
    archive_job(job)


def load_job(store: WorkspaceStore, job_id: str) -> dict[str, Any] | None:
    if store.root.is_dir():
        job = store.read_json(_job_path(store, job_id))
        if job:
            return job
    return load_archived_compose_job(job_id)


def save_job(store: WorkspaceStore, job_id: str, data: dict[str, Any]) -> None:
    if store.root.is_dir():
        store.write_json(_job_path(store, job_id), data)


def _job_is_cancelled(store: WorkspaceStore, job_id: str) -> bool:
    from videoaudiotext.api.job_cancel import is_job_cancelled

    return is_job_cancelled(job_id, store)


def _resolve_master_audio_path(store: WorkspaceStore, scratch: Path) -> Path | None:
    candidates = (
        scratch / "master_final.wav",
        store.audio_active_dir / "master.wav",
        store.audio_dir / "master.wav",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _upload_compose_deliverables(
    *,
    store: WorkspaceStore,
    scratch: Path,
    job_id: str,
    code: str,
    user_id: str,
    log_path: Path,
    task_logger: logging.Logger,
) -> dict[str, Any]:
    if not oss_configured():
        raise process_failed("OSS credentials not configured")

    keys = build_compose_object_keys(code=code, user_id=user_id, job_id=job_id)
    video_path = store.output_mp4
    audio_path = _resolve_master_audio_path(store, scratch)
    subtitle_ass_path = store.subtitles_active_dir / "subtitle.ass"

    if not video_path.is_file():
        raise process_failed("output video missing before OSS upload")
    if audio_path is None:
        raise process_failed("master audio missing before OSS upload")
    if not subtitle_ass_path.is_file():
        raise process_failed("subtitle ASS missing before OSS upload")
    if not log_path.is_file():
        task_logger.warning("compose log file missing: %s", log_path)

    uploads = (
        ("video", video_path, keys["video"]),
        ("audio", audio_path, keys["audio"]),
        ("subtitle_ass", subtitle_ass_path, keys["subtitle_ass"]),
    )
    if log_path.is_file():
        uploads = (*uploads, ("log", log_path, keys["log"]))

    for label, local_path, object_key in uploads:
        task_logger.info("Uploading %s -> %s", local_path, object_key)
        if not upload_file_oss(local_path, object_key):
            raise process_failed(f"upload {label} to OSS failed")

    return {
        "output_video_url": oss_public_url(keys["video"]),
        "output_video_object_key": keys["video"],
        "output_audio_url": oss_public_url(keys["audio"]),
        "output_audio_object_key": keys["audio"],
        "output_subtitle_ass_url": oss_public_url(keys["subtitle_ass"]),
        "output_subtitle_ass_object_key": keys["subtitle_ass"],
        "log_object_key": keys["log"] if log_path.is_file() else None,
        "log_url": oss_public_url(keys["log"]) if log_path.is_file() else None,
        "oss_prefix": keys["video"].rsplit("/", 1)[0],
        "workspace_deleted": False,
    }


def start_compose_job(
    store: WorkspaceStore,
    *,
    code: str,
    user_id: str,
    subtitle_mode: str = "hard",
    reuse_intermediates: bool = True,
    cleanup_scratch_on_success: bool = True,
    upstream_job_id: str | None = None,
    standalone_pipeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not standalone_pipeline:
        store.refresh_flags()
        if not store.flags.split_ready:
            raise bad_request(40004, "split_not_ready")
        if not store.flags.audio_confirmed:
            raise bad_request(40003, "audio_not_confirmed")
        if not store.flags.media_bound:
            raise bad_request(40004, "media_not_bound")

    with workspace_stage_claim_lock(store.workspace_id, "compose"):
        with _lock:
            store.refresh_flags()
            if store.workspace_id in _active or find_active_job(store, "compose"):
                raise conflict(40902, "compose_job_active")

            job_id = store.workspace_id
            job = {
                "job_id": job_id,
                "workspace_id": store.workspace_id,
                "status": "queued",
                "progress": None,
                "result": None,
                "error": None,
                "created_at": _now(),
                "updated_at": _now(),
                "options": {
                    "code": code,
                    "id": user_id,
                    "subtitle_mode": subtitle_mode,
                    "reuse_intermediates": reuse_intermediates,
                    "cleanup_scratch_on_success": cleanup_scratch_on_success,
                    "upstream_job_id": upstream_job_id,
                    **(
                        {"standalone_pipeline": standalone_pipeline}
                        if standalone_pipeline
                        else {}
                    ),
                },
            }
            save_job(store, job_id, job)
            _active[store.workspace_id] = job_id

    submit_api_job(
        _run_job,
        store.workspace_id,
        job_id,
        job_id=job_id,
        should_run=lambda: not _job_is_cancelled(
            load_job_store(store.workspace_id) or store,
            job_id,
        ),
    )

    return {
        "job_id": job_id,
        "status": "queued",
    }


def _run_job(workspace_id: str, job_id: str) -> None:
    store = load_job_store(workspace_id)
    if not store:
        return
    job = load_job(store, job_id) or {}
    scratch = store.root / "intermediate" / "compose" / job_id
    opts = job.get("options") or {}
    output_mtime_before = (
        store.output_mp4.stat().st_mtime if store.output_mp4.is_file() else None
    )
    job["output_mtime_before"] = output_mtime_before
    current_phase = "clip"

    log_path = _compose_log_dir() / f"{workspace_id}_{job_id}.log"
    task_logger = logging.getLogger(f"videoaudiotext.compose.{job_id}")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    task_logger.addHandler(file_handler)
    task_logger.setLevel(logging.INFO)

    def progress(phase: str, percent: int, message: str) -> None:
        nonlocal current_phase
        current_phase = phase
        if _job_is_cancelled(store, job_id):
            raise JobCancelled()
        job["status"] = "running"
        job["progress"] = {"phase": phase, "percent": percent, "message": message}
        job["updated_at"] = _now()
        job["heartbeat_at"] = _now()
        if store.root.is_dir():
            save_job(store, job_id, job)
        task_logger.info("%s (%s%%) %s", phase, percent, message)

    terminal_status: str | None = None
    cleaned_paths: list[str] = []
    workspace_deleted = False

    try:
        if _job_is_cancelled(store, job_id):
            raise JobCancelled()

        job["status"] = "running"
        job["updated_at"] = _now()
        job["heartbeat_at"] = _now()
        save_job(store, job_id, job)
        progress("clip", 10, "准备合成…")

        if opts.get("standalone_pipeline"):
            from videoaudiotext.api.pipeline_bootstrap import apply_standalone_pipeline

            apply_standalone_pipeline(
                store,
                opts["standalone_pipeline"],
                bind_media=True,
            )

        def log_fn(msg: str) -> None:
            task_logger.info(msg)
            if "xfade" in msg or "步骤" in msg:
                progress("compose", 50, msg[:120])

        result = run_flow_b_compose(
            store,
            scratch,
            subtitle_mode=str(opts.get("subtitle_mode") or "hard"),
            reuse_intermediates=bool(opts.get("reuse_intermediates", True)),
            log=log_fn,
        )

        if _job_is_cancelled(store, job_id):
            raise JobCancelled()

        progress("upload", 90, "上传 OSS…")
        oss_result = _upload_compose_deliverables(
            store=store,
            scratch=scratch,
            job_id=job_id,
            code=str(opts["code"]),
            user_id=str(opts["id"]),
            log_path=log_path,
            task_logger=task_logger,
        )
        result.update(oss_result)
        result["intermediates_cleaned"] = bool(opts.get("cleanup_scratch_on_success", True))

        job["status"] = "done"
        job["progress"] = {"phase": "finalize", "percent": 100, "message": "完成"}
        job["result"] = result
        job["error"] = None
        job["updated_at"] = _now()
        save_job(store, job_id, job)
        store.refresh_flags()
        store.mark_compose_completed()

        store.delete_tree()
        workspace_deleted = True
        result["job_context_deleted"] = True
        job["result"] = result
        archive_compose_job(job)

        terminal_status = "done"
        log_info(
            "compose_job_done",
            workspace_id=workspace_id,
            job_id=job_id,
            output_video_object_key=result.get("output_video_object_key"),
            output_subtitle_ass_object_key=result.get(
                "output_subtitle_ass_object_key"
            ),
        )
    except JobCancelled:
        fresh = load_job(store, job_id) or job
        if str(fresh.get("status") or "") == "cancelled":
            terminal_status = "cancelled"
            log_info(
                "compose_job_cancelled",
                workspace_id=workspace_id,
                job_id=job_id,
            )
        else:
            if fresh.get("error") is None:
                fresh["error"] = job_error_cancelled(phase=current_phase)
            fresh["status"] = "cancelled"
            fresh["updated_at"] = _now()
            if store.root.is_dir():
                save_job(store, job_id, fresh)
            _, cleaned_paths = finalize_terminal_job(
                store,
                fresh,
                scratch=scratch if store.root.is_dir() else None,
                output_mtime_before=output_mtime_before,
            )
            terminal_status = "cancelled"
            log_info(
                "compose_job_cancelled",
                workspace_id=workspace_id,
                job_id=job_id,
                cleaned_paths=cleaned_paths,
            )
    except ApiError as exc:
        job["status"] = "failed"
        job["error"] = job_error_from_api(exc, phase=current_phase)
        job["updated_at"] = _now()
        if store.root.is_dir():
            save_job(store, job_id, job)
        _, cleaned_paths = finalize_terminal_job(
            store,
            job,
            scratch=scratch,
            output_mtime_before=output_mtime_before,
        )
        terminal_status = "failed"
        log_error(
            "compose_job_failed",
            workspace_id=workspace_id,
            job_id=job_id,
            code=exc.code,
            message=exc.message,
            phase=current_phase,
        )
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = job_error_from_exception(exc, phase=current_phase)
        job["updated_at"] = _now()
        if store.root.is_dir():
            save_job(store, job_id, job)
        _, cleaned_paths = finalize_terminal_job(
            store,
            job,
            scratch=scratch,
            output_mtime_before=output_mtime_before,
        )
        terminal_status = "failed"
        log_error(
            "compose_job_failed",
            workspace_id=workspace_id,
            job_id=job_id,
            code=50001,
            message=str(exc),
            phase=current_phase,
        )
    finally:
        for handler in task_logger.handlers[:]:
            task_logger.removeHandler(handler)
            handler.close()
        if log_path.is_file() and terminal_status == "done" and workspace_deleted:
            try:
                log_path.unlink()
            except OSError:
                task_logger.warning("failed to remove local compose log: %s", log_path)
        if (
            not workspace_deleted
            and terminal_status == "done"
            and opts.get("cleanup_scratch_on_success", True)
            and scratch.is_dir()
        ):
            shutil.rmtree(scratch, ignore_errors=True)
        with _lock:
            if _active.get(workspace_id) == job_id:
                del _active[workspace_id]


def cancel_job(store: WorkspaceStore, job_id: str) -> dict[str, Any]:
    job = load_job(store, job_id)
    if not job:
        raced = finish_cancel_if_already_terminal(job_id)
        if raced:
            with _lock:
                if _active.get(store.workspace_id) == job_id:
                    del _active[store.workspace_id]
            return raced
        raise not_found("job not found")
    st = job.get("status")
    if st == "cancelled":
        return cancel_result(job_id)
    if st not in ("queued", "running"):
        raise job_not_cancellable(str(st))
    phase = (job.get("progress") or {}).get("phase")
    job["status"] = "cancelled"
    job["error"] = job_error_cancelled(phase=phase)
    job["updated_at"] = _now()
    cleaned_paths: list[str] = []
    output_mtime_before = job.get("output_mtime_before")
    scratch = store.root / "intermediate" / "compose" / job_id
    if store.root.is_dir():
        try:
            save_job(store, job_id, job)
            _, cleaned_paths = finalize_terminal_job(
                store,
                job,
                scratch=scratch,
                output_mtime_before=output_mtime_before,
            )
        except OSError:
            raced = finish_cancel_if_already_terminal(job_id)
            if raced:
                with _lock:
                    if _active.get(store.workspace_id) == job_id:
                        del _active[store.workspace_id]
                return raced
    else:
        raced = finish_cancel_if_already_terminal(job_id)
        if raced:
            with _lock:
                if _active.get(store.workspace_id) == job_id:
                    del _active[store.workspace_id]
            return raced
    with _lock:
        if _active.get(store.workspace_id) == job_id:
            del _active[store.workspace_id]
    return cancel_result(job_id, cleaned_paths=cleaned_paths or None)
