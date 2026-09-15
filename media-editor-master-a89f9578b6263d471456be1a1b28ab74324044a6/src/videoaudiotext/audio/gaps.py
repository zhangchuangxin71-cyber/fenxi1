"""Inter-segment pauses, absolute audio timeline, and clip length planning."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from videoaudiotext.audio.xfade import XfadeBoundaryHint

from videoaudiotext.config import (
    CLIP_XFADE_SEC,
    GAP_BIG_SENTENCE_SEC,
    GAP_ENABLED,
    VIDEO_FPS,
    XFADE_MAX_RATIO,
)

# 大句结束符
SENTENCE_END_CHARS = ("！", "。", "？", "!", "?")
# 小句切分点（连读但留短歇息）
CLAUSE_END_CHARS = ("，", "、", "；", ",", ";")

# 兼容旧引用
SEGMENT_TAIL_PAD_SEC = GAP_BIG_SENTENCE_SEC


@dataclass(frozen=True)
class SegmentTimelineEntry:
    """音轨主轴上的单段绝对时间（秒）。"""

    index: int
    text: str
    speech_duration: float
    gap_duration: float
    speech_start: float
    speech_end: float
    audio_end: float
    xfade_out: float
    video_content_duration: float
    video_clip_duration: float


def get_gap_duration_for_text(
    text: str,
    *,
    is_last_clip: bool,
    speed: float = 1.0,
) -> float:
    """段尾追加的绝对静音（Gap）；末段为 0。非末段统一 GAP_BIG_SENTENCE_SEC / speed。"""
    if not GAP_ENABLED or is_last_clip:
        return 0.0
    t = (text or "").strip()
    if not t:
        return 0.0
    if speed <= 0:
        raise ValueError("speed must be positive")
    return GAP_BIG_SENTENCE_SEC / speed


def segment_padding_for_text(text: str, *, speed: float = 1.0) -> float:
    """兼容旧 API：非末段 gap（调用方需自行判断 is_last）。"""
    return get_gap_duration_for_text(text, is_last_clip=False, speed=speed)


def compute_xfade_out(
    gap_duration: float,
    *,
    is_last: bool,
    at_boundary: bool = True,
    speech_duration: float = 0.0,
    xfade_hint: "XfadeBoundaryHint | None" = None,
) -> float:
    """本段尾画面叠化时长；自适应模式下考虑检索分与素材连续性。"""
    from videoaudiotext.audio.xfade import compute_adaptive_xfade_out

    return compute_adaptive_xfade_out(
        gap_duration,
        is_last=is_last,
        at_boundary=at_boundary,
        speech_duration=speech_duration,
        hint=xfade_hint,
    )


def _legacy_base_xfade(gap_duration: float, *, at_boundary: bool) -> float:
    """Tests / 对照：仅 gap 约束的叠化上限。"""
    if not at_boundary or CLIP_XFADE_SEC <= 0.001:
        return 0.0
    if gap_duration <= 0.001:
        return 0.0
    return min(CLIP_XFADE_SEC, gap_duration * XFADE_MAX_RATIO)


def compute_sentence_gaps(segments: Sequence[str], *, speed: float = 1.0) -> List[float]:
    """每段末尾 gap；与 compute_absolute_timeline 一致。"""
    n = len(segments)
    if n <= 0:
        return []
    return [
        get_gap_duration_for_text(
            segments[i],
            is_last_clip=(i >= n - 1),
            speed=speed,
        )
        for i in range(n)
    ]


def compute_absolute_timeline(
    segments: Sequence[str],
    speech_durations: Sequence[float],
    *,
    speed: float = 1.0,
    xfade_at_boundary: Sequence[bool] | None = None,
    xfade_boundary_hints: Sequence["XfadeBoundaryHint"] | None = None,
) -> List[SegmentTimelineEntry]:
    """
    音轨绝对时间轴：严格累加，不因 xfade 回退。
    xfade_at_boundary[i] 表示段 i 与 i+1 之间是否做画面叠化（大分镜内为 False）。
    """
    n = len(segments)
    if n != len(speech_durations):
        raise ValueError("segments and speech_durations length mismatch")
    if xfade_at_boundary is not None and len(xfade_at_boundary) != max(0, n - 1):
        raise ValueError("xfade_at_boundary length mismatch")
    if xfade_boundary_hints is not None and len(xfade_boundary_hints) != max(0, n - 1):
        raise ValueError("xfade_boundary_hints length mismatch")

    entries: List[SegmentTimelineEntry] = []
    current = 0.0

    for i in range(n):
        text = str(segments[i])
        speech = max(0.0, float(speech_durations[i]))
        is_last = i >= n - 1
        gap = get_gap_duration_for_text(text, is_last_clip=is_last, speed=speed)
        speech_start = current
        speech_end = speech_start + speech
        audio_end = speech_end + gap
        at_boundary = True
        if xfade_at_boundary is not None and i < n - 1:
            at_boundary = bool(xfade_at_boundary[i])
        hint = None
        if xfade_boundary_hints is not None and i < n - 1:
            hint = xfade_boundary_hints[i]
        xfade = compute_xfade_out(
            gap,
            is_last=is_last,
            at_boundary=at_boundary,
            speech_duration=speech,
            xfade_hint=hint,
        )
        content_dur = speech + gap
        clip_dur = content_dur + xfade

        entries.append(
            SegmentTimelineEntry(
                index=i,
                text=text,
                speech_duration=speech,
                gap_duration=gap,
                speech_start=speech_start,
                speech_end=speech_end,
                audio_end=audio_end,
                xfade_out=xfade,
                video_content_duration=content_dur,
                video_clip_duration=clip_dur,
            )
        )
        current = audio_end

    return entries


def _align_up_to_frame(seconds: float) -> float:
    frames = max(1, math.ceil(seconds * VIDEO_FPS))
    return frames / VIDEO_FPS


def segment_video_durations(
    speech_durations: List[float],
    gaps: List[float],
) -> List[float]:
    """
    裁切用画面时长 = 语音 + gap（单一定义，不再叠加 SEGMENT_TAIL_PAD）。
    """
    if len(speech_durations) != len(gaps):
        raise ValueError("durations and gaps length mismatch")
    out: List[float] = []
    for speech, gap in zip(speech_durations, gaps):
        raw = max(0.0, speech) + max(0.0, gap)
        out.append(_align_up_to_frame(raw))
    return out


def timeline_video_content_durations(timeline: Sequence[SegmentTimelineEntry]) -> List[float]:
    """各段画面内容区时长（不含 xfade 尾延长）。"""
    return [_align_up_to_frame(e.video_content_duration) for e in timeline]


def timeline_planned_clip_durations(timeline: Sequence[SegmentTimelineEntry]) -> List[float]:
    """各段 clip 计划时长（内容区 + 尾叠化延长），与 timeline 主轴一致。"""
    return [float(e.video_clip_duration) for e in timeline]


def timeline_output_durations(
    timeline: Sequence[SegmentTimelineEntry],
    *,
    hard_cut: bool = False,
) -> List[float]:
    """成片拼接 trim 时长：硬切按 speech_start 切分（与字幕/口播同轴），叠化含尾叠化延长。"""
    if not hard_cut:
        return timeline_planned_clip_durations(timeline)
    if not timeline:
        return []
    out: List[float] = []
    for i in range(len(timeline) - 1):
        out.append(float(timeline[i + 1].speech_start) - float(timeline[i].speech_start))
    out.append(float(timeline[-1].audio_end) - float(timeline[-1].speech_start))
    return out


def xfade_offsets_for_speech_starts(
    timeline: Sequence[SegmentTimelineEntry],
    xfade_secs: Sequence[float],
) -> List[float]:
    """
    叠化在「下一段口播开始前 xf 秒」启动，口播/字幕开始时新画面已基本到位。
    offset[i] = speech_start[i+1] - xfade_secs[i]
    """
    if len(timeline) <= 1:
        return []
    if len(xfade_secs) != len(timeline) - 1:
        raise ValueError("xfade_secs length mismatch")
    return [
        float(timeline[i + 1].speech_start) - float(xfade_secs[i])
        for i in range(len(xfade_secs))
    ]


def timeline_xfade_transitions(timeline: Sequence[SegmentTimelineEntry]) -> List[float]:
    """段间画面叠化时长（末段无）。"""
    if len(timeline) <= 1:
        return []
    return [e.xfade_out for e in timeline[:-1]]


def shot_level_xfade_transitions(
    timeline: Sequence[SegmentTimelineEntry],
    shots: Sequence,
) -> List[float]:
    """大分镜 clip 之间的叠化时长。"""
    if len(shots) <= 1:
        return []
    out: List[float] = []
    for shot in shots[:-1]:
        last_seg = shot.segment_indices[-1] - 1
        if 0 <= last_seg < len(timeline):
            out.append(timeline[last_seg].xfade_out)
        else:
            out.append(0.0)
    return out


def shot_planned_clip_durations(
    timeline: Sequence[SegmentTimelineEntry],
    shots: Sequence,
) -> List[float]:
    """各 shot clip 计划时长（内容区之和 + 末段尾叠化），与 build_super_clips_direct 一致。"""
    if not shots:
        return []
    n = len(shots)
    use_xfade = CLIP_XFADE_SEC > 0.001 and n > 1
    out: List[float] = []
    for shot_idx, shot in enumerate(shots):
        idxs = shot.segment_indices
        content_dur = sum(timeline[j - 1].video_content_duration for j in idxs)
        last_idx = idxs[-1] - 1
        tail_pad = (
            timeline[last_idx].xfade_out
            if use_xfade and shot_idx < n - 1 and last_idx < len(timeline)
            else 0.0
        )
        out.append(float(content_dur + tail_pad))
    return out


def shot_output_durations(
    timeline: Sequence[SegmentTimelineEntry],
    shots: Sequence,
    *,
    hard_cut: bool = False,
) -> List[float]:
    """成片拼接 trim 时长（shot 级）。"""
    if not hard_cut:
        return shot_planned_clip_durations(timeline, shots)
    if not shots:
        return []
    out: List[float] = []
    for shot_idx, shot in enumerate(shots):
        first = shot.segment_indices[0] - 1
        if shot_idx < len(shots) - 1:
            next_first = shots[shot_idx + 1].segment_indices[0] - 1
            out.append(
                float(timeline[next_first].speech_start)
                - float(timeline[first].speech_start)
            )
        else:
            last = shot.segment_indices[-1] - 1
            out.append(
                float(timeline[last].audio_end) - float(timeline[first].speech_start)
            )
    return out


def master_audio_duration(timeline: Sequence[SegmentTimelineEntry]) -> float:
    if not timeline:
        return 0.0
    return timeline[-1].audio_end


def xfade_at_boundary_from_shots(
    shot_boundaries: Sequence[bool],
    segment_count: int,
) -> List[bool]:
    """段 i→i+1 是否在成片 clip 边界做 xfade（大分镜内 False）。"""
    if segment_count <= 1:
        return []
    if not shot_boundaries:
        return [True] * (segment_count - 1)
    out: List[bool] = []
    for i in range(segment_count - 1):
        out.append(bool(shot_boundaries[i]) if i < len(shot_boundaries) else True)
    return out
