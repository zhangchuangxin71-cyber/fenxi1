"""Progressive subtitles: one screen line at a time, TTS-aligned (no stacked wrap)."""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence

from videoaudiotext.config import SUBTITLE_CUE_SPLIT_GAP_SEC
from videoaudiotext.subtitle.types import Cue

SUBTITLE_WRAP_REVEAL_MIN_SEC = 0.45
SUBTITLE_WRAP_REVEAL_MIN_CHARS = 4


def wrap_reveal_enabled() -> bool:
    """成片默认：逐行渐进（一条一行、无叠行 \\N）。"""
    return True


def _wrap_reveal_min_line_sec() -> float:
    return SUBTITLE_WRAP_REVEAL_MIN_SEC


def _adaptive_min_line_sec(
    speech_dur: float,
    line1: str,
    line2: str,
) -> float:
    """快口播略缩短、行1极短时略延长，减轻阅读节奏撕裂。"""
    base = _wrap_reveal_min_line_sec()
    total_chars = max(1, len(line1.replace("\n", "")) + len(line2.replace("\n", "")))
    rate = total_chars / max(0.1, float(speech_dur))
    if rate >= 8.0:
        return max(0.28, base * 0.75)
    from videoaudiotext.subtitle.display import _core_char_len

    if _core_char_len(line1) <= 3:
        return max(base, min(0.65, base + 0.12))
    return base


def _split_wrap_lines(display: str) -> tuple[str, ...]:
    parts = [
        ln.strip()
        for ln in display.replace("\\N", "\n").split("\n")
        if ln.strip()
    ]
    if len(parts) <= 1:
        return (parts[0],) if parts else ("",)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], "".join(parts[1:])


def _prefix_text_for_audio(segment_text: str, line1: str) -> str:
    """将折行第一行映射为段内 prefix，供拟音/静音对齐。"""
    seg = segment_text.replace("\n", "").strip()
    l1 = line1.replace("\n", "").strip()
    if not seg or not l1:
        return l1

    if seg.startswith(l1):
        return l1

    probe = l1.rstrip("，、；：。！？")
    for candidate in (l1, probe, probe + "，"):
        if candidate and seg.startswith(candidate):
            return candidate

    anchor = probe[: max(2, min(6, len(probe)))]
    idx = seg.find(anchor)
    if idx >= 0:
        end = idx + len(probe)
        tail = seg[end - 1 : end] if end > 0 else ""
        if end <= len(seg) and seg[idx:end].rstrip("，、；") == probe.rstrip("，、；"):
            return seg[:end] if tail in "，、；" else seg[:end]

    return l1


def _boundary_from_audio(
    wav_path: Path,
    segment_text: str,
    prefix_text: str,
    speech_duration: float,
    seg_start: float,
) -> float | None:
    from videoaudiotext.subtitle.audio_pause import (
        estimate_boundary_time,
        pause_at_text_boundary,
        silence_duration_in_window,
    )

    rel = estimate_boundary_time(segment_text, prefix_text, speech_duration)
    pause = pause_at_text_boundary(
        wav_path,
        segment_text,
        prefix_text,
        speech_duration,
    )
    if pause < 0.06:
        return None

    best_rel = rel
    best_pause = pause
    for delta in (-0.12, -0.08, -0.04, 0.04, 0.08, 0.12):
        cand = max(0.05, min(speech_duration - 0.05, rel + delta))
        prefix_probe = segment_text[: max(1, int(len(segment_text) * cand / speech_duration))]
        p = pause_at_text_boundary(
            wav_path,
            segment_text,
            prefix_probe or prefix_text,
            speech_duration,
        )
        if p > best_pause:
            best_pause = p
            best_rel = cand

    center = seg_start + best_rel
    local_pause = silence_duration_in_window(
        wav_path,
        best_rel,
        window_before=0.04,
        window_after=0.14,
    )
    if local_pause >= 0.05:
        return center
    return seg_start + best_rel


def align_wrap_reveal_boundary(
    segment_text: str,
    line1: str,
    line2: str,
    seg_start: float,
    seg_end: float,
    wav_path: Path | str | None,
) -> float:
    """第二行开始时刻：拟音权重 + TTS 边界静音。"""
    from videoaudiotext.subtitle.audio_pause import estimate_boundary_time
    from videoaudiotext.subtitle.text_timing import get_phonetic_weight

    speech_dur = max(0.1, float(seg_end) - float(seg_start))
    min_line = _adaptive_min_line_sec(speech_dur, line1, line2)
    gap = max(0.001, SUBTITLE_CUE_SPLIT_GAP_SEC)

    prefix = _prefix_text_for_audio(segment_text, line1)
    rel = estimate_boundary_time(segment_text, prefix, speech_dur)

    w1 = max(0.5, get_phonetic_weight(line1))
    w2 = max(0.5, get_phonetic_weight(line2))
    weight_rel = speech_dur * (w1 / (w1 + w2))

    if wav_path and Path(wav_path).is_file():
        audio_boundary = _boundary_from_audio(
            Path(wav_path),
            segment_text,
            prefix,
            speech_dur,
            float(seg_start),
        )
        if audio_boundary is not None:
            rel_audio = audio_boundary - float(seg_start)
            rel = 0.55 * rel_audio + 0.45 * weight_rel
        else:
            rel = 0.35 * rel + 0.65 * weight_rel
    else:
        rel = 0.25 * rel + 0.75 * weight_rel

    rel = max(min_line, min(speech_dur - min_line - gap, rel))
    return float(seg_start) + rel


def _line_limit_for_phrase(phrase: str, soft_max: int, *, content_rect=None) -> int:
    from videoaudiotext.config.subtitle import progressive_pack_line_limit

    return progressive_pack_line_limit(phrase, soft_max=soft_max, content_rect=content_rect)


def _phrase_fits_one_line(
    text: str, max_line: int, *, content_rect=None
) -> bool:
    from videoaudiotext.config.subtitle import progressive_screen_line_needs_split
    from videoaudiotext.subtitle.display import _normalize_subtitle_text

    plain = _normalize_subtitle_text(text.replace("\n", ""))
    if not plain:
        return True
    return not progressive_screen_line_needs_split(plain, content_rect=content_rect)


def _screen_line_min_chars() -> int:
    from videoaudiotext.config.subtitle import progressive_min_screen_chars

    return progressive_min_screen_chars()


def _is_screen_line_orphan(line: str, min_part: int) -> bool:
    from videoaudiotext.subtitle.quotes import is_too_short_screen_line

    if is_too_short_screen_line(line):
        return True
    from videoaudiotext.subtitle.display import _ORPHAN_LINE2_CHARS, _core_char_len

    ln = line.strip()
    if not ln:
        return True
    if _core_char_len(ln) < min_part:
        return True
    body = ln.rstrip("，、。！？")
    return len(body) == 1 and body in _ORPHAN_LINE2_CHARS


def _can_merge_screen_lines(left: str, right: str, max_line: int) -> bool:
    from videoaudiotext.subtitle.quotes import (
        progressive_lines_combine_fits,
        progressive_screen_lines_should_stay_split,
        should_merge_de_orphan_lines,
        should_merge_quote_orphan_lines,
        should_merge_short_tail_lines,
        should_merge_single_char_orphan_lines,
    )

    if progressive_screen_lines_should_stay_split(left, right, max_line):
        return False
    if should_merge_quote_orphan_lines(left, right, max_line):
        return True
    if should_merge_de_orphan_lines(left, right, max_line):
        return True
    if should_merge_short_tail_lines(left, right, max_line):
        return True
    if should_merge_single_char_orphan_lines(left, right, max_line):
        return True
    return progressive_lines_combine_fits(left, right)


def _pack_phrase_to_screen_lines(phrase: str, max_line: int) -> list[str]:
    """将短语拆成若干条屏显单行（引号原子、语义切分）。"""
    from videoaudiotext.subtitle.display import _normalize_subtitle_text
    from videoaudiotext.subtitle.quotes import pack_text_to_screen_lines

    plain = _normalize_subtitle_text(phrase.replace("\n", ""))
    if not plain:
        return []
    line_limit = _line_limit_for_phrase(plain, max_line)
    return pack_text_to_screen_lines(plain, line_limit, strict_max=True)


def _merge_orphan_screen_lines(lines: list[str], max_line: int) -> list[str]:
    """合并过短屏显行，避免「的灯」「上桌」等 2～3 字单独成条。"""
    min_part = _screen_line_min_chars()
    work = [ln.strip() for ln in lines if ln.strip()]
    if len(work) <= 1:
        return work

    changed = True
    for _ in range(24):
        if not changed or len(work) <= 1:
            break
        changed = False
        for i in range(len(work)):
            if not _is_screen_line_orphan(work[i], min_part):
                continue
            if i > 0 and _can_merge_screen_lines(work[i - 1], work[i], max_line):
                work[i - 1 : i + 1] = [work[i - 1] + work[i]]
                changed = True
                break
            if i + 1 < len(work) and _can_merge_screen_lines(work[i], work[i + 1], max_line):
                work[i : i + 2] = [work[i] + work[i + 1]]
                changed = True
                break
            if i > 0:
                resplit = _pack_phrase_to_screen_lines(work[i - 1] + work[i], max_line)
                if resplit and (
                    len(resplit) == 1
                    or all(
                        not _is_screen_line_orphan(ln, min_part) for ln in resplit
                    )
                ):
                    work[i - 1 : i + 1] = resplit
                    changed = True
                    break
            if i + 1 < len(work):
                resplit = _pack_phrase_to_screen_lines(work[i] + work[i + 1], max_line)
                if resplit and (
                    len(resplit) == 1
                    or all(
                        not _is_screen_line_orphan(ln, min_part) for ln in resplit
                    )
                ):
                    work[i : i + 2] = resplit
                    changed = True
                    break
        if not changed:
            for i in range(len(work) - 1, 0, -1):
                if _is_screen_line_orphan(work[i], min_part):
                    combined = work[i - 1] + work[i]
                    resplit = _pack_phrase_to_screen_lines(combined, max_line)
                    if resplit != work[i - 1 : i + 1] and (
                        len(resplit) == 1
                        or all(
                            not _is_screen_line_orphan(ln, min_part) for ln in resplit
                        )
                    ):
                        work[i - 1 : i + 1] = resplit
                        changed = True
                        break
                    if _can_merge_screen_lines(work[i - 1], work[i], max_line):
                        work[i - 1 : i + 1] = [combined]
                        changed = True
                    break
    return work


def _split_phrase_to_screen_lines(phrase: str, max_line: int) -> list[str]:
    packed = _pack_phrase_to_screen_lines(phrase, max_line)
    return _merge_orphan_screen_lines(packed, max_line)


def _progressive_max_line(
    start: float,
    end: float,
    *,
    content_rect=None,
    segment_bounds: List[tuple[float, float]] | None = None,
    content_rects: list | None = None,
) -> tuple[int, bool]:
    """渐进屏显：先算 90% 画布硬宽与 pack_chars，再作为装箱硬上限。"""
    from videoaudiotext.config.subtitle import progressive_layout_limits
    from videoaudiotext.subtitle.display import _content_rect_for_cue, _layout_for_cue

    rect = _content_rect_for_cue(
        start,
        end,
        content_rect=content_rect,
        segment_bounds=segment_bounds,
        content_rects=content_rects,
    )
    _, prefer = _layout_for_cue(
        start,
        end,
        content_rect=rect,
        segment_bounds=segment_bounds,
        content_rects=content_rects,
    )
    limits = progressive_layout_limits(content_rect=rect)
    return int(limits["pack_chars"]), prefer


def _split_cue_to_progressive_lines(
    text: str,
    t0: float,
    t1: float,
    max_line: int,
    *,
    content_rect=None,
) -> List[Cue]:
    """将单条 phrase 拆成多条渐进 cue，按字数比例分配时间。"""
    from videoaudiotext.subtitle.progressive_timing import (
        floor_degenerate_cue_durations,
        min_reading_duration,
        pack_screen_lines_for_time_budget,
    )
    from videoaudiotext.subtitle.text_timing import distribute_intervals, get_phonetic_weight

    plain = text.replace("\\N", "\n").strip()
    dur = max(0.05, float(t1) - float(t0))
    lines = pack_screen_lines_for_time_budget(
        plain,
        max_line,
        dur,
        content_rect=content_rect,
    )
    if len(lines) <= 1:
        line = lines[0] if lines else plain
        return [(line, float(t0), float(t1))]

    gap = max(0.001, SUBTITLE_CUE_SPLIT_GAP_SEC)
    n_gap = max(0, len(lines) - 1)
    usable = max(0.05 * len(lines), dur - gap * n_gap)
    weights = [max(0.5, get_phonetic_weight(ln)) for ln in lines]
    min_slices = [max(0.12, min_reading_duration(ln)) for ln in lines]
    if sum(min_slices) >= usable - 1e-6:
        slices = distribute_intervals(usable, min_slices, min_slice=0.12)
    else:
        extra = usable - sum(min_slices)
        wsum = sum(weights) or 1.0
        alloc = [min_slices[i] + extra * (weights[i] / wsum) for i in range(len(lines))]
        slices = distribute_intervals(usable, alloc, min_slice=0.12)

    out: List[Cue] = []
    cursor = float(t0)
    for i, line in enumerate(lines):
        slice_dur = max(0.12, slices[i][1] - slices[i][0]) if i < len(slices) else 0.12
        start = cursor
        end = float(t1) if i == len(lines) - 1 else min(float(t1), start + slice_dur)
        if end <= start:
            end = min(float(t1), start + min_reading_duration(line))
        out.append((line, start, end))
        cursor = end + gap
    if out:
        out[-1] = (out[-1][0], out[-1][1], float(t1))

    return floor_degenerate_cue_durations(out, span_end=float(t1))


def _split_cue_to_screen_lines(
    text: str,
    t0: float,
    t1: float,
    max_line: int,
    *,
    content_rect=None,
) -> List[Cue]:
    """单条 cue 若超 90% 硬宽，按渐进语义装箱切时间轴为多条单行 cue。"""
    from videoaudiotext.config.subtitle import progressive_screen_line_needs_split
    from videoaudiotext.subtitle.display import _normalize_subtitle_text

    plain = text.replace("\\N", "\n").strip()
    normalized = _normalize_subtitle_text(plain.replace("\n", ""))

    if not progressive_screen_line_needs_split(normalized, content_rect=content_rect):
        return [(normalized or plain, float(t0), float(t1))]
    return _split_cue_to_progressive_lines(
        text, t0, t1, max_line, content_rect=content_rect
    )


def _merge_orphan_phrase_cues(cues: List[Cue], max_line: int) -> List[Cue]:
    """合并时间轴上过短的短语 cue（如「细看」「的灯」）。"""
    min_part = _screen_line_min_chars()
    work = [(t.strip(), float(s), float(e)) for t, s, e in cues if t.strip()]
    if len(work) <= 1:
        return work

    changed = True
    for _ in range(16):
        if not changed or len(work) <= 1:
            break
        changed = False
        for i in range(len(work)):
            text, t0, t1 = work[i]
            if not _is_screen_line_orphan(text, min_part):
                continue
            if i + 1 < len(work):
                ntext, _ns, ne = work[i + 1]
                if _can_merge_screen_lines(text, ntext, max_line) or _is_screen_line_orphan(
                    ntext, min_part
                ):
                    work[i : i + 2] = [(text + ntext, t0, ne)]
                    changed = True
                    break
            if i > 0:
                ptext, ps, _pe = work[i - 1]
                if _can_merge_screen_lines(ptext, text, max_line) or _is_screen_line_orphan(
                    ptext, min_part
                ):
                    work[i - 1 : i + 1] = [(ptext + text, ps, t1)]
                    changed = True
                    break
    return work


def _merge_quote_orphan_phrase_cues(cues: List[Cue], max_line: int) -> List[Cue]:
    """Merge quote-only / quote-leading progressive cues with the previous cue."""
    from videoaudiotext.subtitle.protected_spans import should_merge_protected_orphan_lines

    work = [(t.strip(), float(s), float(e)) for t, s, e in cues if t.strip()]
    if len(work) <= 1:
        return work

    changed = True
    for _ in range(16):
        if not changed or len(work) <= 1:
            break
        changed = False
        for i in range(1, len(work)):
            ptext, ps, _pe = work[i - 1]
            text, _t0, te = work[i]
            if should_merge_protected_orphan_lines(ptext, text, max_line):
                work[i - 1 : i + 1] = [(ptext + text, ps, te)]
                changed = True
                break
    return work


def _enforce_cue_screen_width(
    cues: List[Cue], max_line: int, *, content_rect=None
) -> List[Cue]:
    work = list(cues)
    for _ in range(8):
        out: List[Cue] = []
        for text, start, end in work:
            out.extend(
                _split_cue_to_screen_lines(
                    text, start, end, max_line, content_rect=content_rect
                )
            )
        if all(
            _phrase_fits_one_line(text, max_line, content_rect=content_rect)
            for text, _, _ in out
        ):
            return out
        work = out
    return work


def _finalize_segment_cues(
    cues: List[Cue], max_line: int, *, content_rect=None
) -> List[Cue]:
    merged = _merge_orphan_phrase_cues(cues, max_line)
    merged = _merge_quote_orphan_phrase_cues(merged, max_line)
    merged = _enforce_cue_screen_width(merged, max_line, content_rect=content_rect)
    merged = _eliminate_single_char_cues(merged, max_line)
    if not merged:
        return merged
    from videoaudiotext.subtitle.progressive_timing import (
        floor_degenerate_cue_durations,
        progressive_time_budget_enabled,
    )

    if progressive_time_budget_enabled():
        span_end = float(merged[-1][2])
        merged = floor_degenerate_cue_durations(merged, span_end=span_end)
    return merged


def _eliminate_single_char_cues(cues: List[Cue], max_line: int) -> List[Cue]:
    """时间轴兜底：单字 cue 并入邻条（扩展展示时长），禁止单字独显。"""
    from videoaudiotext.subtitle.quotes import (
        eliminate_single_char_screen_lines,
        is_too_short_screen_line,
        progressive_lines_combine_fits,
        rebalance_short_tail_pair,
        should_merge_quote_orphan_lines,
        should_merge_single_char_orphan_lines,
    )

    work = [(t.strip(), float(s), float(e)) for t, s, e in cues if t.strip()]
    if len(work) <= 1:
        return work

    changed = True
    for _ in range(24):
        if not changed or len(work) <= 1:
            break
        changed = False
        for i in range(len(work)):
            text, t0, t1 = work[i]
            if not is_too_short_screen_line(text):
                continue
            if i > 0:
                ptext, ps, _pe = work[i - 1]
                if should_merge_single_char_orphan_lines(
                    ptext, text, max_line
                ) or should_merge_quote_orphan_lines(ptext, text, max_line):
                    work[i - 1 : i + 1] = [(ptext + text, ps, t1)]
                    changed = True
                    break
                rebalanced = rebalance_short_tail_pair(ptext, text)
                if isinstance(rebalanced, list):
                    work[i - 1 : i + 1] = [(rebalanced[0], ps, t1)]
                    changed = True
                    break
                nl, nr = rebalanced
                if not is_too_short_screen_line(nr):
                    if is_too_short_screen_line(nl) and progressive_lines_combine_fits(
                        nl, nr
                    ):
                        work[i - 1 : i + 1] = [(nl + nr, ps, t1)]
                    elif not is_too_short_screen_line(nl):
                        work[i - 1 : i + 1] = [(nl, ps, _pe), (nr, t0, t1)]
                    changed = True
                    break
            if i + 1 < len(work):
                ntext, _ns, ne = work[i + 1]
                if should_merge_single_char_orphan_lines(
                    text, ntext, max_line
                ) or should_merge_quote_orphan_lines(text, ntext, max_line):
                    work[i : i + 2] = [(text + ntext, t0, ne)]
                    changed = True
                    break
        if not changed:
            flat = [t for t, _, _ in work]
            fixed = eliminate_single_char_screen_lines(flat, max_line)
            if fixed != flat:
                # 屏显已可合并为更少行：按顺序重分配时间轴
                if len(fixed) < len(flat):
                    merged: List[Cue] = []
                    fi = 0
                    for t, s, e in work:
                        if fi >= len(fixed):
                            break
                        if is_too_short_screen_line(t) and merged:
                            pt, ps, _pe = merged[-1]
                            if progressive_lines_combine_fits(pt, t):
                                merged[-1] = (pt + t, ps, e)
                                continue
                        merged.append((fixed[fi], s, e))
                        fi += 1
                    work = merged
                    changed = True
    return work


def _steps_to_absolute_cues(
    steps: list[dict],
    seg_start: float,
    seg_end: float,
    *,
    max_line: int,
    content_rect=None,
) -> List[Cue]:
    """Map segment-relative steps to absolute cue times."""
    gap = max(0.001, SUBTITLE_CUE_SPLIT_GAP_SEC)
    speech_dur = max(0.05, float(seg_end) - float(seg_start))
    out: List[Cue] = []
    for j, step in enumerate(steps):
        phrase = str(step.get("text") or "").strip()
        if not phrase:
            continue
        rel0 = float(step.get("start_time") or step.get("speech_start") or 0.0)
        rel1 = float(step.get("end_time") or step.get("speech_end") or rel0)
        rel0 = max(0.0, min(speech_dur, rel0))
        rel1 = max(rel0 + 0.12, min(speech_dur, rel1))
        t0 = float(seg_start) + rel0
        t1 = float(seg_start) + rel1
        if out and t0 < out[-1][2] + gap - 1e-9:
            t0 = out[-1][2] + gap
        if t1 <= t0:
            from videoaudiotext.subtitle.progressive_timing import min_reading_duration

            t1 = min(float(seg_end), t0 + min_reading_duration(phrase))
        out.append((phrase, t0, t1))
    if out:
        out[-1] = (out[-1][0], out[-1][1], float(seg_end))
    return _finalize_segment_cues(out, max_line, content_rect=content_rect)


def _fallback_screen_line_cues(
    sentence: str,
    seg_start: float,
    seg_end: float,
    *,
    max_line: int,
    wav_path: Path | str | None = None,
    content_rect=None,
) -> List[Cue]:
    """无逗号分步时：按屏显折行拆成多条单行 cue（仍不叠双行）。"""
    from videoaudiotext.subtitle.display import _normalize_subtitle_text
    from videoaudiotext.subtitle.progressive_timing import pack_screen_lines_for_time_budget

    plain = _normalize_subtitle_text(sentence.replace("\n", ""))
    speech_dur = max(0.05, float(seg_end) - float(seg_start))
    lines = pack_screen_lines_for_time_budget(
        plain,
        max_line,
        speech_dur,
        content_rect=content_rect,
    )
    if len(lines) <= 1:
        return _finalize_segment_cues(
            [(plain, float(seg_start), float(seg_end))],
            max_line,
            content_rect=content_rect,
        )

    gap = max(0.001, SUBTITLE_CUE_SPLIT_GAP_SEC)
    out: List[Cue] = []
    for i, line in enumerate(lines):
        if i == 0:
            t0 = float(seg_start)
        else:
            t0 = out[-1][2] + gap
        if i == len(lines) - 1:
            t1 = float(seg_end)
        else:
            boundary = align_wrap_reveal_boundary(
                sentence,
                line,
                lines[i + 1],
                float(seg_start),
                float(seg_end),
                wav_path,
            )
            min_line = _adaptive_min_line_sec(float(seg_end) - float(seg_start), line, lines[i + 1])
            boundary = max(
                t0 + min_line,
                min(float(seg_end) - min_line - gap, boundary),
            )
            t1 = boundary - gap / 2
        if t1 <= t0:
            from videoaudiotext.subtitle.progressive_timing import min_reading_duration

            t1 = min(float(seg_end), t0 + min_reading_duration(line))
        out.append((line, t0, t1))
    if out:
        out[-1] = (out[-1][0], out[-1][1], float(seg_end))
    return _finalize_segment_cues(out, max_line, content_rect=content_rect)


def expand_wrap_reveal_cues(
    cues: List[Cue],
    *,
    sentences: List[str],
    wav_paths: List[Path] | None = None,
    timeline: Sequence | None = None,
    segment_bounds: List[tuple[float, float]] | None = None,
    content_rects: list | None = None,
) -> tuple[List[Cue], list[str]]:
    """
    逐行渐进：每条 cue 仅一行屏显文字，按逗号/短语 + TTS 对齐切换。
    不再把双行折行叠在同一时刻显示。
    """
    if not cues or not wrap_reveal_enabled():
        return list(cues), []

    from videoaudiotext.subtitle.display import _normalize_subtitle_text
    from videoaudiotext.subtitle.text_timing import split_segment_into_steps

    if len(sentences) != len(cues):
        return list(cues), [
            "wrap_reveal: sentences/cues 数量不一致，跳过逐行渐进"
        ]

    out: List[Cue] = []
    warnings: list[str] = []

    for i, (_text, start, end) in enumerate(cues):
        sentence = sentences[i]
        rect = content_rects[i] if content_rects and i < len(content_rects) else None
        max_line, _prefer_single = _progressive_max_line(
            float(start),
            float(end),
            content_rect=rect,
            segment_bounds=segment_bounds,
            content_rects=content_rects,
        )
        speech_dur = max(0.05, float(end) - float(start))
        wav = wav_paths[i] if wav_paths and i < len(wav_paths) else None

        steps = split_segment_into_steps(
            sentence,
            speech_dur,
            wav_path=wav,
            max_chars=max_line,
        )
        if len(steps) <= 1:
            seg_cues = _fallback_screen_line_cues(
                sentence,
                float(start),
                float(end),
                max_line=max_line,
                wav_path=wav,
                content_rect=rect,
            )
        else:
            seg_cues = _steps_to_absolute_cues(
                steps,
                float(start),
                float(end),
                max_line=max_line,
                content_rect=rect,
            )

        if len(seg_cues) > 1:
            warnings.append(f"段{i + 1} 逐行渐进：{len(seg_cues)} 步")
        out.extend(seg_cues)

    return out, warnings


def preview_wrap_reveal_cues(
    sentence: str,
    seg_start: float,
    seg_end: float,
    *,
    content_rect=None,
    wav_path: Path | str | None = None,
) -> List[Cue]:
    """预览：逐行渐进多条 cue 及估算切换时刻。"""
    wavs = [Path(wav_path)] if wav_path else None
    rects = [content_rect] if content_rect is not None else None
    expanded, _ = expand_wrap_reveal_cues(
        [(sentence.strip(), float(seg_start), float(seg_end))],
        sentences=[sentence],
        wav_paths=wavs,
        content_rects=rects,
    )
    return expanded
