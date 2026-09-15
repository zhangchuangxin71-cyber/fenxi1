"""Bootstrap ephemeral job stores from self-contained request bodies (no upstream task_id)."""

from __future__ import annotations

import logging
import shutil
from typing import Any, Callable, Sequence

from videoaudiotext.api.errors import bad_request
from videoaudiotext.api.job_context import create_job_store
from videoaudiotext.api.services import audio_revisions as rev
from videoaudiotext.api.services import media as media_service
from videoaudiotext.api.url_fetch import fetch_url_to_path
from videoaudiotext.api.workspace.digests import audio_preview_digest, text_digest_from_segments
from videoaudiotext.api.workspace.store import WorkspaceStore
from videoaudiotext.audio.gaps import compute_sentence_gaps, segment_video_durations
from videoaudiotext.audio.srt_timing import speech_durations_and_gaps_from_srt
from videoaudiotext.storage.oss import (
    download_video_snapshot_oss,
    oss_configured,
    preview_video_oss_snapshot_enabled,
    preview_video_oss_snapshot_fallback_enabled,
    resolve_oss_object_key,
)
from videoaudiotext.tts.audio import get_audio_duration_seconds

logger = logging.getLogger(__name__)


def bootstrap_split_plan(
    store: WorkspaceStore,
    segments: Sequence[dict[str, Any]],
) -> str:
    """Write split_plan.json from client segments. Returns text_digest."""
    if not segments:
        raise bad_request(40001, "segments is required")
    plan_segments: list[dict[str, Any]] = []
    texts: list[str] = []
    seen_indices: set[int] = set()
    for item in segments:
        idx = int(item["index"])
        if idx in seen_indices:
            raise bad_request(40001, f"duplicate segment index {idx}")
        seen_indices.add(idx)
        text = str(item["text"]).strip()
        if not text:
            raise bad_request(40001, f"empty text at index {idx}")
        texts.append(text)
        plan_segments.append(
            {
                "index": idx,
                "text": text,
                "search_query": str(item.get("search_query") or ""),
                "visual": bool(item.get("visual", True)),
            }
        )
    digest = text_digest_from_segments(texts)
    store.ensure_tree()
    store.write_json(
        store.split_plan_path,
        {
            "text_digest": digest,
            "segments": plan_segments,
            "split_mode_used": "client",
        },
    )
    store.refresh_flags()
    return digest


def bootstrap_confirmed_audio(
    store: WorkspaceStore,
    *,
    master_audio_url: str,
    subtitle_srt_url: str,
    subtitle_ass_url: str | None = None,
    segment_items: Sequence[dict[str, Any]],
    segment_urls: dict[str, str] | None = None,
    revision_id: str | None = None,
    audio_config_digest: str | None = None,
    speed: float = 1.0,
) -> str:
    """Fetch OSS deliverables and mark audio confirmed for compose bootstrap."""
    rid = (revision_id or rev.new_revision_id()).strip()
    store.ensure_tree()
    active_audio = store.audio_dir / "active"
    active_sub = store.subtitles_dir / "active"
    active_audio.mkdir(parents=True, exist_ok=True)
    active_sub.mkdir(parents=True, exist_ok=True)

    fetch_url_to_path(str(master_audio_url).strip(), active_audio / "master.wav")
    srt_path = active_sub / "subtitle.srt"
    fetch_url_to_path(str(subtitle_srt_url).strip(), srt_path)
    ass_url = str(subtitle_ass_url or "").strip()
    if ass_url:
        fetch_url_to_path(ass_url, active_sub / "subtitle.ass")

    texts = [str(item["text"]).strip() for item in segment_items]
    need_srt_timing = any(item.get("duration_sec") is None for item in segment_items)
    srt_durations: list[float] | None = None
    srt_gaps: list[float] | None = None
    try:
        srt_durations, srt_gaps = speech_durations_and_gaps_from_srt(srt_path, texts)
    except ValueError as exc:
        if need_srt_timing:
            raise bad_request(40001, "subtitle_srt_timing_invalid", detail=str(exc)) from exc

    seg_results: list[dict[str, Any]] = []
    durations: list[float] = []
    for i, item in enumerate(segment_items):
        idx = int(item["index"])
        text = str(item["text"]).strip()
        if segment_urls and str(idx) in segment_urls:
            wav_path = active_audio / f"{idx}.wav"
            if not wav_path.is_file():
                fetch_url_to_path(str(segment_urls[str(idx)]).strip(), wav_path)
        if item.get("duration_sec") is not None:
            dur = float(item["duration_sec"])
        elif srt_durations is not None:
            dur = srt_durations[i]
        elif segment_urls and str(idx) in segment_urls:
            dur = get_audio_duration_seconds(active_audio / f"{idx}.wav")
        else:
            raise bad_request(
                40001,
                "duration_sec or subtitle_srt_url timing required",
                index=idx,
            )
        durations.append(dur)
        seg_results.append(
            {
                "index": idx,
                "text": text,
                "duration_sec": dur,
                "clip_duration_sec": float(item.get("clip_duration_sec") or dur),
            }
        )

    if srt_gaps is not None:
        gaps = list(srt_gaps)
    else:
        gaps = list(compute_sentence_gaps(texts, speed=speed))
    clip_durations = segment_video_durations(durations, gaps)
    for i, seg in enumerate(seg_results):
        seg["clip_duration_sec"] = clip_durations[i]

    digest_payload = [
        {
            "index": int(s["index"]),
            "text": str(s["text"]),
            "audio_url": str((segment_urls or {}).get(str(s["index"])) or ""),
        }
        for s in seg_results
    ]
    digest = (audio_config_digest or "").strip() or audio_preview_digest(
        digest_payload,
        gaps,
        speed=speed,
    )

    store.write_json(
        store.audio_config_path,
        {
            "revision_id": rid,
            "label": "standalone",
            "preview": True,
            "confirmed": True,
            "audio_config_digest": digest,
            "audio_confirmed_digest": digest,
            "segments": seg_results,
            "gaps_sec": gaps,
            "speed": speed,
            "segment_count": len(seg_results),
        },
    )
    store.refresh_flags()
    if not store.flags.audio_confirmed:
        raise bad_request(40004, "audio bootstrap failed")
    return rid


def bootstrap_visual_preview(
    store: WorkspaceStore,
    *,
    media_url: str,
    media_type: str,
    text: str,
    start_sec: float = 0.0,
    on_progress: Callable[[str, int, str], None] | None = None,
) -> None:
    """Download one media asset for visual preview (no audio / split required)."""
    from videoaudiotext.api.services.media import _ext_for_type

    url = str(media_url).strip()
    mtype = str(media_type).strip().lower()
    subtitle = str(text).strip()
    if not url or mtype not in ("image", "video"):
        raise bad_request(40001, "media_url and media_type (image|video) are required")
    if not subtitle:
        raise bad_request(40001, "text is required")

    store.ensure_tree()
    start = float(start_sec or 0.0)
    local_path: str
    fetch_mode = "full_download"

    if (
        mtype == "video"
        and preview_video_oss_snapshot_enabled()
        and oss_configured()
    ):
        object_key = resolve_oss_object_key(url)
        if object_key:
            if on_progress:
                on_progress("fetch", 10, "OSS 截帧…")
            raw = store.root / "uploads" / "media_raw_preview.jpg"
            try:
                download_video_snapshot_oss(object_key, raw, start_sec=start)
                store.source_media_dir.mkdir(parents=True, exist_ok=True)
                dest = store.source_media_dir / "1.jpg"
                shutil.copy2(raw, dest)
                raw.unlink(missing_ok=True)
                local_path = "source_media/1.jpg"
                fetch_mode = "oss_snapshot"
            except Exception:
                logger.exception(
                    "preview OSS snapshot failed for %s; fallback=%s",
                    object_key,
                    preview_video_oss_snapshot_fallback_enabled(),
                )
                if not preview_video_oss_snapshot_fallback_enabled():
                    raise

    if fetch_mode != "oss_snapshot":
        if on_progress:
            on_progress("fetch", 10, "拉取预览素材…")
        ext = _ext_for_type(mtype, url)
        raw = store.root / "uploads" / f"media_raw_preview{ext}"
        fetch_url_to_path(url, raw)
        store.source_media_dir.mkdir(parents=True, exist_ok=True)
        dest = store.source_media_dir / f"1{ext}"
        shutil.copy2(raw, dest)
        raw.unlink(missing_ok=True)
        local_path = f"source_media/1{ext}"

    media_payload: dict[str, Any] = {
        "source_url": url,
        "type": mtype,
        "local_path": local_path,
        "start_sec": start,
    }
    if fetch_mode == "oss_snapshot":
        media_payload["fetch_mode"] = fetch_mode

    store.write_json(
        store.media_binding_path,
        {
            "bound": True,
            "preview_only": True,
            "segments": [
                {
                    "index": 1,
                    "text": subtitle,
                    "media": media_payload,
                }
            ],
        },
    )
    store.flags.media_bound = True


def bootstrap_media_bound(
    store: WorkspaceStore,
    segments: Sequence[dict[str, Any]],
    *,
    revision_id: str,
) -> dict[str, Any]:
    """Download media URLs and write media_binding (sync, for preview/compose bootstrap)."""
    return media_service.run_media_bind(
        store,
        list(segments),
        revision_id=revision_id,
    )


def _compose_audio_params(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve flat or legacy nested ``audio`` block from a compose bootstrap payload."""
    nested = payload.get("audio") or {}
    segments = payload.get("segments") or []

    def _pick(key: str) -> str:
        return str(payload.get(key) or nested.get(key) or "").strip()

    timing_segments: list[dict[str, Any]] = []
    for item in segments:
        timing: dict[str, Any] = {
            "index": int(item["index"]),
            "text": str(item["text"]).strip(),
        }
        if item.get("duration_sec") is not None:
            timing["duration_sec"] = float(item["duration_sec"])
        if item.get("clip_duration_sec") is not None:
            timing["clip_duration_sec"] = float(item["clip_duration_sec"])
        timing_segments.append(timing)

    if not timing_segments:
        timing_segments = list(nested.get("segments") or [])

    return {
        "master_audio_url": _pick("master_audio_url"),
        "subtitle_srt_url": _pick("subtitle_srt_url"),
        "subtitle_ass_url": _pick("subtitle_ass_url") or None,
        "segment_urls": payload.get("segment_urls") or nested.get("segment_urls"),
        "speed": float(payload.get("speed") or nested.get("speed") or 1.0),
        "segment_items": timing_segments,
    }


def apply_standalone_pipeline(
    store: WorkspaceStore,
    payload: dict[str, Any],
    *,
    bind_media: bool,
) -> str:
    """Hydrate split + confirmed audio (+ optional media bind) from a standalone payload."""
    segments = payload.get("segments") or []
    if not segments:
        raise bad_request(40001, "segments is required")

    bind_segments = [
        {"index": int(s["index"]), "text": str(s["text"]), "media": dict(s.get("media") or {})}
        for s in segments
    ]
    bootstrap_split_plan(store, bind_segments)

    audio_params = _compose_audio_params(payload)
    if not audio_params["master_audio_url"]:
        raise bad_request(40001, "master_audio_url is required")
    if not audio_params["subtitle_srt_url"]:
        raise bad_request(40001, "subtitle_srt_url is required")
    if not audio_params["segment_items"]:
        raise bad_request(40001, "segments is required")

    revision_id = bootstrap_confirmed_audio(
        store,
        master_audio_url=audio_params["master_audio_url"],
        subtitle_srt_url=audio_params["subtitle_srt_url"],
        subtitle_ass_url=audio_params["subtitle_ass_url"],
        segment_items=audio_params["segment_items"],
        segment_urls=audio_params["segment_urls"],
        speed=audio_params["speed"],
    )

    if bind_media:
        bootstrap_media_bound(
            store,
            bind_segments,
            revision_id=revision_id,
        )
    maybe_write_render_style(store, payload)
    return revision_id


def _ext_for_bgm_url(url: str) -> str:
    path = str(url).split("?")[0].lower()
    for ext in (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"):
        if path.endswith(ext):
            return ext
    return ".mp3"


def _bootstrap_bgm(store: WorkspaceStore, bgm: dict[str, Any]) -> dict[str, Any]:
    url = str(bgm.get("url") or "").strip()
    if not url:
        raise bad_request(40001, "bgm.url is required")
    store.ensure_tree()
    ext = _ext_for_bgm_url(url)
    local = store.root / "uploads" / f"bgm_raw{ext}"
    fetch_url_to_path(url, local)
    if not local.is_file():
        raise bad_request(40004, "bgm fetch failed")
    return {
        "source_url": url,
        "local_path": f"uploads/bgm_raw{ext}",
        "bgm_volume": max(0.0, float(bgm.get("bgm_volume", 0.15))),
    }


def maybe_write_render_style(store: WorkspaceStore, payload: dict[str, Any]) -> None:
    resolution = payload.get("resolution")
    subtitle_style = payload.get("subtitle_style")
    bgm = payload.get("bgm")
    voice_volume = payload.get("voice_volume")
    if not resolution and not subtitle_style and not bgm and voice_volume is None:
        return
    from videoaudiotext.api.services.visual import _resolve_resolution
    from videoaudiotext.api.subtitle_style import clamp_y_offset_steps

    store.ensure_tree()
    data: dict[str, Any] = dict(store.read_json(store.render_style_path) or {})

    if resolution or subtitle_style or "resolution" not in data:
        out_w, out_h = _resolve_resolution(resolution)
        sty = subtitle_style or data.get("subtitle_style") or {}
        font_name_input = str(sty.get("font_name") or "思源黑体")
        data["resolution"] = {"width": out_w, "height": out_h}
        data["subtitle_style"] = {
            "font_name": font_name_input,
            "font_name_input": font_name_input,
            "font_scale": float(sty.get("font_scale") or 1.0),
            "y_offset": clamp_y_offset_steps(int(sty.get("y_offset") or 0)),
            "y_offset_unit": "steps",
        }

    if voice_volume is not None:
        data["audio"] = {"voice_volume": max(0.0, float(voice_volume))}

    if bgm:
        data["bgm"] = _bootstrap_bgm(store, bgm)

    store.write_json(store.render_style_path, data)
    store.refresh_flags()


def prepare_standalone_job(kind: str) -> WorkspaceStore:
    return create_job_store(kind=kind)
