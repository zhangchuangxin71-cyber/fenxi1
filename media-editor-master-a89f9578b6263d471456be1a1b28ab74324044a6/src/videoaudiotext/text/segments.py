"""Merge short atomic splits into pipeline segments (TTS / media / SRT)."""

from __future__ import annotations

from typing import List, Optional

from videoaudiotext.text.segment_meta import SegmentVisualMeta
from videoaudiotext.config import (
    CHARS_PER_SEC_ESTIMATE,
    MAX_SEGMENT_CHARS,
    MAX_SEGMENT_SEC,
    MIN_SEGMENT_SEC,
)
from videoaudiotext.text.split import split_sentence
from videoaudiotext.tts.voice_timing import chars_per_sec_for_voice


def _segment_char_count(text: str) -> int:
    return len(text.strip())


def estimate_duration(
    text: str,
    *,
    voice: str | None = None,
    rate: str = "+0%",
    chars_per_sec: float | None = None,
) -> float:
    n = _segment_char_count(text)
    if n == 0:
        return 0.0
    cps = chars_per_sec
    if cps is None:
        cps = (
            chars_per_sec_for_voice(voice, rate)
            if voice
            else CHARS_PER_SEC_ESTIMATE
        )
    return max(0.5, n / cps)


def merge_short_segments(
    sentences: List[str],
    durations: List[float],
    *,
    min_sec: float = MIN_SEGMENT_SEC,
    max_chars: int = MAX_SEGMENT_CHARS,
    max_sec: float = MAX_SEGMENT_SEC,
) -> List[str]:
    """
    双阈值合并：仅在累计时长 < min_sec 且累计字数 < max_chars 时继续合并下一句。
    硬上限 max_sec / max_chars 防止单段过长爆框。
    LLM / pipeline-segment 模式：直接原样返回，不做任何合并。
    """
    from videoaudiotext.config import segment_merge_enabled

    if not segment_merge_enabled():
        return list(sentences)
    if not sentences:
        return []
    if len(sentences) != len(durations):
        raise ValueError("sentences and durations length mismatch")

    groups: List[List[int]] = []
    current: List[int] = []
    cur_dur = 0.0
    cur_chars = 0

    def flush() -> None:
        nonlocal current, cur_dur, cur_chars
        if current:
            groups.append(current)
        current = []
        cur_dur = 0.0
        cur_chars = 0

    for i, d in enumerate(durations):
        clen = _segment_char_count(sentences[i])
        if not current:
            current = [i]
            cur_dur = d
            cur_chars = clen
            if cur_dur >= max_sec or cur_chars >= max_chars:
                flush()
            continue

        would_dur = cur_dur + d
        would_chars = cur_chars + clen
        can_merge = (
            cur_dur < min_sec
            and cur_chars < max_chars
            and would_dur < max_sec
            and would_chars < max_chars
        )
        if can_merge:
            current.append(i)
            cur_dur = would_dur
            cur_chars = would_chars
        else:
            flush()
            current = [i]
            cur_dur = d
            cur_chars = clen
            if cur_dur >= max_sec or cur_chars >= max_chars:
                flush()

    if current:
        if (
            groups
            and cur_dur < min_sec
            and cur_chars < max_chars
            and (groups[-1] and True)
        ):
            last_dur = sum(durations[k] for k in groups[-1])
            last_chars = sum(_segment_char_count(sentences[k]) for k in groups[-1])
            if last_dur + cur_dur < max_sec and last_chars + cur_chars < max_chars:
                groups[-1].extend(current)
            else:
                groups.append(current)
        else:
            groups.append(current)

    return ["".join(sentences[k] for k in group) for group in groups]


def build_pipeline_segments(
    text: str,
    durations: Optional[List[float]] = None,
    *,
    voice: str | None = None,
    rate: str = "+0%",
) -> List[str]:
    """LLM/规则外层分句 +（规则模式）短句合并；不再做源头预切分。"""
    from videoaudiotext.config import segment_merge_enabled

    atomic = split_sentence(text)
    if len(atomic) <= 1:
        return atomic
    if not segment_merge_enabled():
        return atomic
    durs = durations
    if durs is None:
        durs = [estimate_duration(s, voice=voice, rate=rate) for s in atomic]
    merged = merge_short_segments(atomic, durs)
    return merged if merged else atomic


def resolve_pipeline_segments(
    text: str,
    durations: Optional[List[float]] = None,
    *,
    voice: str | None = None,
    rate: str = "+0%",
) -> List[str]:
    """成片单元唯一入口：LLM 语义分句（或规则回退），不做预切分。"""
    segs, _, _ = resolve_pipeline_segments_with_groups(
        text, durations, voice=voice, rate=rate
    )
    return segs


def resolve_pipeline_segments_with_groups(
    text: str,
    durations: Optional[List[float]] = None,
    *,
    voice: str | None = None,
    rate: str = "+0%",
) -> tuple[List[str], List[int], List[SegmentVisualMeta | None] | None]:
    """成片单元 + group id + LLM 视觉标（规则回退时为 None）。"""
    from videoaudiotext.config import segment_merge_enabled
    from videoaudiotext.text.split import split_sentence_with_outcome

    outcome = split_sentence_with_outcome(text)
    segments = outcome.segments
    groups = (
        list(outcome.segment_groups)
        if outcome.segment_groups is not None
        else list(range(len(segments)))
    )
    visual_meta = outcome.segment_visual_meta
    if len(segments) <= 1:
        return segments, groups, visual_meta
    if not segment_merge_enabled():
        return segments, groups, visual_meta
    durs = durations
    if durs is None:
        durs = [estimate_duration(s, voice=voice, rate=rate) for s in segments]
    merged = merge_short_segments(segments, durs)
    if not merged or merged == segments:
        return segments, groups, visual_meta
    # 规则模式合并后 group / 视觉标失效，每段自成一组
    return merged, list(range(len(merged))), None
