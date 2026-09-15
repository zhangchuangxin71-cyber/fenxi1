"""Reading-speed (CPS) guard for subtitle cues."""

from __future__ import annotations

from typing import Sequence

from videoaudiotext.config import (
    SUBTITLE_CPS_ENFORCE,
    SUBTITLE_CPS_MAX,
    SUBTITLE_CUE_MIN_DISPLAY_SEC,
    SUBTITLE_CUE_SPLIT_GAP_SEC,
)
from videoaudiotext.subtitle.display import _core_char_len
from videoaudiotext.subtitle.types import Cue


def cue_cps(text: str, start: float, end: float) -> float:
    dur = max(1e-6, float(end) - float(start))
    return _core_char_len(text.replace("\n", "")) / dur


def _speech_segment_index(
    start: float,
    end: float,
    bounds: Sequence[tuple[float, float]],
) -> int:
    from videoaudiotext.subtitle.safe_area import segment_index_for_cue

    return segment_index_for_cue(float(start), float(end), bounds)


def _same_speech_segment(
    start_a: float,
    end_a: float,
    start_b: float,
    end_b: float,
    bounds: Sequence[tuple[float, float]],
) -> bool:
    return _speech_segment_index(start_a, end_a, bounds) == _speech_segment_index(
        start_b, end_b, bounds
    )


def _speech_segment_end(
    start: float,
    end: float,
    bounds: Sequence[tuple[float, float]],
) -> float:
    idx = _speech_segment_index(start, end, bounds)
    return float(bounds[idx][1])


def enforce_cps_limits(
    cues: list[Cue],
    *,
    max_cps: float | None = None,
    min_duration: float | None = None,
    speech_segment_bounds: Sequence[tuple[float, float]] | None = None,
    allow_merge: bool = True,
) -> tuple[list[Cue], list[str]]:
    """
    超标 CPS 时优先与后条合并；仍超标则在不超过下条 start 的前提下延长 end。

    When ``speech_segment_bounds`` is set, merge/extend never crosses segment speech windows.
    """
    if not cues or not SUBTITLE_CPS_ENFORCE:
        return list(cues), []

    cap = SUBTITLE_CPS_MAX if max_cps is None else float(max_cps)
    min_dur = (
        SUBTITLE_CUE_MIN_DISPLAY_SEC if min_duration is None else float(min_duration)
    )
    if cap <= 0:
        return list(cues), []

    gap = max(0.001, SUBTITLE_CUE_SPLIT_GAP_SEC)
    bounds = speech_segment_bounds
    warnings: list[str] = []
    out: list[Cue] = []
    i = 0
    n = len(cues)

    while i < n:
        text, start, end = cues[i]
        dur = float(end) - float(start)
        chars = _core_char_len(text.replace("\n", ""))
        cps = chars / max(1e-6, dur)

        if cps <= cap + 0.05:
            out.append((text, start, end))
            i += 1
            continue

        merged = False
        if allow_merge and i + 1 < n:
            ntext, ns, ne = cues[i + 1]
            same_segment = (
                bounds is None
                or _same_speech_segment(start, end, ns, ne, bounds)
            )
            preserve_boundary = text.rstrip().endswith("、")
            if same_segment and not preserve_boundary:
                combined = text.replace("\n", "") + ntext.replace("\n", "")
                mdur = float(ne) - float(start)
                mcps = _core_char_len(combined) / max(1e-6, mdur)
                if mdur >= min_dur and mcps <= cap + 0.15:
                    out.append((combined, start, ne))
                    warnings.append(
                        f"CPS {cps:.1f}>{cap:.1f}：合并 cue {len(out)}+{len(out)+1}"
                    )
                    i += 2
                    merged = True
            elif bounds is not None and not same_segment:
                warnings.append(
                    f"CPS {cps:.1f}>{cap:.1f}：跳过跨段合并 cue {i + 1}+{i + 2}"
                )

        if merged:
            continue

        need_dur = max(min_dur, chars / cap)
        if i + 1 < n:
            max_end = float(cues[i + 1][1]) - gap
            if bounds is not None and not _same_speech_segment(
                start, end, cues[i + 1][1], cues[i + 1][2], bounds
            ):
                max_end = min(max_end, _speech_segment_end(start, end, bounds))
        else:
            max_end = float(end) + 3.0
        if bounds is not None:
            max_end = min(max_end, _speech_segment_end(start, end, bounds))
        new_end = min(max(float(end), float(start) + need_dur), max_end)
        if new_end > float(end) + 0.01:
            out.append((text, start, new_end))
            warnings.append(
                f"CPS {cps:.1f}>{cap:.1f}：延长 cue {len(out)+1} 至 {new_end:.2f}s"
            )
        else:
            out.append((text, start, end))
            warnings.append(
                f"CPS {cps:.1f}>{cap:.1f}：cue {len(out)+1} 无法合并/延长"
            )
        i += 1

    return out, warnings
