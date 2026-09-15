"""Stage 3: bind media URLs to segments."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, Sequence

from videoaudiotext.api.errors import bad_request
from videoaudiotext.api.services.audio import ensure_audio_confirmed_for_bind
from videoaudiotext.api.services import audio_revisions as rev
from videoaudiotext.api.url_fetch import fetch_url_to_path
from videoaudiotext.api.workspace.digests import media_binding_digest
from videoaudiotext.api.workspace.store import WorkspaceStore

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi"}


def _ext_for_type(type_: str, url: str) -> str:
    t = (type_ or "").strip().lower()
    if t == "image":
        return ".jpg"
    if t == "video":
        return ".mp4"
    path = url.split("?")[0].lower()
    for ext in _IMAGE_EXT | _VIDEO_EXT:
        if path.endswith(ext):
            return ext
    return ".mp4"


def cleanup_media_bind_uploads(store: WorkspaceStore) -> int:
    """Remove in-flight media/bind raw upload temp files."""
    uploads = store.root / "uploads"
    removed = 0
    if not uploads.is_dir():
        return removed
    for p in uploads.glob("media_raw_*"):
        p.unlink(missing_ok=True)
        removed += 1
    return removed


def run_media_bind(
    store: WorkspaceStore,
    segments_input: Sequence[dict[str, Any]],
    *,
    revision_id: str,
    cancel_check: Callable[[], None] | None = None,
    on_progress: Callable[[str, int, str], None] | None = None,
) -> dict[str, Any]:
    if not (revision_id or "").strip():
        raise bad_request(40001, "revision_id is required")

    plan = store.read_json(store.split_plan_path)
    if not plan:
        raise bad_request(40004, "split_not_ready")

    ensure_audio_confirmed_for_bind(
        store,
        revision_id.strip(),
    )
    audio_meta = store.read_json(store.audio_config_path) or {}

    expected = len(plan.get("segments") or [])
    if len(segments_input) != expected:
        raise bad_request(
            40001,
            "segments count mismatch with split_plan",
            expected_segment_count=expected,
            received=len(segments_input),
        )

    plan_by_index = {int(s["index"]): s for s in plan.get("segments") or []}
    audio_by_index = {int(s["index"]): s for s in audio_meta.get("segments") or []}

    bound_segments: list[dict[str, Any]] = []
    binding_payload: list[dict[str, Any]] = []
    total = len(segments_input)

    try:
        for seg_i, item in enumerate(segments_input, start=1):
            if cancel_check:
                cancel_check()
            idx = int(item["index"])
            text = str(item["text"]).strip()
            plan_seg = plan_by_index.get(idx)
            if not plan_seg or plan_seg.get("text") != text:
                raise bad_request(40001, f"segment text mismatch at index {idx}")
            media = item.get("media") or {}
            url = str(media.get("url") or "").strip()
            mtype = str(media.get("type") or "").strip().lower()
            if not url or mtype not in ("image", "video"):
                raise bad_request(40001, f"invalid media at index {idx}")
            start_sec = float(media.get("start_sec") or 0.0)
            if on_progress:
                pct = min(90, 10 + int(80 * seg_i / max(total, 1)))
                on_progress(
                    "fetch",
                    pct,
                    f"拉取素材 {seg_i}/{total}（index={idx}）…",
                )

            ext = _ext_for_type(mtype, url)
            raw = store.root / "uploads" / f"media_raw_{idx}{ext}"
            fetch_url_to_path(url, raw)
            dest = store.source_media_dir / f"{idx}{ext}"
            shutil.copy2(raw, dest)

            audio_seg = audio_by_index.get(idx) or {}
            bound = {
                "index": idx,
                "text": text,
                "search_query": plan_seg.get("search_query") or "",
                "duration_sec": audio_seg.get("duration_sec"),
                "clip_duration_sec": audio_seg.get("clip_duration_sec"),
                "media": {
                    "source_url": url,
                    "type": mtype,
                    "local_path": f"source_media/{idx}{ext}",
                    "start_sec": start_sec,
                },
            }
            bound_segments.append(bound)
            binding_payload.append(
                {
                    "index": idx,
                    "text": text,
                    "media": {
                        "url": url,
                        "type": mtype,
                        "start_sec": start_sec,
                    },
                }
            )
    except Exception:
        cleanup_media_bind_uploads(store)
        raise

    if cancel_check:
        cancel_check()
    if on_progress:
        on_progress("finalize", 95, "写入绑定配置…")

    digest = media_binding_digest(binding_payload)
    stored_digest = audio_meta.get("audio_confirmed_digest") or audio_meta.get(
        "audio_config_digest"
    )
    store.write_json(
        store.media_binding_path,
        {
            "bound": True,
            "revision_id": revision_id.strip(),
            "audio_config_digest": stored_digest or None,
            "media_binding_digest": digest,
            "segments": bound_segments,
        },
    )

    clip_offsets: dict[str, float] = {}
    for seg in bound_segments:
        media = seg.get("media") or {}
        if media.get("type") == "video":
            start = float(media.get("start_sec") or 0.0)
            if start > 0:
                clip_offsets[str(seg["index"])] = start
    offsets_path = store.source_media_dir / "clip_offsets.json"
    if clip_offsets:
        offsets_path.write_text(
            json.dumps(clip_offsets, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif offsets_path.is_file():
        offsets_path.unlink()

    store.flags.media_bound = True
    store.flags.visual_preview_ready = False
    store.flags.compose_ready = False

    return {
        "bound": True,
        "revision_id": revision_id.strip(),
        "segment_count": len(bound_segments),
        "segments": bound_segments,
        "media_binding_digest": digest,
    }
