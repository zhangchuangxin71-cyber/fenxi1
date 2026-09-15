"""Stage 4: subtitle style preview."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from videoaudiotext.api.errors import bad_request
from videoaudiotext.api.workspace.context import workspace_paths
from videoaudiotext.api.workspace.digests import render_style_digest
from videoaudiotext.api.workspace.store import WorkspaceStore
from videoaudiotext.core.compose import export_preview_cover_frame
from videoaudiotext.api.subtitle_style import (
    clamp_y_offset_steps,
    subtitle_y_offset_step,
    subtitle_y_offset_steps_label,
    y_offset_steps_to_pixels,
)
from videoaudiotext.config import (
    scaled_subtitle_ass_font_size,
    get_subtitle_font_name,
    set_active_output_dimensions,
    set_active_subtitle_font_name,
    set_active_subtitle_font_scale,
    set_active_subtitle_y_offset,
)
from videoaudiotext.media.dimensions import ensure_even


def _resolve_resolution(resolution: dict[str, Any] | None) -> tuple[int, int]:
    res = resolution or {}
    width = int(res.get("width") or 1080)
    height = int(res.get("height") or 1920)
    return ensure_even(width, height)


def run_visual_preview(
    store: WorkspaceStore,
    resolution: dict[str, Any],
    subtitle_style: dict[str, Any],
    *,
    cancel_check: Callable[[], None] | None = None,
    on_progress: Callable[[str, int, str], None] | None = None,
) -> dict[str, Any]:
    if cancel_check:
        cancel_check()

    binding = store.read_json(store.media_binding_path) or {}
    segments = binding.get("segments") or []
    seg = next((s for s in segments if int(s["index"]) == 1), None)
    if not seg:
        raise bad_request(40004, "preview media not ready")

    out_w, out_h = _resolve_resolution(resolution)
    font_name_input = str(subtitle_style.get("font_name") or "思源黑体")
    font_scale = float(subtitle_style.get("font_scale") or 1.0)
    y_offset_steps = clamp_y_offset_steps(int(subtitle_style.get("y_offset") or 0))
    y_offset_px = y_offset_steps_to_pixels(y_offset_steps)

    media = seg.get("media") or {}
    media_path = store.root / media.get("local_path", "")
    if not media_path.is_file():
        raise bad_request(40004, "media file missing")

    preview_path = store.previews_dir / "seg_1.jpg"
    start_sec = float(media.get("start_sec") or 0.0)
    if str(media.get("fetch_mode") or "") == "oss_snapshot":
        start_sec = 0.0
    mtype = media.get("type") or "image"
    text = str(seg.get("text") or "")

    with workspace_paths(store):
        set_active_output_dimensions(out_w, out_h)
        set_active_subtitle_font_scale(font_scale)
        set_active_subtitle_font_name(font_name_input)
        set_active_subtitle_y_offset(y_offset_px)

        if on_progress:
            on_progress("preview", 60, "渲染预览图…")
        if cancel_check:
            cancel_check()
        export_preview_cover_frame(
            media_path,
            text,
            preview_path,
            start_sec=start_sec,
            ass_path=None,
            preview_segment_index=1,
            segment_bounds=None,
        )

    font_name = get_subtitle_font_name()

    style_payload = {
        "resolution": {
            "width": out_w,
            "height": out_h,
        },
        "subtitle_style": {
            "font_name": font_name,
            "font_name_input": font_name_input,
            "font_scale": font_scale,
            "y_offset": y_offset_steps,
            "y_offset_unit": "steps",
        },
    }
    store.write_json(store.render_style_path, style_payload)
    store.flags.visual_preview_ready = True
    digest = render_style_digest(style_payload)

    return {
        "render_style_digest": digest,
        "resolution": {"width": out_w, "height": out_h},
        "subtitle_style_applied": {
            "font_name": font_name,
            "font_name_input": font_name_input,
            "font_size_px": scaled_subtitle_ass_font_size(),
            "font_scale": font_scale,
            "y_offset": y_offset_steps,
            "y_offset_px": y_offset_px,
            "y_offset_step_px": subtitle_y_offset_step(),
            "y_offset_label": subtitle_y_offset_steps_label(y_offset_steps),
        },
        "preview": {
            "text": text,
            "media_type": mtype,
        },
    }
