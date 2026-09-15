"""Detect source media size and configure active output resolution."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable, List, Tuple

from videoaudiotext.config import OUTPUT_HEIGHT, OUTPUT_RESOLUTION_MODE, OUTPUT_WIDTH, set_active_output_dimensions

# Gradio 下拉：(显示名, mode 值)
RESOLUTION_PRESET_CHOICES: List[Tuple[str, str]] = [
    ("竖屏 1080P · 1080×1920（抖音/视频号）", "1080x1920"),
    ("竖屏 720P · 720×1280", "720x1280"),
    ("竖屏 4K · 2160×3840", "2160x3840"),
    ("横屏 1080P · 1920×1080（B站/横版）", "1920x1080"),
    ("横屏 720P · 1280×720", "1280x720"),
    ("横屏 4K · 3840×2160", "3840x2160"),
    ("方形 1080 · 1080×1080（小红书）", "1080x1080"),
    ("方形 720 · 720×720", "720x720"),
    ("第 1 段素材原始分辨率", "source"),
    ("各段素材最高清分辨率", "source_largest"),
    ("自定义宽高", "custom"),
]

_DEFAULT_PRESET = "1080x1920"
_WXH_RE = re.compile(r"^(\d{2,5})\s*[xX×]\s*(\d{2,5})$")


def ensure_even(width: int, height: int) -> Tuple[int, int]:
    w = max(2, int(width))
    h = max(2, int(height))
    if w % 2:
        w -= 1
    if h % 2:
        h -= 1
    return w, h


def parse_resolution_spec(spec: str) -> Tuple[int, int] | None:
    """解析 1080x1920 / 1920×1080 等显式宽高。"""
    m = _WXH_RE.match((spec or "").strip())
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w < 64 or h < 64 or w > 7680 or h > 7680:
        return None
    return ensure_even(w, h)


def normalize_resolution_mode(mode: str | None) -> str:
    m = (mode or _DEFAULT_PRESET).strip().lower()
    if m == "fixed":
        return _DEFAULT_PRESET
    return m


def resolution_mode_description(mode: str | None) -> str:
    m = normalize_resolution_mode(mode)
    for label, value in RESOLUTION_PRESET_CHOICES:
        if value == m:
            return label.split(" · ", 1)[0]
    parsed = parse_resolution_spec(m)
    if parsed:
        return f"自定义 {parsed[0]}×{parsed[1]}"
    return m


_PROBE_DIM_CACHE: dict[str, tuple[int, int, float]] = {}


def probe_media_dimensions(path: Path) -> Tuple[int, int]:
    """ffprobe 读取图片/视频宽高的首条视频流（或图片解码尺寸）。"""
    if not path.is_file():
        return 0, 0
    key = str(path.resolve())
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _PROBE_DIM_CACHE.get(key)
    if cached and cached[2] == mtime:
        return cached[0], cached[1]
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    try:
        raw = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        streams = json.loads(raw).get("streams") or []
        if not streams:
            return 0, 0
        w = int(streams[0].get("width") or 0)
        h = int(streams[0].get("height") or 0)
        w, h = ensure_even(w, h)
        _PROBE_DIM_CACHE[key] = (w, h, mtime)
        return w, h
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError, ValueError):
        return 0, 0


def _resolve_from_media_first(media_paths: List[Path]) -> Tuple[int, int]:
    for path in media_paths:
        w, h = probe_media_dimensions(path)
        if w > 0 and h > 0:
            return w, h
    return ensure_even(OUTPUT_WIDTH, OUTPUT_HEIGHT)


def _resolve_from_media_largest(media_paths: List[Path]) -> Tuple[int, int]:
    best: Tuple[int, int] | None = None
    best_area = 0
    for path in media_paths:
        w, h = probe_media_dimensions(path)
        if w <= 0 or h <= 0:
            continue
        area = w * h
        if area > best_area:
            best_area = area
            best = (w, h)
    if best:
        return best
    return ensure_even(OUTPUT_WIDTH, OUTPUT_HEIGHT)


def resolve_output_dimensions(
    media_paths: List[Path] | None = None,
    *,
    mode: str | None = None,
    custom_width: int | float | None = None,
    custom_height: int | float | None = None,
) -> Tuple[int, int]:
    """
    解析成片分辨率：
    - 1080x1920 / 1920x1080 等预设字符串
    - source / source_largest
    - custom + custom_width/height
    - fixed（兼容旧值 → 1080x1920）
    """
    m = normalize_resolution_mode(mode or OUTPUT_RESOLUTION_MODE)

    if m == "custom":
        cw = int(custom_width or OUTPUT_WIDTH)
        ch = int(custom_height or OUTPUT_HEIGHT)
        return ensure_even(cw, ch)

    if m == "source":
        return _resolve_from_media_first(media_paths or [])

    if m == "source_largest":
        return _resolve_from_media_largest(media_paths or [])

    parsed = parse_resolution_spec(m)
    if parsed:
        return parsed

    return ensure_even(OUTPUT_WIDTH, OUTPUT_HEIGHT)


def resolve_dimensions_from_media(
    media_paths: List[Path],
    *,
    mode: str | None = None,
    custom_width: int | float | None = None,
    custom_height: int | float | None = None,
) -> Tuple[int, int]:
    """兼容旧接口。"""
    return resolve_output_dimensions(
        media_paths,
        mode=mode,
        custom_width=custom_width,
        custom_height=custom_height,
    )


def configure_output_from_media(
    media_paths: List[Path],
    *,
    mode: str | None = None,
    custom_width: int | float | None = None,
    custom_height: int | float | None = None,
    log: Callable[[str], None] | None = None,
) -> Tuple[int, int]:
    """写入全局成片宽高，供裁切、拼接、字幕烧录共用。"""
    m = normalize_resolution_mode(mode)
    w, h = resolve_output_dimensions(
        media_paths,
        mode=m,
        custom_width=custom_width,
        custom_height=custom_height,
    )
    set_active_output_dimensions(w, h)
    if log:
        log(f"  成片分辨率：{w}×{h}（{resolution_mode_description(m)}）")
    if m in ("source", "source_largest") and media_paths:
        target = (w, h)
        for i, path in enumerate(media_paths, start=1):
            pw, ph = probe_media_dimensions(path)
            if pw > 0 and ph > 0 and (pw, ph) != target:
                msg = (
                    f"  提示：第{i}段素材为 {pw}×{ph}，与成片 {w}×{h} 不一致，"
                    "将等比缩放并居中填充"
                )
                if log:
                    log(msg)
                else:
                    print(msg, flush=True)
    return w, h
