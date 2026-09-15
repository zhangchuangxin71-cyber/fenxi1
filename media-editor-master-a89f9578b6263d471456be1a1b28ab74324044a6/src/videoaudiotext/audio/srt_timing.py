"""Derive per-segment speech durations and inter-segment gaps from subtitle SRT."""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence

from videoaudiotext.subtitle.types import Cue

# Progressive cues within one sentence are separated by ~0.04s; sentence gaps >= ~0.12s.
_INTER_SEGMENT_GAP_SEC = 0.10


def _normalize_text(text: str) -> str:
    return "".join(str(text or "").split())


def _speech_window(group: Sequence[Cue]) -> tuple[float, float]:
    starts = [float(c[1]) for c in group]
    ends = [float(c[2]) for c in group]
    return min(starts), max(ends)


def _group_cues_one_to_one(cues: Sequence[Cue], n_segments: int) -> List[List[Cue]] | None:
    if len(cues) != n_segments:
        return None
    return [[cue] for cue in cues]


def _group_cues_by_largest_gaps(
    cues: Sequence[Cue],
    n_segments: int,
) -> List[List[Cue]] | None:
    """
    Split cues at the (n_segments - 1) largest inter-cue gaps.

    Display SRT often has more cues than segments (wrap-reveal); sentence gaps
    are typically much larger than intra-sentence cue gaps (~0.04s vs ~0.35s).
    """
    n_cues = len(cues)
    if n_cues < n_segments or n_segments <= 0:
        return None
    if n_segments == 1:
        return [list(cues)]
    if n_cues == n_segments:
        return [[cue] for cue in cues]

    boundary_gaps: list[tuple[float, int]] = []
    for i in range(n_cues - 1):
        gap = float(cues[i + 1][1]) - float(cues[i][2])
        boundary_gaps.append((gap, i))

    split_after = sorted(
        idx for _, idx in sorted(boundary_gaps, key=lambda item: item[0], reverse=True)[
            : n_segments - 1
        ]
    )
    if len(split_after) != n_segments - 1:
        return None

    groups: List[List[Cue]] = []
    start = 0
    for split in split_after:
        end = split + 1
        if end <= start:
            return None
        groups.append(list(cues[start:end]))
        start = end
    if start >= n_cues:
        return None
    groups.append(list(cues[start:]))
    if len(groups) != n_segments:
        return None
    return groups


def _group_cues_by_gap_clusters(
    cues: Sequence[Cue],
    n_segments: int,
) -> List[List[Cue]] | None:
    if not cues:
        return None
    groups: List[List[Cue]] = [[cues[0]]]
    for prev, cur in zip(cues, cues[1:]):
        gap = float(cur[1]) - float(prev[2])
        if gap >= _INTER_SEGMENT_GAP_SEC and len(groups) < n_segments:
            groups.append([cur])
        else:
            groups[-1].append(cur)
    if len(groups) != n_segments:
        return None
    return groups


def _group_cues_by_text(
    cues: Sequence[Cue],
    segment_texts: Sequence[str],
) -> List[List[Cue]] | None:
    groups: List[List[Cue]] = []
    idx = 0
    for seg_text in segment_texts:
        target = _normalize_text(seg_text)
        if not target:
            return None
        group: List[Cue] = []
        acc = ""
        while idx < len(cues):
            group.append(cues[idx])
            acc += _normalize_text(cues[idx][0])
            idx += 1
            if acc == target:
                break
        if acc != target:
            return None
        groups.append(group)
    if idx != len(cues):
        return None
    return groups


def speech_durations_and_gaps_from_srt(
    srt_path: Path,
    segment_texts: Sequence[str],
) -> tuple[list[float], list[float]]:
    """
    Map SRT cues to segments and return (speech_durations, gaps_sec).

    speech_durations[i] = segment i speech span (last cue end - first cue start).
    gaps_sec[i] = silence after segment i until segment i+1 speech starts (last segment 0).
    """
    from videoaudiotext.subtitle.export import parse_srt_file

    n = len(segment_texts)
    if n <= 0:
        raise ValueError("segment_texts is empty")

    cues = sorted(parse_srt_file(Path(srt_path)), key=lambda c: (float(c[1]), float(c[2])))
    if not cues:
        raise ValueError("SRT has no cues")

    groups = (
        _group_cues_one_to_one(cues, n)
        or _group_cues_by_largest_gaps(cues, n)
        or _group_cues_by_gap_clusters(cues, n)
        or _group_cues_by_text(cues, segment_texts)
    )
    if groups is None:
        raise ValueError(
            f"cannot map {len(cues)} SRT cues to {n} segments"
        )

    speech_starts: list[float] = []
    speech_ends: list[float] = []
    for group in groups:
        s0, s1 = _speech_window(group)
        speech_starts.append(s0)
        speech_ends.append(s1)

    durations = [max(0.01, e - s) for s, e in zip(speech_starts, speech_ends)]
    gaps: list[float] = []
    for i in range(n):
        if i >= n - 1:
            gaps.append(0.0)
        else:
            gaps.append(max(0.0, speech_starts[i + 1] - speech_ends[i]))
    return durations, gaps
