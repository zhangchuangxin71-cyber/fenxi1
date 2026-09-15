"""Hydrate ephemeral job stores from upstream archived jobs (stateless API)."""

from __future__ import annotations

from videoaudiotext.api.audio_deliverables import hydrate_bind_from_confirmed_audio
from videoaudiotext.api.errors import ApiError, bad_request
from videoaudiotext.api.job_context import create_job_store, load_job_record, read_job_index
from videoaudiotext.api.job_ephemeral import (
    consume_upstream_local_job,
    hydrate_store_from_archive,
    hydrate_store_from_upstream,
)
from videoaudiotext.api.workspace.store import WorkspaceStore


def require_done_job_record(job_id: str, *, kind: str) -> dict:
    index = read_job_index(job_id)
    if not index or index.get("kind") != kind:
        raise ApiError(404, 40401, "task not found")
    job = load_job_record(job_id)
    if not job:
        raise ApiError(404, 40401, "task not found")
    if job.get("status") != "done":
        raise ApiError(409, 40905, "upstream_job_not_done")
    return job


def prepare_split_job() -> WorkspaceStore:
    return create_job_store(kind="split")


def prepare_audio_job(split_job_id: str) -> WorkspaceStore:
    require_done_job_record(split_job_id, kind="split")
    store = create_job_store(kind="audio_process")
    hydrate_store_from_upstream(store, split_job_id)
    if not store.read_json(store.split_plan_path):
        job = load_job_record(split_job_id) or {}
        result = job.get("result") or {}
        segments = result.get("segments")
        if segments:
            store.write_json(
                store.split_plan_path,
                {
                    "text_digest": result.get("text_digest"),
                    "segments": segments,
                    "split_mode_used": result.get("split_mode_used", "rule"),
                },
            )
    if not store.read_json(store.split_plan_path):
        raise bad_request(40004, "split_not_ready")
    return store


def prepare_bind_job(audio_job_id: str) -> WorkspaceStore:
    audio_job = require_done_job_record(audio_job_id, kind="audio_process")
    store = create_job_store(kind="media_bind")
    if audio_job.get("audio_deliverables"):
        if not audio_job.get("audio_confirmed"):
            raise bad_request(40003, "audio_not_confirmed")
        hydrate_bind_from_confirmed_audio(store, audio_job)
    else:
        hydrate_store_from_upstream(store, audio_job_id)
    return store


def prepare_visual_job(bind_job_id: str) -> WorkspaceStore:
    require_done_job_record(bind_job_id, kind="media_bind")
    store = create_job_store(kind="visual_preview")
    hydrate_store_from_upstream(store, bind_job_id, download_media=True)
    consume_upstream_local_job(bind_job_id)
    return store


def prepare_compose_job(pipeline_job_id: str) -> WorkspaceStore:
    index_kind = None
    meta = read_job_index(pipeline_job_id)
    if meta:
        index_kind = meta.get("kind")
    if index_kind not in ("media_bind", "visual_preview"):
        raise bad_request(40001, "pipeline_job_id must be a completed bind or visual job")
    require_done_job_record(pipeline_job_id, kind=str(index_kind))
    store = create_job_store(kind="compose")
    hydrate_store_from_upstream(store, pipeline_job_id, download_media=True)
    consume_upstream_local_job(pipeline_job_id)
    return store
