"""有效画面区域：素材等比缩放进画布后的实际显示矩形（去黑边）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

from videoaudiotext.config import (
    SUBTITLE_CUE_MAX_CHARS,
    SUBTITLE_UNIFIED_CANVAS_Y,
    get_output_height,
    get_output_width,
    scaled_subtitle_frame_lr_margin,
    scaled_subtitle_margin_bottom,
    subtitle_max_width_ratio,
)
from videoaudiotext.media.dimensions import probe_media_dimensions

# 句尾情绪标点不参与有效画面/字幕布局宽度估算（避免字形纵向拉伸干扰）
_EMOTION_LAYOUT_PUNCT = "！～!?…"


@dataclass(frozen=True)
class ContentRect:
    """画布坐标系下有效画面区（与 FFmpeg scale+decrease+pad 一致）。"""

    ox: int
    oy: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.ox + self.width

    @property
    def bottom(self) -> int:
        return self.oy + self.height


def strip_emotion_punct_for_layout(text: str) -> str:
    """布局计算时剔除句尾情绪符号，避免爆框估算偏高。"""
    t = text.strip()
    while t and t[-1] in _EMOTION_LAYOUT_PUNCT:
        t = t[:-1]
    return t


def fitted_content_rect(
    src_w: int,
    src_h: int,
    canvas_w: int,
    canvas_h: int,
) -> ContentRect:
    """与 media_clip._scale_filter 相同：decrease 缩放 + 居中 pad。"""
    if src_w <= 0 or src_h <= 0:
        return ContentRect(0, 0, canvas_w, canvas_h)

    scale = min(canvas_w / src_w, canvas_h / src_h)
    cw = max(2, int(round(src_w * scale)))
    ch = max(2, int(round(src_h * scale)))
    if cw % 2:
        cw -= 1
    if ch % 2:
        ch -= 1
    ox = max(0, (canvas_w - cw) // 2)
    oy = max(0, (canvas_h - ch) // 2)
    return ContentRect(ox, oy, cw, ch)


def content_rect_for_media(
    media_path: Path,
    *,
    canvas_w: int | None = None,
    canvas_h: int | None = None,
) -> ContentRect:
    cw = canvas_w if canvas_w is not None else get_output_width()
    ch = canvas_h if canvas_h is not None else get_output_height()
    sw, sh = probe_media_dimensions(media_path)
    from videoaudiotext.media.fill import needs_blur_background

    if needs_blur_background(sw, sh):
        return ContentRect(0, 0, cw, ch)
    return fitted_content_rect(sw, sh, cw, ch)


def content_rects_for_media_paths(
    media_paths: Sequence[Path],
    *,
    canvas_w: int | None = None,
    canvas_h: int | None = None,
) -> List[ContentRect]:
    return [content_rect_for_media(p, canvas_w=canvas_w, canvas_h=canvas_h) for p in media_paths]


def is_landscape_canvas(
    *,
    canvas_w: int | None = None,
    canvas_h: int | None = None,
) -> bool:
    """成片画布是否为横屏（宽 > 高）。"""
    frame_w = canvas_w if canvas_w is not None else get_output_width()
    frame_h = canvas_h if canvas_h is not None else get_output_height()
    return frame_w > frame_h


def is_square_canvas(
    *,
    canvas_w: int | None = None,
    canvas_h: int | None = None,
) -> bool:
    frame_w = canvas_w if canvas_w is not None else get_output_width()
    frame_h = canvas_h if canvas_h is not None else get_output_height()
    return frame_w == frame_h


def _pillarboxed_in_landscape(
    rect: ContentRect,
    *,
    canvas_w: int | None = None,
) -> bool:
    """竖屏素材落在横屏画布中央（左右黑边），有效区呈窄条。"""
    frame_w = canvas_w if canvas_w is not None else get_output_width()
    if not is_landscape_canvas(canvas_w=frame_w, canvas_h=get_output_height()):
        return False
    return rect.width < int(frame_w * 0.85) and rect.height >= rect.width


def max_chars_for_content_width(
    content_width: int,
    *,
    content_height: int | None = None,
) -> int:
    """按有效画面宽度缩放单行字数；全幅画面用 margin 内像素估算，不再重复乘 width_ratio。"""
    from videoaudiotext.config import (
        get_output_height,
        get_output_width,
        scaled_subtitle_cue_max_chars,
        scaled_subtitle_landscape_max_chars,
        scaled_subtitle_renderable_width_px,
        scaled_subtitle_square_max_chars,
    )

    frame_w = get_output_width()
    frame_h = get_output_height()
    base = scaled_subtitle_cue_max_chars()
    renderable_w = scaled_subtitle_renderable_width_px()

    if is_landscape_canvas(canvas_w=frame_w, canvas_h=frame_h):
        return min(
            scaled_subtitle_cue_max_chars(),
            scaled_subtitle_landscape_max_chars(frame_w),
        )
    if is_square_canvas(canvas_w=frame_w, canvas_h=frame_h):
        return min(
            scaled_subtitle_cue_max_chars(),
            scaled_subtitle_square_max_chars(frame_w),
        )

    if content_width >= frame_w - 2:
        if content_height is not None and content_width >= content_height:
            return max(base, scaled_subtitle_landscape_max_chars(content_width))
        return base

    ratio = min(1.0, content_width / max(1, renderable_w))
    return max(6, int(round(base * ratio)))


def content_prefers_single_line(rect: ContentRect) -> bool:
    """横屏/方屏有效区能不折就不折；竖屏按有效区宽高比。"""
    if is_landscape_canvas() or is_square_canvas():
        return True
    return rect.width >= rect.height


def subtitle_layout_for_content(rect: ContentRect) -> tuple[int, bool]:
    """返回 (单行字数上限, 是否优先单行)。"""
    return (
        max_chars_for_content_width(rect.width, content_height=rect.height),
        content_prefers_single_line(rect),
    )


def segment_time_bounds(segment_video_durations: Sequence[float]) -> List[Tuple[float, float]]:
    bounds: List[Tuple[float, float]] = []
    t = 0.0
    for dur in segment_video_durations:
        d = max(0.01, float(dur))
        bounds.append((t, t + d))
        t += d
    return bounds


def segment_time_bounds_for_output(
    clip_durations: Sequence[float],
    *,
    xfade_sec: float | None = None,
) -> List[Tuple[float, float]]:
    """
    成片时间轴上的 Segment 边界。
    clip_durations 为各 clip/N.mp4 实测时长（含 xfade 尾延长）；xfade 时相邻段重叠 xfade_sec。
    """
    from videoaudiotext.config import CLIP_XFADE_SEC

    xs = CLIP_XFADE_SEC if xfade_sec is None else float(xfade_sec)
    if xs <= 0.001 or len(clip_durations) <= 1:
        return segment_time_bounds(clip_durations)

    bounds: List[Tuple[float, float]] = []
    t = 0.0
    n = len(clip_durations)
    for i, raw in enumerate(clip_durations):
        d = max(0.01, float(raw))
        span = d - xs if i < n - 1 else d
        bounds.append((t, t + span))
        t += span
    return bounds


def segment_index_at_time(
    t: float,
    bounds: Sequence[Tuple[float, float]],
) -> int:
    for i, (start, end) in enumerate(bounds):
        if start <= t < end - 1e-6:
            return i
    return max(0, len(bounds) - 1)


def segment_index_for_cue(
    start: float,
    end: float,
    bounds: Sequence[Tuple[float, float]],
) -> int:
    """按与片段时间重叠最多的段选素材布局（避免跨段字幕误用上一段横竖屏）。"""
    if not bounds:
        return 0
    best_i = 0
    best_overlap = -1.0
    cue_start = float(start)
    cue_end = max(cue_start + 1e-6, float(end))
    for i, (seg_start, seg_end) in enumerate(bounds):
        overlap = max(0.0, min(cue_end, seg_end) - max(cue_start, seg_start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_i = i
    return best_i


def ass_margin_override_prefix(
    rect: ContentRect,
    *,
    canvas_w: int | None = None,
    canvas_h: int | None = None,
    inner_lr: int | None = None,
    inner_bottom: int | None = None,
) -> str:
    """
    ASS 对话行前缀：将底中字幕限制在有效画面内。
    字号由 Style 统一控制；此处只设 \\margl / \\margr / \\margv，避免各段素材字号跳变。
    """
    frame_w = canvas_w if canvas_w is not None else get_output_width()
    frame_h = canvas_h if canvas_h is not None else get_output_height()
    pillarbox = _pillarboxed_in_landscape(rect, canvas_w=frame_w)
    from videoaudiotext.subtitle.wrap_reveal import wrap_reveal_enabled

    if (
        wrap_reveal_enabled()
        and not pillarbox
        and rect.width >= frame_w - 2
    ):
        from videoaudiotext.config.subtitle import scaled_subtitle_progressive_frame_lr_margin

        side = scaled_subtitle_progressive_frame_lr_margin()
    else:
        side = scaled_subtitle_frame_lr_margin()
    pad_lr = inner_lr if inner_lr is not None else max(8, side // 4)
    pad_b = inner_bottom if inner_bottom is not None else (
        max(
            scaled_subtitle_margin_bottom(),
            int(rect.height * 0.12),
        )
        + max(32, int(frame_h * 0.022))
    )
    canvas_pad_b = (
        max(scaled_subtitle_margin_bottom(), int(frame_h * 0.16))
        + max(32, int(frame_h * 0.022))
    )
    cx = frame_w // 2
    landscape_out = is_landscape_canvas(canvas_w=frame_w, canvas_h=frame_h)
    square_out = is_square_canvas(canvas_w=frame_w, canvas_h=frame_h)

    if landscape_out:
        if pillarbox:
            margin_l = max(side, rect.ox + pad_lr)
            margin_r = max(side, frame_w - rect.right + pad_lr)
            cy = rect.oy + rect.height - pad_b
        else:
            margin_l = margin_r = side
            cy = frame_h - max(
                scaled_subtitle_margin_bottom(),
                int(frame_h * 0.08),
            )
    elif square_out:
        margin_l = margin_r = side
        cy = frame_h - max(
            scaled_subtitle_margin_bottom(),
            int(frame_h * 0.12),
        )
    else:
        margin_l = max(side, rect.ox + pad_lr)
        margin_r = max(side, frame_w - rect.right + pad_lr)
        if SUBTITLE_UNIFIED_CANVAS_Y and not pillarbox:
            cy = frame_h - canvas_pad_b
        else:
            cy = rect.oy + rect.height - pad_b

    from videoaudiotext.config.subtitle import apply_subtitle_y_to_cy

    cy = apply_subtitle_y_to_cy(cy)

    return (
        f"{{\\an2\\q2\\pos({cx},{cy})\\margl{margin_l}\\margr{margin_r}}}"
    )
