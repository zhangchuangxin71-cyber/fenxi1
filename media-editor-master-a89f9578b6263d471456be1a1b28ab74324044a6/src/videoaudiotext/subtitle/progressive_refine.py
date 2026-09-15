"""Refine progressive subtitle cue boundaries using segment TTS wav."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Sequence

from videoaudiotext.subtitle.types import Cue


def progressive_audio_nudge_enabled() -> bool:
    if os.environ.get("SUBTITLE_PROGRESSIVE_AUDIO_NUDGE", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    from videoaudiotext.config import subtitle_match_pipeline_segments

    return not subtitle_match_pipeline_segments()


def progressive_nudge_max_sec() -> float:
    try:
        value = float(os.environ.get("SUBTITLE_PROGRESSIVE_NUDGE_SEC", "0.10"))
    except ValueError:
        value = 0.10
    return max(0.02, min(0.25, value))


def _segment_ranges(timeline: Sequence) -> list[tuple[float, float]]:
    return [(float(e.speech_start), float(e.speech_end)) for e in timeline]


def _cue_segment_index(start: float, ranges: Sequence[tuple[float, float]]) -> int | None:
    for i, (s0, s1) in enumerate(ranges):
        if s0 - 0.02 <= start < s1 + 0.02:
            return i
    return None


def _group_cues_by_segment(
    cues: List[Cue],
    ranges: Sequence[tuple[float, float]],
) -> dict[int, list[int]]:
    groups: dict[int, list[int]] = {}
    for idx, (_text, start, _end) in enumerate(cues):
        seg_i = _cue_segment_index(float(start), ranges)
        if seg_i is None:
            continue
        groups.setdefault(seg_i, []).append(idx)
    return groups


def refine_progressive_cues_from_audio(
    cues: List[Cue],
    *,
    wav_paths: List[Path] | None,
    sentences: List[str],
    timeline: Sequence | None,
) -> tuple[List[Cue], list[str]]:
    """
    段内多条 cue 时，用 TTS wav 在边界处检测换气，微调相邻 cue 分界（±nudge）。
    不改变段级 speech_start/speech_end 包络。
    """
    if not cues or not wav_paths or not timeline or not progressive_audio_nudge_enabled():
        return list(cues), []
    if len(wav_paths) != len(sentences) or len(timeline) != len(sentences):
        return list(cues), []

    from videoaudiotext.subtitle.audio_pause import (
        estimate_boundary_time,
        pause_at_text_boundary,
    )

    ranges = _segment_ranges(timeline)
    groups = _group_cues_by_segment(cues, ranges)
    if not any(len(idxs) > 1 for idxs in groups.values()):
        return list(cues), []

    nudge = progressive_nudge_max_sec()
    gap = 0.04
    out: List[Cue] = list(cues)
    warnings: list[str] = []

    for seg_i, idxs in groups.items():
        if len(idxs) < 2:
            continue
        wav = wav_paths[seg_i]
        if not Path(wav).is_file():
            continue
        seg_text = sentences[seg_i]
        s0, s1 = ranges[seg_i]
        speech_dur = max(0.05, s1 - s0)

        for k in range(len(idxs) - 1):
            left_i, right_i = idxs[k], idxs[k + 1]
            left_text, left_start, left_end = out[left_i]
            right_text, right_start, right_end = out[right_i]

            prefix_parts: list[str] = []
            for j in idxs[: k + 1]:
                prefix_parts.append(out[j][0].replace("\n", ""))
            prefix_text = "".join(prefix_parts)

            est_rel = estimate_boundary_time(seg_text, prefix_text, speech_dur)
            est_global = s0 + est_rel
            pause_dur = pause_at_text_boundary(
                Path(wav),
                seg_text,
                prefix_text,
                speech_dur,
            )
            if pause_dur < 0.05:
                continue

            current = float(left_end)
            delta = est_global - current
            if abs(delta) <= 0.008:
                continue
            shift = max(-nudge, min(nudge, delta))
            new_boundary = current + shift
            new_boundary = max(float(left_start) + 0.12, new_boundary)
            new_boundary = min(float(right_end) - 0.12, new_boundary)
            if new_boundary <= float(left_start) + 0.05:
                continue
            if new_boundary >= float(right_end) - 0.05:
                continue
            if abs(new_boundary - current) < 0.008:
                continue

            out[left_i] = (left_text, float(left_start), new_boundary)
            out[right_i] = (right_text, new_boundary + gap, float(right_end))
            warnings.append(
                f"段{seg_i + 1} cue{left_i + 1}-{right_i + 1} 边界 "
                f"{current:.3f}s→{new_boundary:.3f}s（音频微调）"
            )

    return out, warnings
