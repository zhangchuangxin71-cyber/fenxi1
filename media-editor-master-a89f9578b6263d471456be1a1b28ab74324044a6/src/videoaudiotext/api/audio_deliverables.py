"""Audio stage OSS deliverables upload, confirm, and bind hydration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from videoaudiotext.api.errors import bad_request, conflict, not_found, process_failed
from videoaudiotext.api.job_archive import archive_job, load_archived_job
from videoaudiotext.api.job_context import load_job_store
from videoaudiotext.api.services import audio_revisions as rev
from videoaudiotext.api.url_fetch import fetch_url_to_path
from videoaudiotext.api.workspace.store import WorkspaceStore
from videoaudiotext.storage.oss import (
    build_audio_deliverable_keys,
    build_audio_segment_object_key,
    oss_configured,
    oss_public_url,
    upload_file_oss,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upload_audio_deliverables(
    store: WorkspaceStore,
    job: dict[str, Any],
    *,
    job_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Upload master, subtitle.srt, and segment wavs to OSS; return result + deliverables record."""
    opts = job.get("options") or {}
    code = str(opts.get("code") or "").strip()
    user_id = str(opts.get("id") or "").strip()
    if not code or not user_id:
        raise bad_request(40001, "code and id are required for audio OSS upload")
    if not oss_configured():
        raise process_failed("OSS credentials not configured")

    result = dict(job.get("result") or {})
    revision_id = str(result.get("revision_id") or "")
    if not revision_id:
        raise process_failed("revision_id missing before OSS upload")

    rev_audio = rev.revision_audio_dir(store, revision_id)
    rev_sub = rev.revision_subtitles_dir(store, revision_id)
    master_path = rev_audio / "master.wav"
    srt_path = rev_sub / "subtitle.srt"
    for path, label in (
        (master_path, "master.wav"),
        (srt_path, "subtitle.srt"),
    ):
        if not path.is_file():
            raise process_failed(f"{label} missing before OSS upload")

    keys = build_audio_deliverable_keys(code=code, user_id=user_id, job_id=job_id)
    uploads = (
        ("master_audio_url", master_path, keys["master"]),
        ("subtitle_srt_url", srt_path, keys["subtitle_srt"]),
    )
    deliverables: dict[str, Any] = {
        "revision_id": revision_id,
        "object_keys": {k: keys[k] for k in ("master", "subtitle_srt")},
    }
    segment_urls: dict[str, str] = {}

    for field, local_path, object_key in uploads:
        if not upload_file_oss(local_path, object_key):
            raise process_failed(f"upload {field} to OSS failed")
        url = oss_public_url(object_key)
        result[field] = url
        deliverables[field] = url

    for wav in sorted(rev_audio.glob("[0-9]*.wav"), key=lambda p: int(p.stem)):
        idx = int(wav.stem)
        seg_key = build_audio_segment_object_key(
            code=code,
            user_id=user_id,
            job_id=job_id,
            segment_index=idx,
        )
        if not upload_file_oss(wav, seg_key):
            raise process_failed(f"upload segment {idx} to OSS failed")
        segment_urls[str(idx)] = oss_public_url(seg_key)

    deliverables["segment_urls"] = segment_urls
    result["segment_urls"] = segment_urls
    for seg in result.get("segments") or []:
        idx = str(seg.get("index"))
        if idx in segment_urls:
            seg["audio_url"] = segment_urls[idx]
    result["audio_deliverables_uploaded"] = True
    return result, deliverables


def confirm_audio_job(
    job_id: str,
    *,
    revision_id: str | None = None,
    audio_config_digest: str | None = None,
    master_audio_url: str | None = None,
    subtitle_srt_url: str | None = None,
    subtitle_ass_url: str | None = None,
) -> dict[str, Any]:
    job = load_archived_job(job_id)
    store = load_job_store(job_id)
    if not job and store:
        from videoaudiotext.api.job_context import load_job_record

        job = load_job_record(job_id)
    if not job or job.get("status") != "done":
        raise not_found("job not found")

    result = dict(job.get("result") or {})
    deliverables = dict(job.get("audio_deliverables") or {})
    if not deliverables:
        if not store or not store.root.is_dir():
            raise bad_request(40004, "audio_deliverables_not_ready")
        rid = (revision_id or "").strip() or str(result.get("revision_id") or "")
        if not rid:
            raise bad_request(40001, "revision_id is required")
        rev.promote_revision_to_active(store, rid, confirmed=True, invalidate_media=True)
        payload = dict(job)
        payload["audio_confirmed"] = True
        payload["confirmed_at"] = _now_iso()
        cfg = dict(payload.get("embedded_audio_config") or store.read_json(store.audio_config_path) or {})
        cfg.update({"revision_id": rid, "confirmed": True, "preview": True})
        payload["embedded_audio_config"] = cfg
        archive_job(payload)
        return {
            "job_id": job_id,
            "revision_id": rid,
            "audio_config_digest": str(result.get("audio_config_digest") or ""),
            "confirmed": True,
        }

    rid = (revision_id or "").strip() or str(deliverables.get("revision_id") or "")
    if not rid:
        raise bad_request(40001, "revision_id is required")
    if str(result.get("revision_id") or "") != rid:
        raise not_found("revision not found", revision_id=rid)

    digest = str(result.get("audio_config_digest") or "")
    if audio_config_digest and audio_config_digest != digest:
        raise conflict(40901, "audio_config_digest mismatch")

    expected = {
        "master_audio_url": deliverables.get("master_audio_url"),
        "subtitle_srt_url": deliverables.get("subtitle_srt_url"),
    }
    provided = {
        "master_audio_url": (master_audio_url or "").strip() or None,
        "subtitle_srt_url": (subtitle_srt_url or "").strip() or None,
    }
    for key, exp in expected.items():
        if not exp:
            continue
        got = provided[key] or str(result.get(key) or "")
        if got and got != exp:
            raise conflict(40901, f"{key} mismatch")

    if store and store.root.is_dir():
        rev.promote_revision_to_active(store, rid, confirmed=True, invalidate_media=True)

    payload = dict(job)
    payload["audio_confirmed"] = True
    payload["confirmed_at"] = _now_iso()
    cfg = dict(payload.get("embedded_audio_config") or {})
    cfg.update(
        {
            "revision_id": rid,
            "confirmed": True,
            "preview": True,
            "audio_config_digest": digest,
            "audio_confirmed_digest": digest,
            "audio_preview_digest": digest,
        }
    )
    payload["embedded_audio_config"] = cfg
    result["status"] = "confirmed"
    payload["result"] = result
    archive_job(payload)
    if store and store.root.is_dir():
        store.delete_tree()
        payload["job_context_deleted"] = True

    return {
        "job_id": job_id,
        "revision_id": rid,
        "audio_config_digest": digest,
        "confirmed": True,
        "master_audio_url": expected["master_audio_url"],
        "subtitle_srt_url": expected["subtitle_srt_url"],
    }


def hydrate_bind_from_confirmed_audio(store: WorkspaceStore, audio_job: dict[str, Any]) -> None:
    if not audio_job.get("audio_confirmed"):
        raise bad_request(40003, "audio_not_confirmed")

    deliverables = audio_job.get("audio_deliverables") or {}
    if not deliverables:
        raise bad_request(40004, "audio_deliverables_not_ready")

    store.ensure_tree()
    if audio_job.get("embedded_split_plan"):
        store.write_json(store.split_plan_path, audio_job["embedded_split_plan"])
    cfg = dict(audio_job.get("embedded_audio_config") or {})
    cfg["confirmed"] = True
    from videoaudiotext.api.services.audio_revisions import resolve_audio_config_digest

    if not cfg.get("audio_config_digest"):
        cfg["audio_config_digest"] = resolve_audio_config_digest(cfg)
    store.write_json(store.audio_config_path, cfg)

    active_audio = store.audio_dir / "active"
    active_sub = store.subtitles_dir / "active"
    active_audio.mkdir(parents=True, exist_ok=True)
    active_sub.mkdir(parents=True, exist_ok=True)

    fetch_url_to_path(str(deliverables["master_audio_url"]), active_audio / "master.wav")
    fetch_url_to_path(str(deliverables["subtitle_srt_url"]), active_sub / "subtitle.srt")
    legacy_ass = str(deliverables.get("subtitle_ass_url") or "").strip()
    if legacy_ass:
        fetch_url_to_path(legacy_ass, active_sub / "subtitle.ass")

    for idx_str, url in (deliverables.get("segment_urls") or {}).items():
        fetch_url_to_path(str(url), active_audio / f"{idx_str}.wav")

    store.flags.audio_ready = True
    store.flags.audio_confirmed = True
    store.refresh_flags()
