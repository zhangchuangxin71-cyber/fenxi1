"""Async Job runners for split, audio/process, and visual/preview stages."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from videoaudiotext.concurrency.pools import submit_api_job
from videoaudiotext.concurrency.coordination import workspace_stage_claim_lock
from typing import Any, Callable

from videoaudiotext.api.api_logging import log_error, log_info
from videoaudiotext.api.errors import ApiError, bad_request, conflict, not_found
from videoaudiotext.api.job_cancel import (
    cancel_result,
    finish_cancel_if_already_terminal,
    is_cancelled,
    is_job_cancelled,
    job_not_cancellable,
)
from videoaudiotext.api.job_common import (
    ACTIVE_JOB_STATUSES,
    find_active_job,
    job_is_cancelled,
    load_job_by_prefix,
    make_cancel_check,
    new_job_id,
    now_iso,
    save_job_by_prefix,
    save_job_by_prefix_if_live,
)
from videoaudiotext.api.job_errors import (
    JobCancelled,
    job_error_cancelled,
    job_error_from_api,
    job_error_from_exception,
)
from videoaudiotext.api.services import audio as audio_service
from videoaudiotext.api.services import split as split_service
from videoaudiotext.api.services import visual as visual_service
from videoaudiotext.api.job_archive import archive_job
from videoaudiotext.api.job_context import load_job_store
from videoaudiotext.api.job_ephemeral import (
    finalize_stage_job,
    finalize_terminal_job,
    require_oss_for_deliverable_upload,
)
from videoaudiotext.api.workspace.store import WorkspaceStore

_lock = threading.Lock()
_active: dict[tuple[str, str], str] = {}  # (workspace_id, prefix) -> job_id

STAGE_JOB_PREFIXES = ("split", "audio_process", "visual_preview")


@dataclass(frozen=True)
class StageJobSpec:
    prefix: str
    route_base: str
    conflict_message: str


SPLIT_JOB = StageJobSpec("split", "split", "split_job_active")
AUDIO_PROCESS_JOB = StageJobSpec("audio_process", "audio/process", "audio_process_job_active")
VISUAL_PREVIEW_JOB = StageJobSpec("visual_preview", "visual/preview", "visual_preview_job_active")


def load_stage_job(store: WorkspaceStore, prefix: str, job_id: str) -> dict[str, Any] | None:
    return load_job_by_prefix(store, prefix, job_id)


def _release_active(workspace_id: str, prefix: str, job_id: str) -> None:
    with _lock:
        if _active.get((workspace_id, prefix)) == job_id:
            del _active[(workspace_id, prefix)]


def _start_job(
    store: WorkspaceStore,
    spec: StageJobSpec,
    options: dict[str, Any],
    *,
    precheck: Callable[[WorkspaceStore], None] | None = None,
) -> dict[str, Any]:
    if precheck:
        precheck(store)

    with workspace_stage_claim_lock(store.workspace_id, spec.prefix):
        with _lock:
            if _active.get((store.workspace_id, spec.prefix)):
                raise conflict(40902, spec.conflict_message)
            if find_active_job(store, spec.prefix):
                raise conflict(40902, spec.conflict_message)

            job_id = store.workspace_id
            job = {
                "job_id": job_id,
                "kind": spec.prefix,
                "status": "queued",
                "progress": None,
                "result": None,
                "error": None,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "options": options,
            }
            save_job_by_prefix(store, spec.prefix, job_id, job)
            _active[(store.workspace_id, spec.prefix)] = job_id

    submit_api_job(
        _run_job,
        store.workspace_id,
        spec,
        job_id,
        job_id=job_id,
        should_run=lambda: not job_is_cancelled(
            load_job_store(store.workspace_id) or store,
            spec.prefix,
            job_id,
        ),
    )

    return {
        "job_id": job_id,
        "status": "queued",
    }


def _log_stage_cancelled(store: WorkspaceStore, job_id: str, spec: StageJobSpec) -> None:
    log_info(
        "stage_job_cancelled",
        workspace_id=store.workspace_id,
        job_id=job_id,
        kind=spec.prefix,
    )


def _run_job(workspace_id: str, spec: StageJobSpec, job_id: str) -> None:
    store = load_job_store(workspace_id)
    if not store:
        return

    job = load_job_by_prefix(store, spec.prefix, job_id) or {}
    opts = job.get("options") or {}
    cancel_check = make_cancel_check(store, spec.prefix, job_id)
    current_phase = spec.prefix

    def progress(phase: str, percent: int, message: str) -> None:
        nonlocal current_phase
        current_phase = phase
        cancel_check()
        job["status"] = "running"
        job["progress"] = {"phase": phase, "percent": percent, "message": message}
        job["updated_at"] = now_iso()
        job["heartbeat_at"] = now_iso()
        if store.root.is_dir():
            save_job_by_prefix_if_live(store, spec.prefix, job_id, job)

    terminal_status: str | None = None

    try:
        if job_is_cancelled(store, spec.prefix, job_id):
            raise JobCancelled()

        job["status"] = "running"
        job["updated_at"] = now_iso()
        job["heartbeat_at"] = now_iso()
        if store.root.is_dir():
            save_job_by_prefix_if_live(store, spec.prefix, job_id, job)

        if spec.prefix == "split":
            progress("split", 5, "准备分句…")
            result = split_service.run_split(
                store,
                str(opts.get("text") or ""),
                include_ai_prompts=bool(opts.get("include_ai_prompts")),
                global_style=_global_style_from_opts(opts),
                cancel_check=cancel_check,
                on_progress=progress,
            )
        elif spec.prefix == "audio_process":
            progress("fetch", 5, "准备处理配音…")
            result = audio_service.run_audio_process(
                store,
                opts.get("segments") or [],
                force_refresh=bool(opts.get("force_refresh")),
                speed=float(opts.get("speed") or 1.0),
                cancel_check=cancel_check,
                on_progress=progress,
            )
        elif spec.prefix == "visual_preview":
            preview_input = opts.get("preview_input")
            if preview_input:
                from videoaudiotext.api.pipeline_bootstrap import bootstrap_visual_preview

                bootstrap_visual_preview(
                    store,
                    media_url=str(preview_input["media_url"]),
                    media_type=str(preview_input["media_type"]),
                    text=str(preview_input["text"]),
                    start_sec=float(preview_input.get("start_sec") or 0.0),
                    on_progress=progress,
                )
            progress("preview", 5, "准备生成预览…")
            result = visual_service.run_visual_preview(
                store,
                opts.get("resolution") or {},
                opts.get("subtitle_style") or {},
                cancel_check=cancel_check,
                on_progress=progress,
            )
        else:
            raise RuntimeError(f"unknown stage job: {spec.prefix}")

        cancel_check()
        if is_job_cancelled(job_id, store, spec.prefix):
            raise JobCancelled()
        job["progress"] = {"phase": "finalize", "percent": 99, "message": "归档中…"}
        job["result"] = result
        job["error"] = None
        job["updated_at"] = now_iso()
        save_job_by_prefix_if_live(store, spec.prefix, job_id, job)
        try:
            job = finalize_stage_job(store, job, spec.prefix)
        except ApiError as exc:
            if is_job_cancelled(job_id, store, spec.prefix):
                terminal_status = "cancelled"
                _log_stage_cancelled(store, job_id, spec)
                return
            job["status"] = "failed"
            job["error"] = job_error_from_api(exc, phase="finalize")
            job["updated_at"] = now_iso()
            save_job_by_prefix_if_live(store, spec.prefix, job_id, job)
            finalize_terminal_job(store, job)
            terminal_status = "failed"
            log_error(
                "stage_job_failed",
                workspace_id=store.workspace_id,
                job_id=job_id,
                kind=spec.prefix,
                code=exc.code,
                message=exc.message,
            )
            return
        if is_job_cancelled(job_id, store, spec.prefix):
            raise JobCancelled()
        job["status"] = "done"
        job["progress"] = {"phase": "finalize", "percent": 100, "message": "完成"}
        job["updated_at"] = now_iso()
        if store.root.is_dir():
            save_job_by_prefix_if_live(store, spec.prefix, job_id, job)
        else:
            payload = dict(job)
            payload["job_id"] = job_id
            payload.setdefault("kind", spec.prefix)
            archive_job(payload)
        terminal_status = "done"
        log_info(
            "stage_job_done",
            workspace_id=store.workspace_id,
            job_id=job_id,
            kind=spec.prefix,
        )
    except JobCancelled:
        fresh = load_job_by_prefix(store, spec.prefix, job_id) or job
        if str(fresh.get("status") or "") != "cancelled":
            if fresh.get("error") is None:
                fresh["error"] = job_error_cancelled(phase=current_phase)
            fresh["status"] = "cancelled"
            fresh["updated_at"] = now_iso()
            if store.root.is_dir():
                save_job_by_prefix_if_live(store, spec.prefix, job_id, fresh)
                if spec.prefix == "audio_process":
                    audio_service.cleanup_audio_process_ephemeral(store)
                finalize_terminal_job(store, fresh)
        terminal_status = "cancelled"
        _log_stage_cancelled(store, job_id, spec)
    except ApiError as exc:
        if is_job_cancelled(job_id, store, spec.prefix):
            terminal_status = "cancelled"
            _log_stage_cancelled(store, job_id, spec)
        else:
            job["status"] = "failed"
            job["error"] = job_error_from_api(exc, phase=current_phase)
            job["updated_at"] = now_iso()
            save_job_by_prefix_if_live(store, spec.prefix, job_id, job)
            if spec.prefix == "audio_process":
                audio_service.cleanup_audio_process_ephemeral(store)
            finalize_terminal_job(store, job)
            terminal_status = "failed"
            log_error(
                "stage_job_failed",
                workspace_id=store.workspace_id,
                job_id=job_id,
                kind=spec.prefix,
                code=exc.code,
                message=exc.message,
            )
    except Exception as exc:
        if is_job_cancelled(job_id, store, spec.prefix):
            terminal_status = "cancelled"
            _log_stage_cancelled(store, job_id, spec)
        else:
            job["status"] = "failed"
            job["error"] = job_error_from_exception(exc, phase=current_phase)
            job["updated_at"] = now_iso()
            save_job_by_prefix_if_live(store, spec.prefix, job_id, job)
            if spec.prefix == "audio_process":
                audio_service.cleanup_audio_process_ephemeral(store)
            finalize_terminal_job(store, job)
            terminal_status = "failed"
            log_error(
                "stage_job_failed",
                workspace_id=store.workspace_id,
                job_id=job_id,
                kind=spec.prefix,
                code=50001,
                message=str(exc),
            )
    finally:
        _release_active(workspace_id, spec.prefix, job_id)


def _global_style_from_opts(opts: dict[str, Any]) -> str | None:
    gs = opts.get("global_style")
    if isinstance(gs, str):
        return gs
    legacy = opts.get("prompt_style")
    if isinstance(legacy, dict):
        legacy_gs = legacy.get("global_style")
        if isinstance(legacy_gs, str):
            return legacy_gs
    return None


def start_split_job(
    store: WorkspaceStore,
    *,
    text: str,
    include_ai_prompts: bool = False,
    global_style: str | None = None,
) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise bad_request(40001, "text is required")
    return _start_job(
        store,
        SPLIT_JOB,
        {
            "text": text,
            "include_ai_prompts": include_ai_prompts,
            "global_style": global_style,
        },
    )


def start_audio_process_job(
    store: WorkspaceStore,
    segments: list[dict[str, Any]],
    *,
    code: str,
    user_id: str,
    force_refresh: bool = False,
    speed: float = 1.0,
    upstream_job_id: str | None = None,
) -> dict[str, Any]:
    def precheck(ws: WorkspaceStore) -> None:
        ws.refresh_flags()
        if not ws.flags.split_ready:
            raise bad_request(40004, "split_not_ready")
        require_oss_for_deliverable_upload()

    return _start_job(
        store,
        AUDIO_PROCESS_JOB,
        {
            "code": code,
            "id": user_id,
            "segments": segments,
            "force_refresh": force_refresh,
            "speed": speed,
            "upstream_job_id": upstream_job_id,
        },
        precheck=precheck,
    )


def start_visual_preview_job(
    store: WorkspaceStore,
    *,
    code: str,
    user_id: str,
    resolution: dict[str, Any],
    subtitle_style: dict[str, Any],
    preview_input: dict[str, Any],
    upstream_job_id: str | None = None,
) -> dict[str, Any]:
    require_oss_for_deliverable_upload()
    return _start_job(
        store,
        VISUAL_PREVIEW_JOB,
        {
            "code": code,
            "id": user_id,
            "resolution": resolution,
            "subtitle_style": subtitle_style,
            "preview_input": preview_input,
            "preview_segment_index": 1,
            "upstream_job_id": upstream_job_id,
        },
    )


def cancel_stage_job(store: WorkspaceStore, prefix: str, job_id: str) -> dict[str, Any]:
    job = load_job_by_prefix(store, prefix, job_id)
    if not job:
        raced = finish_cancel_if_already_terminal(job_id)
        if raced:
            _release_active(store.workspace_id, prefix, job_id)
            return raced
        raise not_found("job not found")
    st = job.get("status")
    if st == "cancelled":
        return cancel_result(job_id)
    if st not in ACTIVE_JOB_STATUSES:
        raise job_not_cancellable(str(st))
    phase = (job.get("progress") or {}).get("phase")
    job["status"] = "cancelled"
    job["error"] = job_error_cancelled(phase=phase)
    job["updated_at"] = now_iso()
    cleaned_paths: list[str] = []
    if store.root.is_dir():
        try:
            save_job_by_prefix_if_live(store, prefix, job_id, job)
            if prefix == "audio_process":
                cleared, raw_removed = audio_service.cleanup_audio_process_ephemeral(store)
                if cleared:
                    cleaned_paths.append("intermediate/audio_process/_scratch")
                if raw_removed:
                    cleaned_paths.append(f"uploads/audio_raw_* ({raw_removed})")
            _, terminal_cleaned = finalize_terminal_job(store, job)
            cleaned_paths.extend(terminal_cleaned)
        except OSError:
            raced = finish_cancel_if_already_terminal(job_id)
            if raced:
                _release_active(store.workspace_id, prefix, job_id)
                return raced
    else:
        raced = finish_cancel_if_already_terminal(job_id)
        if raced:
            _release_active(store.workspace_id, prefix, job_id)
            return raced
    _release_active(store.workspace_id, prefix, job_id)
    return cancel_result(job_id, cleaned_paths=cleaned_paths or None)


def any_active_stage_job(store: WorkspaceStore) -> str | None:
    for prefix in STAGE_JOB_PREFIXES:
        active = find_active_job(store, prefix)
        if active:
            return str(active.get("job_id") or "")
    with _lock:
        for (ws_id, _prefix), job_id in _active.items():
            if ws_id == store.workspace_id:
                return job_id
    return None


def active_stage_job_ids(store: WorkspaceStore) -> dict[str, str | None]:
    return {
        "active_split_job_id": (find_active_job(store, "split") or {}).get("job_id"),
        "active_audio_process_job_id": (find_active_job(store, "audio_process") or {}).get(
            "job_id"
        ),
        "active_visual_preview_job_id": (find_active_job(store, "visual_preview") or {}).get(
            "job_id"
        ),
    }
