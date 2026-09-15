"""Output resolution, encoding, and canvas settings."""

from __future__ import annotations

import os

OUTPUT_WIDTH = int(os.environ.get("OUTPUT_WIDTH", "1080"))
OUTPUT_HEIGHT = int(os.environ.get("OUTPUT_HEIGHT", "1920"))
OUTPUT_RESOLUTION_MODE = os.environ.get("OUTPUT_RESOLUTION_MODE", "fixed").strip().lower()
if OUTPUT_RESOLUTION_MODE not in ("fixed", "source", "source_largest"):
    OUTPUT_RESOLUTION_MODE = "fixed"
_ACTIVE_OUTPUT_WIDTH = OUTPUT_WIDTH
_ACTIVE_OUTPUT_HEIGHT = OUTPUT_HEIGHT
_SUBTITLE_REF_HEIGHT = 1920
_SUBTITLE_REF_WIDTH = 1080
VIDEO_FPS = 25
CLIP_BUILD_WORKERS = max(
    1,
    min(8, int(os.environ.get("CLIP_BUILD_WORKERS", str(min(4, (os.cpu_count() or 2)))))),
)
SUBTITLE_X264_PRESET = os.environ.get("SUBTITLE_X264_PRESET", "medium").strip() or "medium"
SUBTITLE_X264_CRF = os.environ.get("SUBTITLE_X264_CRF", "23").strip() or "23"
# 步骤 3 clip / 步骤 5 xfade 中间编码（非合并硬烧路径）
CLIP_X264_PRESET = os.environ.get("CLIP_X264_PRESET", "veryfast").strip() or "veryfast"
CLIP_X264_CRF = os.environ.get("CLIP_X264_CRF", "23").strip() or "23"
MERGE_XFADE_SUBTITLE = os.environ.get("MERGE_XFADE_SUBTITLE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
# 成片默认：素材不够长时用 stream_loop 循环，不用 tpad 尾帧定格
CLIP_AVOID_STREAM_LOOP = False
AUDIO_CODEC = "aac"
STRICT_CANVAS_ASSERTION = os.environ.get("STRICT_CANVAS_ASSERTION", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
CLIP_EDGE_FADE_SEC = float(os.environ.get("CLIP_EDGE_FADE_SEC", "0"))
CLIP_XFADE_SEC = float(os.environ.get("CLIP_XFADE_SEC", "0.3"))
XFADE_MAX_RATIO = float(os.environ.get("XFADE_MAX_RATIO", "0.8"))
# 成片画面与口播/字幕硬切对齐（关闭则保留段间 xfade，换句时可能仍见上一段画面）
VIDEO_SYNC_HARD_CUT = os.environ.get("VIDEO_SYNC_HARD_CUT", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def set_active_output_dimensions(width: int, height: int) -> None:
    global _ACTIVE_OUTPUT_WIDTH, _ACTIVE_OUTPUT_HEIGHT
    w, h = int(width), int(height)
    if w % 2:
        w -= 1
    if h % 2:
        h -= 1
    _ACTIVE_OUTPUT_WIDTH = max(2, w)
    _ACTIVE_OUTPUT_HEIGHT = max(2, h)


def get_output_width() -> int:
    return _ACTIVE_OUTPUT_WIDTH


def get_output_height() -> int:
    return _ACTIVE_OUTPUT_HEIGHT


def strict_canvas_assertion_enabled() -> bool:
    return STRICT_CANVAS_ASSERTION
