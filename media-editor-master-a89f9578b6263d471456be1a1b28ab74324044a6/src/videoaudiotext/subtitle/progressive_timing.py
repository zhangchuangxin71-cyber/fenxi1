"""Time-budget helpers for progressive (wrap-reveal) subtitle cues."""

from __future__ import annotations

from videoaudiotext.config import SUBTITLE_CPS_MAX, SUBTITLE_CUE_SPLIT_GAP_SEC
from videoaudiotext.subtitle.types import Cue

# 渐进单行最短展示（与 wrap_reveal 一致，快口播会自适应缩短）
_WRAP_REVEAL_MIN_LINE_SEC = 0.45


def progressive_time_budget_enabled() -> bool:
    import os

    return os.environ.get("SUBTITLE_PROGRESSIVE_TIME_BUDGET", "1").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def min_reading_duration(text: str, *, max_cps: float | None = None) -> float:
    """单条 cue 满足阅读 CPS 上限所需的最短时长。"""
    from videoaudiotext.subtitle.display import _core_char_len

    cap = SUBTITLE_CPS_MAX if max_cps is None else float(max_cps)
    chars = max(1, _core_char_len(text.replace("\n", "")))
    by_cps = chars / max(0.1, cap)
    by_floor = _WRAP_REVEAL_MIN_LINE_SEC if chars <= 3 else max(0.28, _WRAP_REVEAL_MIN_LINE_SEC * 0.75)
    return max(0.12, by_cps, by_floor)


def max_progressive_lines_for_duration(
    speech_dur: float,
    *,
    max_cps: float | None = None,
) -> int:
    """在给定口播时长内，保证最短阅读时长时最多能切多少条 progressive cue。"""
    dur = max(0.05, float(speech_dur))
    gap = max(0.001, SUBTITLE_CUE_SPLIT_GAP_SEC)
    min_line = min_reading_duration("测", max_cps=max_cps)
    if dur <= min_line:
        return 1
    return max(1, int((dur + gap) // (min_line + gap)))


def pack_screen_lines_for_time_budget(
    text: str,
    pixel_pack: int,
    speech_dur: float,
    *,
    content_rect=None,
    max_cps: float | None = None,
) -> list[str]:
    """
    在像素装箱上限基础上，若时间预算不允许切太碎，则放宽每行字数（仍尽量满足硬宽）。
    只影响屏显行数，不改动 TTS 切换时刻。
    """
    from videoaudiotext.config.subtitle import progressive_line_fits_hard_width
    from videoaudiotext.subtitle.display import _core_char_len, _normalize_subtitle_text
    from videoaudiotext.subtitle.quotes import pack_progressive_screen_lines

    plain = _normalize_subtitle_text(text.replace("\n", ""))
    if not plain:
        return []

    pack = max(1, int(pixel_pack))
    lines = pack_progressive_screen_lines(plain, pack, content_rect=content_rect)
    if not progressive_time_budget_enabled() or speech_dur <= 0.05:
        return lines

    max_lines = max_progressive_lines_for_duration(speech_dur, max_cps=max_cps)
    if len(lines) <= max_lines:
        return lines

    total = _core_char_len(plain)
    limit = pack
    best = lines
    while limit < total:
        limit += 1
        candidate = pack_progressive_screen_lines(plain, limit, content_rect=content_rect)
        if len(candidate) >= len(best):
            continue
        if not all(
            progressive_line_fits_hard_width(ln, content_rect=content_rect)
            for ln in candidate
        ):
            continue
        best = candidate
        if len(best) <= max_lines:
            return best

    return best


def floor_degenerate_cue_durations(
    cues: list[Cue],
    *,
    span_end: float | None = None,
    min_dur: float = 0.08,
) -> list[Cue]:
    """
    仅消除近零时长 cue，**保持 start 不动**（TTS / wav 对齐的切换点）。
    """
    if not cues or not progressive_time_budget_enabled():
        return cues

    gap = max(0.001, SUBTITLE_CUE_SPLIT_GAP_SEC)
    end_cap = float(span_end) if span_end is not None else float(cues[-1][2])
    out: list[Cue] = []

    for i, (text, start, end) in enumerate(cues):
        start = float(start)
        end = float(end)
        dur = end - start
        if dur >= min_dur:
            out.append((text, start, end))
            continue
        next_start = float(cues[i + 1][1]) if i + 1 < len(cues) else end_cap
        cap = (next_start - gap) if i + 1 < len(cues) else end_cap
        end = min(cap, start + min_dur)
        if end <= start:
            end = min(cap, start + 0.05)
        out.append((text, start, end))

    if out:
        out[-1] = (out[-1][0], out[-1][1], end_cap)
    return out
