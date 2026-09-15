"""Ephemeral job storage: local temp dirs + OSS deliverables only."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from videoaudiotext.api.errors import bad_request, process_failed
from videoaudiotext.api.job_archive import archive_job, load_archived_job
from videoaudiotext.api.url_fetch import fetch_url_to_path
from videoaudiotext.api.workspace.store import WorkspaceStore
from videoaudiotext.storage.oss import (
    build_preview_object_key,
    oss_configured,
    oss_public_url,
    upload_file_oss,
)

_PIPELINE_DIRS = (
    "config",
    "audio",
    "subtitles",
    "source_media",
    "previews",
)


def ephemeral_jobs_enabled() -> bool:
    raw = os.environ.get("JOB_EPHEMERAL", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def delete_local_on_terminal_enabled() -> bool:
    """When true, archive terminal jobs and remove jobs/{task_id}/ (default: on)."""
    raw = os.environ.get("JOB_DELETE_LOCAL_ON_TERMINAL", "").strip().lower()
    if not raw:
        return ephemeral_jobs_enabled()
    return raw not in ("0", "false", "no", "off")


def should_delete_local_job_dir() -> bool:
    return ephemeral_jobs_enabled() and delete_local_on_terminal_enabled()


def require_oss_for_deliverable_upload() -> None:
    """Strict mode: audio/preview/compose deliverables must upload to OSS."""
    if not should_delete_local_job_dir():
        return
    if not oss_configured():
        raise process_failed("OSS credentials not configured")


def archive_and_maybe_delete_local(
    store: WorkspaceStore,
    payload: dict[str, Any],
    *,
    delete_local: bool | None = None,
) -> dict[str, Any]:
    job_payload = dict(payload)
    do_delete = should_delete_local_job_dir() if delete_local is None else delete_local
    archive_job(job_payload)
    job_payload["job_context_deleted"] = False
    if do_delete and store.root.is_dir():
        store.delete_tree()
        job_payload["job_context_deleted"] = True
    return job_payload


def finalize_terminal_job(
    store: WorkspaceStore,
    job: dict[str, Any],
    *,
    scratch: Path | None = None,
    output_mtime_before: float | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Archive a terminal job and optionally delete the local job directory."""
    from videoaudiotext.api.reconcile import cleanup_compose_artifacts

    cleaned_paths: list[str] = []
    if store.root.is_dir() and scratch is not None:
        cleaned_paths = cleanup_compose_artifacts(
            store,
            scratch,
            output_mtime_before=output_mtime_before,
        )

    payload = dict(job)
    if cleaned_paths:
        err = dict(payload.get("error") or {})
        err["data"] = {**(err.get("data") or {}), "cleaned_paths": cleaned_paths}
        payload["error"] = err

    finalized = archive_and_maybe_delete_local(store, payload)
    if finalized.get("job_context_deleted"):
        cleaned_paths.append(payload.get("job_id") or store.workspace_id)
    return finalized, cleaned_paths


def purge_terminal_job_dir(store: WorkspaceStore) -> bool:
    """Archive all terminal jobs under a job dir, then delete the directory."""
    from videoaudiotext.api.reconcile import TERMINAL_JOB_STATUSES

    if not should_delete_local_job_dir() or not store.root.is_dir():
        return False
    jobs_dir = store.root / "jobs"
    if not jobs_dir.is_dir():
        return False

    terminal_jobs: list[dict[str, Any]] = []
    for job_path in sorted(jobs_dir.glob("*.json")):
        job = store.read_json(job_path)
        if not job:
            continue
        if str(job.get("status") or "") in TERMINAL_JOB_STATUSES:
            terminal_jobs.append(job)
    if not terminal_jobs:
        return False

    for job in terminal_jobs:
        archive_job(job)
    store.delete_tree()
    return True


def delete_local_job_dir(job_id: str) -> bool:
    from videoaudiotext.api.job_context import load_job_store

    store = load_job_store(job_id)
    if store and store.root.is_dir():
        store.delete_tree()
        return True
    return False


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.is_dir():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def copy_pipeline_state_from_live(
    source: WorkspaceStore,
    target: WorkspaceStore,
) -> None:
    """Copy pipeline temp artifacts from live upstream job into a new job dir."""
    target.ensure_tree()
    for name in _PIPELINE_DIRS:
        _copy_tree(source.root / name, target.root / name)
    target.refresh_flags()


def _embed_pipeline_configs(store: WorkspaceStore, payload: dict[str, Any]) -> None:
    split_plan = store.read_json(store.split_plan_path)
    if split_plan:
        payload["embedded_split_plan"] = split_plan
    audio_cfg = store.read_json(store.audio_config_path)
    if audio_cfg:
        payload["embedded_audio_config"] = audio_cfg
    media_binding = store.read_json(store.media_binding_path)
    if media_binding:
        payload["embedded_media_binding"] = media_binding
    render_style = store.read_json(store.render_style_path)
    if render_style:
        payload["embedded_render_style"] = render_style


def _media_sources_from_binding(store: WorkspaceStore) -> list[dict[str, Any]]:
    binding = store.read_json(store.media_binding_path) or {}
    sources: list[dict[str, Any]] = []
    for seg in binding.get("segments") or []:
        media = seg.get("media") or {}
        url = str(media.get("source_url") or "").strip()
        if not url:
            continue
        mtype = str(media.get("type") or "video").lower()
        local_path = str(media.get("local_path") or "")
        ext = Path(local_path).suffix if local_path else (".mp4" if mtype == "video" else ".jpg")
        sources.append(
            {
                "index": int(seg["index"]),
                "url": url,
                "type": mtype,
                "start_sec": float(media.get("start_sec") or 0.0),
                "ext": ext,
            }
        )
    return sources


def _attach_upstream_audio_deliverables(payload: dict[str, Any]) -> None:
    if payload.get("audio_deliverables"):
        payload.setdefault("audio_confirmed", True)
        return
    candidates: list[str] = []
    upstream = (payload.get("options") or {}).get("upstream_job_id")
    if upstream:
        candidates.append(str(upstream))
    candidates.extend(str(uid) for uid in payload.get("upstream_job_ids") or [])
    seen: set[str] = set()
    for uid in candidates:
        if uid in seen:
            continue
        seen.add(uid)
        record = load_archived_job(uid)
        if not record or not record.get("audio_deliverables"):
            continue
        payload["audio_deliverables"] = record["audio_deliverables"]
        payload["audio_confirmed"] = record.get("audio_confirmed", True)
        payload.setdefault("embedded_split_plan", record.get("embedded_split_plan"))
        payload.setdefault("embedded_audio_config", record.get("embedded_audio_config"))
        return


def _can_restore_pipeline_from_archive(payload: dict[str, Any]) -> bool:
    if not oss_configured():
        return False
    sources = payload.get("media_sources") or []
    if sources:
        return all(str(src.get("url") or "").startswith("http") for src in sources)
    binding = payload.get("embedded_media_binding") or {}
    segments = binding.get("segments") or []
    if not segments:
        return False
    return all(
        str((seg.get("media") or {}).get("source_url") or "").startswith("http")
        for seg in segments
    )


def _restore_audio_deliverables_from_archive(
    store: WorkspaceStore,
    archived: dict[str, Any],
) -> None:
    from videoaudiotext.api.audio_deliverables import hydrate_bind_from_confirmed_audio

    record = dict(archived)
    _attach_upstream_audio_deliverables(record)
    if record.get("audio_deliverables") and record.get("audio_confirmed"):
        hydrate_bind_from_confirmed_audio(store, record)


def _upload_visual_preview(
    store: WorkspaceStore,
    job: dict[str, Any],
    *,
    job_id: str,
) -> dict[str, Any]:
    opts = job.get("options") or {}
    code = str(opts.get("code") or "").strip()
    user_id = str(opts.get("id") or "").strip()
    if not code or not user_id:
        raise bad_request(40001, "code and id are required for visual preview OSS upload")
    require_oss_for_deliverable_upload()
    if not oss_configured():
        return dict(job.get("result") or {})

    preview_index = int(opts.get("preview_segment_index") or 1)
    preview_path = store.previews_dir / f"seg_{preview_index}.jpg"
    if not preview_path.is_file():
        raise process_failed("preview image missing before OSS upload")

    object_key = build_preview_object_key(
        code=code,
        user_id=user_id,
        job_id=job_id,
        segment_index=preview_index,
    )
    if not upload_file_oss(preview_path, object_key):
        raise process_failed("upload preview to OSS failed")

    result = dict(job.get("result") or {})
    preview_url = oss_public_url(object_key)
    if isinstance(result.get("preview"), dict):
        result["preview"] = {**result["preview"], "preview_image_url": preview_url}
    result["preview_image_url"] = preview_url
    result["preview_object_key"] = object_key
    return result


def finalize_stage_job(
    store: WorkspaceStore,
    job: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    """Archive metadata; upload OSS deliverables where applicable; delete local when safe."""
    if not ephemeral_jobs_enabled():
        return job

    job_id = str(job["job_id"])
    opts = job.get("options") or {}
    upstream = opts.get("upstream_job_id")
    upstream_ids: list[str] = []
    if upstream:
        upstream_ids.append(str(upstream))

    payload = dict(job)
    result = dict(payload.get("result") or {})

    if kind == "split":
        _embed_pipeline_configs(store, payload)
        payload["artifact_manifest"] = []
        payload["upstream_job_ids"] = upstream_ids
        return archive_and_maybe_delete_local(store, payload)

    if kind == "audio_process":
        from videoaudiotext.api.audio_deliverables import upload_audio_deliverables

        require_oss_for_deliverable_upload()
        _embed_pipeline_configs(store, payload)
        result, deliverables = upload_audio_deliverables(store, job, job_id=job_id)
        payload["result"] = result
        payload["audio_deliverables"] = deliverables
        payload["audio_confirmed"] = False
        payload["artifact_manifest"] = []
        payload["upstream_job_ids"] = upstream_ids
        return archive_and_maybe_delete_local(store, payload)

    if kind == "media_bind":
        _embed_pipeline_configs(store, payload)
        media_sources = _media_sources_from_binding(store)
        if media_sources:
            payload["media_sources"] = media_sources
        _attach_upstream_audio_deliverables(payload)
        payload["artifact_manifest"] = []
        payload["upstream_job_ids"] = upstream_ids
        payload["result"] = result
        delete_local = should_delete_local_job_dir() and _can_restore_pipeline_from_archive(
            payload
        )
        return archive_and_maybe_delete_local(store, payload, delete_local=delete_local)

    if kind == "visual_preview":
        _embed_pipeline_configs(store, payload)
        media_sources = _media_sources_from_binding(store)
        if media_sources:
            payload["media_sources"] = media_sources
        _attach_upstream_audio_deliverables(payload)
        payload["result"] = _upload_visual_preview(store, job, job_id=job_id)
        payload["artifact_manifest"] = []
        payload["upstream_job_ids"] = upstream_ids
        delete_local = should_delete_local_job_dir() and _can_restore_pipeline_from_archive(
            payload
        )
        return archive_and_maybe_delete_local(store, payload, delete_local=delete_local)

    return job


def hydrate_store_from_upstream(
    target: WorkspaceStore,
    upstream_job_id: str,
    *,
    download_media: bool = False,
) -> None:
    from videoaudiotext.api.job_context import load_job_record, load_job_store

    live = load_job_store(upstream_job_id)
    if live:
        copy_pipeline_state_from_live(live, target)
        return
    record = load_job_record(upstream_job_id)
    if not record:
        from videoaudiotext.api.errors import not_found

        raise not_found("job not found")
    hydrate_store_from_archive(target, record, download_media=download_media)


def hydrate_store_from_archive(
    store: WorkspaceStore,
    archived: dict[str, Any],
    *,
    download_media: bool = False,
) -> None:
    """Rebuild a job dir from archived embedded configs (no OSS staging replay)."""
    store.ensure_tree()
    if archived.get("embedded_split_plan"):
        store.write_json(store.split_plan_path, archived["embedded_split_plan"])
    if archived.get("embedded_audio_config"):
        from videoaudiotext.api.services.audio_revisions import resolve_audio_config_digest

        audio_cfg = dict(archived["embedded_audio_config"])
        if not audio_cfg.get("audio_config_digest"):
            audio_cfg["audio_config_digest"] = resolve_audio_config_digest(audio_cfg)
        store.write_json(store.audio_config_path, audio_cfg)
    if archived.get("embedded_media_binding"):
        store.write_json(store.media_binding_path, archived["embedded_media_binding"])
    if archived.get("embedded_render_style"):
        store.write_json(store.render_style_path, archived["embedded_render_style"])

    if download_media:
        _restore_audio_deliverables_from_archive(store, archived)
        binding = store.read_json(store.media_binding_path) or {}
        offsets: dict[str, float] = {}
        for src in archived.get("media_sources") or []:
            idx = int(src["index"])
            url = str(src["url"]).strip()
            ext = str(src.get("ext") or ".mp4")
            mtype = str(src.get("type") or "video")
            dest = store.source_media_dir / f"{idx}{ext}"
            fetch_url_to_path(url, dest)
            start_sec = float(src.get("start_sec") or 0.0)
            if mtype == "video" and start_sec > 0:
                offsets[str(idx)] = start_sec
            seg = next(
                (s for s in binding.get("segments") or [] if int(s.get("index") or 0) == idx),
                None,
            )
            if seg:
                media = dict(seg.get("media") or {})
                media["local_path"] = f"source_media/{idx}{ext}"
                seg["media"] = media
        if offsets:
            offsets_path = store.source_media_dir / "clip_offsets.json"
            offsets_path.parent.mkdir(parents=True, exist_ok=True)
            offsets_path.write_text(
                json.dumps(offsets, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        elif (store.source_media_dir / "clip_offsets.json").is_file():
            (store.source_media_dir / "clip_offsets.json").unlink(missing_ok=True)

    store.refresh_flags()


def consume_upstream_local_job(upstream_job_id: str) -> None:
    """After copying upstream temp into a new job, delete upstream local dir."""
    if not ephemeral_jobs_enabled():
        return
    delete_local_job_dir(upstream_job_id)
