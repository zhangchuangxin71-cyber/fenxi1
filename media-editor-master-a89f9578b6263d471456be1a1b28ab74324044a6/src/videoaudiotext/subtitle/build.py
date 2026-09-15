"""Step 4b: One SRT per pipeline segment; balanced inner wrap; breath trim."""

from __future__ import annotations

from pathlib import Path
from typing import List

from videoaudiotext.config import (
    CONTENT_SAFE_SUBTITLES,
    SUBTITLE_BREATH_MIN_SEC,
    SUBTITLE_CHUNK_MAX_CHARS,
    SUBTITLE_CONSTRAINTS_ENABLED,
    SUBTITLE_CUE_MAX_DURATION,
    SUBTITLE_CUE_MIN_CHARS,
    SUBTITLE_CUE_MIN_DURATION,
    SUBTITLE_CUE_SPLIT_GAP_SEC,
    SUBTITLE_CUE_WRAPPED_MAX_DURATION,
    SUBTITLE_MAX_LINES,
    SUBTITLE_MERGE_MIN_CHARS,
    SUBTITLE_MERGE_MIN_DURATION,
    SUBTITLE_MIN_CUE_SEC,
    SUBTITLE_PARALLEL_MAX_DURATION,
    SUBTITLE_PROGRESSIVE,
    subtitle_match_pipeline_segments,
    SUBTITLE_SPLIT_MIN_CHARS,
    scaled_subtitle_cue_max_chars,
    scaled_subtitle_merge_max_chars,
)
from videoaudiotext.subtitle.display import (
    _PREFER_BREAK_AFTER,
    _core_char_len,
    _finalize_cue_displays,
    _format_balance_wrap,
    _is_break_allowed,
    _join_phrase_units,
    _normalize_subtitle_text,
    _split_plain_at_center,
)
from videoaudiotext.subtitle.types import Cue

def _is_substantial_chunk(text: str, min_len: int = 3) -> bool:
    core = "".join(
        ch for ch in text if ch not in _PREFER_BREAK_AFTER + "。！？；.!?; \t"
    )
    return len(core) >= min_len


def _split_at_commas(text: str) -> List[str]:
    """仅在逗号/顿号/分号处切分，用于段内分步字幕；不在字中间截断。"""
    text = _normalize_subtitle_text(text)
    if not text:
        return []

    parts: List[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "，、；":
            chunk = buf.strip()
            if chunk:
                parts.append(chunk.rstrip("，、；").strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())

    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return [text]
    return parts


def _split_phrase_chunks(text: str, max_chars: int = SUBTITLE_CHUNK_MAX_CHARS) -> List[str]:
    """兼容旧逻辑：逗号切分；过长子句才按词边界再切。"""
    parts = _split_at_commas(text)
    if len(parts) <= 1:
        return parts

    cleaned = [p for p in parts if _is_substantial_chunk(p)]
    if len(cleaned) <= 1:
        return [text]

    try:
        import jieba

        final: List[str] = []
        for part in cleaned:
            if len(part) <= max_chars:
                final.append(part)
                continue
            words = list(jieba.cut(part))
            buf = ""
            for word in words:
                if buf and len(buf) + len(word) > max_chars:
                    final.append(buf)
                    buf = word
                else:
                    buf += word
            if buf:
                final.append(buf)
        cleaned = [p for p in final if _is_substantial_chunk(p)]
    except ImportError:
        pass

    return cleaned if len(cleaned) > 1 else [text]


def _progressive_units(raw_text: str) -> List[str]:
    """段内分步单元：有逗号则按逗号切，否则整段一条。"""
    text = _normalize_subtitle_text(raw_text)
    if not text:
        return []
    return _split_at_commas(text)


def _char_weight(text: str) -> int:
    return max(1, len(text.replace("\n", "")))


def split_phrases_for_timing(text: str) -> List[str]:
    """Timeline 同源逗号切分（预览=文本规则，成片=音频优先）。"""
    from videoaudiotext.subtitle.text_timing import split_segment_phrases

    return [p["text"] for p in split_segment_phrases(_normalize_subtitle_text(text))]


def phrase_char_weight(text: str) -> int:
    from videoaudiotext.subtitle.text_timing import get_phonetic_weight

    return max(1, int(round(get_phonetic_weight(text))))

def _split_text_into_parts(
    text: str,
    *,
    max_chars: int | None = None,
    max_screen: int | None = None,
) -> List[str]:
    if max_chars is None:
        max_chars = scaled_subtitle_cue_max_chars()
    """将过长文本拆成多条字幕用的短语（优先词边界）。"""
    text = _normalize_subtitle_text(text)
    if not text:
        return []
    screen = max_screen or (max_chars * SUBTITLE_MAX_LINES)
    if _core_char_len(text) <= screen:
        return [text]

    parts: List[str] = []
    remaining = text
    while remaining and _core_char_len(remaining) > screen:
        chunk = remaining
        cut: int | None = None
        for pos in range(min(max_chars + 2, len(remaining)), 0, -1):
            if _is_break_allowed(remaining, pos):
                cut = pos
                break
        if cut is None:
            cut = min(max_chars, len(remaining))
        head = remaining[:cut].strip()
        if head:
            parts.append(head)
        remaining = remaining[cut:].strip()

    if remaining:
        parts.append(remaining)
    return parts if parts else [text]


def _distribute_time_proportional(
    start: float,
    end: float,
    parts: List[str],
    *,
    min_duration: float = SUBTITLE_CUE_MIN_DURATION,
) -> List[Cue]:
    """按拟音权重比例切分时间轴。"""
    from videoaudiotext.subtitle.text_timing import get_phonetic_weight

    if not parts:
        return []
    if len(parts) == 1:
        return [(parts[0], start, end)]

    dur = max(min_duration, end - start)
    weights = [max(0.5, get_phonetic_weight(p)) for p in parts]
    total_w = sum(weights)
    gap = SUBTITLE_CUE_SPLIT_GAP_SEC

    if len(parts) == 2:
        w1 = weights[0]
        mid = start + dur * (w1 / total_w)
        mid = max(start + SUBTITLE_BREATH_MIN_SEC, min(end - SUBTITLE_BREATH_MIN_SEC, mid))
        return [
            (parts[0], start, max(start + SUBTITLE_BREATH_MIN_SEC, mid - gap / 2)),
            (parts[1], mid, end),
        ]

    cues: List[Cue] = []
    elapsed = 0.0
    for i, part in enumerate(parts):
        t0 = start + dur * (elapsed / total_w)
        if i == len(parts) - 1:
            t1 = end
        else:
            elapsed += weights[i]
            t1 = start + dur * (elapsed / total_w)
            t1 = max(t0 + SUBTITLE_BREATH_MIN_SEC, t1 - gap / 2)
        cues.append((part, t0, t1))
    return cues
def _merge_two_cue_texts(a: str, b: str) -> str:
    plain_a = _normalize_subtitle_text(a.replace("\n", ""))
    plain_b = _normalize_subtitle_text(b.replace("\n", ""))
    return _join_phrase_units([plain_a, plain_b])


def _cues_are_contiguous(prev: Cue, nxt: Cue, tol: float = 0.12) -> bool:
    return abs(prev[2] - nxt[1]) <= tol


def _swallow_flash_cues(cues: List[Cue]) -> List[Cue]:
    """
    防闪现：duration < 1.2s 绝不单独成条。
    优先并回上一条（治愈系慢节奏），否则并到下一条。
    """
    if not cues:
        return []

    work = [
        (_normalize_subtitle_text(t.replace("\n", "")), s, e) for t, s, e in cues
    ]
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(work):
            text, start, end = work[i]
            if (end - start) >= SUBTITLE_CUE_MIN_DURATION:
                i += 1
                continue

            if i > 0 and _cues_are_contiguous(work[i - 1], (text, start, end)):
                ptext, ps, pe = work[i - 1]
                work[i - 1 : i + 1] = [
                    (_merge_two_cue_texts(ptext, text), ps, end)
                ]
                changed = True
                continue

            if i + 1 < len(work) and _cues_are_contiguous(
                (text, start, end), work[i + 1]
            ):
                ntext, ns, ne = work[i + 1]
                work[i : i + 2] = [(_merge_two_cue_texts(text, ntext), start, ne)]
                changed = True
                continue

            i += 1

    return work


def _merge_parallel_short_cues(cues: List[Cue]) -> List[Cue]:
    """
    排比/对称短句合并：如「认真品尝三餐」(1.3s) +「好好感受烟火」(1.4s)
    → 一条稳定字幕，避免 1.3s 级别的细碎跳动。
    """
    if len(cues) < 2:
        return list(cues)

    out: List[Cue] = []
    i = 0
    while i < len(cues):
        if i + 1 < len(cues):
            t1, s1, e1 = cues[i]
            t2, s2, e2 = cues[i + 1]
            c1, c2 = _core_char_len(t1), _core_char_len(t2)
            d1, d2 = e1 - s1, e2 - s2
            combined_core = c1 + c2
            symmetric = (
                _cues_are_contiguous((t1, s1, e1), (t2, s2, e2))
                and 4 <= c1 <= 10
                and 4 <= c2 <= 10
                and abs(c1 - c2) <= 3
                and d1 <= SUBTITLE_PARALLEL_MAX_DURATION
                and d2 <= SUBTITLE_PARALLEL_MAX_DURATION
                and combined_core
                <= scaled_subtitle_cue_max_chars() * SUBTITLE_MAX_LINES
                and (e2 - s1) <= SUBTITLE_CUE_MAX_DURATION + 0.4
            )
            if symmetric:
                out.append(
                    (_merge_two_cue_texts(t1, t2), s1, e2)
                )
                i += 2
                continue

        out.append(cues[i])
        i += 1

    return out


def _post_filter_one_cue(text: str, start: float, end: float) -> List[Cue]:
    """
    A) 时长 >3s 且字数较多 → 语义中切 + 按字数比例切时间轴；
    B) 时长略超 3s 但双行可放下（≤12字/行）→ 仅折行，不拆时间轴；
    C) 否则保留。
    """
    plain = _normalize_subtitle_text(text.replace("\n", ""))
    if not plain:
        return []

    dur = end - start
    core = _core_char_len(plain)

    if dur > SUBTITLE_CUE_MAX_DURATION:
        cue_max = scaled_subtitle_cue_max_chars()
        fits_wrapped_screen = core <= cue_max * SUBTITLE_MAX_LINES
        wrap_only = (
            fits_wrapped_screen
            and core > cue_max
            and dur <= SUBTITLE_CUE_WRAPPED_MAX_DURATION
        )
        if wrap_only:
            return [(plain, start, end)]

        if core >= SUBTITLE_SPLIT_MIN_CHARS or dur > SUBTITLE_CUE_MAX_DURATION + 0.8:
            from videoaudiotext.subtitle.text_timing import split_phrase_at_jieba_midpoint

            left, right = None, None
            split_pair = split_phrase_at_jieba_midpoint(plain)
            if split_pair:
                left, right = split_pair
            if not (
                left
                and right
                and _core_char_len(left) >= 4
                and _core_char_len(right) >= 4
            ):
                left, right = _split_plain_at_center(plain)
            if (
                left
                and right
                and _core_char_len(left) >= 4
                and _core_char_len(right) >= 4
            ):
                split = _distribute_time_proportional(start, end, [left, right])
                out: List[Cue] = []
                for part, s, e in split:
                    out.extend(_post_filter_one_cue(part, s, e))
                return out

    return [(plain, start, end)]


def _post_filter_cues(cues: List[Cue]) -> List[Cue]:
    out: List[Cue] = []
    for text, start, end in cues:
        out.extend(_post_filter_one_cue(text, start, end))
    return out
def _resolve_segment_video_durations(
    sentences: List[str],
    durations: List[float],
    gaps: List[float] | None,
    *,
    timeline: List | None = None,
    segment_video_durations: List[float] | None = None,
) -> List[float]:
    if segment_video_durations is not None:
        return segment_video_durations
    from videoaudiotext.audio.gaps import (
        compute_absolute_timeline,
        compute_sentence_gaps,
        timeline_video_content_durations,
    )

    if timeline is None:
        if gaps is None:
            gaps = compute_sentence_gaps(sentences)
        timeline = compute_absolute_timeline(sentences, durations)
    return timeline_video_content_durations(timeline)


def _subtitle_layout_context(
    segment_video_durations: List[float] | None,
    media_paths: List[Path] | None,
) -> tuple[List[tuple[float, float]] | None, list | None]:
    if not CONTENT_SAFE_SUBTITLES or not media_paths or not segment_video_durations:
        return None, None
    from videoaudiotext.subtitle.safe_area import content_rects_for_media_paths, segment_time_bounds_for_output

    return (
        segment_time_bounds_for_output(segment_video_durations),
        content_rects_for_media_paths(media_paths),
    )
def optimize_subtitle_pipeline(
    raw_cues: List[Cue],
    *,
    timeline_locked: bool = False,
    content_rect=None,
    segment_bounds: List[tuple[float, float]] | None = None,
    content_rects: list | None = None,
) -> List[Cue]:
    """
    Segment 内 raw steps → Post-Filter。

    timeline_locked=True（Timeline Master 同源）：
    只折行展示，不吞并闪现、不排比合并、不按字数重切时间轴。
    """
    if not raw_cues:
        return []

    if timeline_locked:
        return _finalize_cue_displays(
            raw_cues,
            content_rect=content_rect,
            segment_bounds=segment_bounds,
            content_rects=content_rects,
        )

    work = [
        (_normalize_subtitle_text(t.replace("\n", "")), s, e) for t, s, e in raw_cues
    ]

    for _ in range(max(4, len(work) + 1)):
        prev_n = len(work)
        work = _swallow_flash_cues(work)
        work = _merge_parallel_short_cues(work)
        if len(work) == prev_n and all(
            (e - s) >= SUBTITLE_CUE_MIN_DURATION for _, s, e in work
        ):
            break

    work = _post_filter_cues(work)

    for _ in range(4):
        prev = len(work)
        work = _swallow_flash_cues(work)
        work = _merge_parallel_short_cues(work)
        work = _post_filter_cues(work)
        if len(work) == prev:
            break

    work = _swallow_flash_cues(work)
    return _finalize_cue_displays(
        work,
        content_rect=content_rect,
        segment_bounds=segment_bounds,
        content_rects=content_rects,
    )


def apply_subtitle_constraints(
    cues: List[Cue],
    *,
    timeline_locked: bool = False,
) -> List[Cue]:
    """四大约束 Post-Filter（对外入口）。"""
    return optimize_subtitle_pipeline(cues, timeline_locked=timeline_locked)


def _cue_is_stable(
    start: float,
    end: float,
    text: str,
    *,
    min_duration: float,
    min_chars: int,
) -> bool:
    return (end - start) >= min_duration and _core_char_len(text) >= min_chars


def _is_strong_stable(
    start: float,
    end: float,
    text: str,
    *,
    min_duration: float,
    min_chars: int,
) -> bool:
    """达标且字数/时长足够「单独成条」，不再与后文碎片合并。"""
    if not _cue_is_stable(start, end, text, min_duration=min_duration, min_chars=min_chars):
        return False
    core = _core_char_len(text)
    dur = end - start
    return core >= max(min_chars + 2, 10) or dur >= max(min_duration + 0.5, 2.5)


def merge_progressive_cues(
    raw_cues: List[Cue],
    *,
    min_duration: float = SUBTITLE_MERGE_MIN_DURATION,
    min_chars: int = SUBTITLE_MERGE_MIN_CHARS,
    max_chars: int | None = None,
    max_lines: int = SUBTITLE_MAX_LINES,
) -> List[Cue]:
    """
    动态合并：已达标（时长≥min、字数≥min）的条目单独成条；
    连续未达标片段向前滚动合并，再均衡双行折行。
    """
    if not raw_cues:
        return []

    if max_chars is None:
        max_chars = scaled_subtitle_merge_max_chars()
    max_screen_chars = max_chars * max_lines
    optimized: List[Cue] = []
    i = 0
    n = len(raw_cues)

    while i < n:
        text, start, end = raw_cues[i]
        unit = _normalize_subtitle_text(text)

        if _is_strong_stable(
            start, end, unit, min_duration=min_duration, min_chars=min_chars
        ):
            optimized.append(
                (
                    _format_balance_wrap(unit, max_line=max_chars, max_lines=max_lines),
                    start,
                    end,
                )
            )
            i += 1
            continue

        buf_parts = [unit]
        buf_start, buf_end = start, end
        i += 1

        while i < n:
            ntext, ns, ne = raw_cues[i]
            nunit = _normalize_subtitle_text(ntext)
            merged_plain = _join_phrase_units(buf_parts + [nunit])
            if _core_char_len(merged_plain) > max_screen_chars:
                break

            buf_plain = _join_phrase_units(buf_parts)
            buf_core = _core_char_len(buf_plain)
            n_core = _core_char_len(nunit)

            if (
                _is_strong_stable(ns, ne, nunit, min_duration=min_duration, min_chars=min_chars)
                and n_core >= 14
                and buf_core < 10
            ):
                break

            buf_parts.append(nunit)
            buf_end = ne
            i += 1

            if _is_strong_stable(
                buf_start,
                buf_end,
                _join_phrase_units(buf_parts),
                min_duration=min_duration,
                min_chars=min_chars,
            ):
                break

        plain = _join_phrase_units(buf_parts)
        optimized.append(
            (
                _format_balance_wrap(plain, max_line=max_chars, max_lines=max_lines),
                buf_start,
                buf_end,
            )
        )

    return optimized


def _raw_cues_for_segment(
    raw_text: str,
    seg_start: float,
    seg_end: float,
) -> List[Cue]:
    """生成 Segment 内 raw 分步（Timeline Master / 拟音权重，未洗数）。"""
    from videoaudiotext.audio.timeline import build_segment_timeline, timeline_raw_cues

    speech = max(0.01, seg_end - seg_start)
    tl = build_segment_timeline(1, raw_text, speech)
    return timeline_raw_cues(tl, seg_start)


def _progressive_cues_for_segment(
    raw_text: str,
    seg_start: float,
    seg_end: float,
) -> List[Cue]:
    """单段预览：Timeline raw → 保护通道（只折行）。"""
    raw = _raw_cues_for_segment(raw_text, seg_start, seg_end)
    if SUBTITLE_CONSTRAINTS_ENABLED:
        return optimize_subtitle_pipeline(raw, timeline_locked=True)
    return _finalize_cue_displays(raw)


def preview_progressive_cues(sentence: str, duration: float) -> List[tuple[str, float, float]]:
    """预览段内分步字幕（文本 + 起止秒）。"""
    if subtitle_match_pipeline_segments():
        text = _normalize_subtitle_text(sentence)
        end = max(duration, SUBTITLE_MIN_CUE_SEC)
        return [(text, 0.0, end)]
    cues = _progressive_cues_for_segment(
        sentence, 0.0, max(duration, SUBTITLE_MIN_CUE_SEC)
    )
    return apply_subtitle_breath(
        cues,
        min_duration=SUBTITLE_BREATH_MIN_SEC,
    )


def apply_subtitle_breath(
    cues: List[Cue],
    *,
    min_duration: float = SUBTITLE_BREATH_MIN_SEC,
) -> List[Cue]:
    """保证每条 ≥ min_duration；不提前收字幕（音后留白由 Timeline post_hold 负责）。"""
    gap = max(0.001, SUBTITLE_CUE_SPLIT_GAP_SEC)
    out: List[Cue] = []
    for i, (text, start, end) in enumerate(cues):
        end_adj = max(start + min_duration, end)
        if i + 1 < len(cues):
            next_start = cues[i + 1][1]
            end_adj = min(end_adj, next_start - gap)
        out.append((text, start, end_adj))
    return out


def enforce_cue_non_overlap(
    cues: List[Cue],
    *,
    min_gap: float | None = None,
    min_duration: float | None = None,
) -> tuple[List[Cue], list[str]]:
    """消除相邻字幕时间重叠，保证后一条 start ≥ 前一条 end + min_gap。"""
    if not cues:
        return [], []
    gap = SUBTITLE_CUE_SPLIT_GAP_SEC if min_gap is None else float(min_gap)
    min_dur = SUBTITLE_MIN_CUE_SEC if min_duration is None else float(min_duration)
    warnings: list[str] = []
    out: List[Cue] = []
    prev_end = 0.0
    for i, (text, start, end) in enumerate(cues, start=1):
        s, e = float(start), float(end)
        if out and s < prev_end + gap - 1e-9:
            new_s = prev_end + gap
            warnings.append(
                f"cue {i}: 与上条重叠，start {s:.3f}s → {new_s:.3f}s"
            )
            s = new_s
        if e <= s:
            e = s + min_dur
            warnings.append(f"cue {i}: end<=start，已延长到 {e:.3f}s")
        out.append((text, s, e))
        prev_end = e
    return out, warnings


def collect_cue_quality_warnings(cues: List[Cue]) -> list[str]:
    """过长字幕条等质量提示（不修改时间轴）。"""
    warnings: list[str] = []
    for i, (text, start, end) in enumerate(cues, start=1):
        dur = float(end) - float(start)
        if dur > SUBTITLE_CUE_MAX_DURATION + 0.05:
            sample = text.replace("\n", " ")[:20]
            warnings.append(
                f"cue {i}: 单条 {dur:.1f}s > {SUBTITLE_CUE_MAX_DURATION}s"
                f"（{sample}…，建议缩短或启用分步）"
            )
    return warnings


def strip_trailing_em_dash_at_segment_ends(
    cues: List[Cue],
    *,
    speech_segment_bounds: List[tuple[float, float]] | None,
) -> List[Cue]:
    """Drop trailing ——/— on the last cue of each segment when a next segment follows."""
    if not cues or not speech_segment_bounds or len(speech_segment_bounds) < 2:
        return list(cues)

    from videoaudiotext.subtitle.display import strip_trailing_em_dash
    from videoaudiotext.subtitle.safe_area import segment_index_for_cue

    bounds = list(speech_segment_bounds)
    seg_last_idx: dict[int, int] = {}
    for i, (_text, start, end) in enumerate(cues):
        si = segment_index_for_cue(float(start), float(end), bounds)
        prev = seg_last_idx.get(si)
        if prev is None or float(end) >= float(cues[prev][2]):
            seg_last_idx[si] = i

    work = list(cues)
    last_segment = len(bounds) - 1
    for si, cue_i in seg_last_idx.items():
        if si >= last_segment:
            continue
        text, t0, t1 = work[cue_i]
        cleaned = strip_trailing_em_dash(text)
        if cleaned and cleaned != text:
            work[cue_i] = (cleaned, t0, t1)
    return work


def finalize_export_cues(
    cues: List[Cue],
    *,
    speech_segment_bounds: List[tuple[float, float]] | None = None,
) -> tuple[List[Cue], list[str]]:
    """导出前：CPS 护栏 + 去重叠 + 收集质量警告。"""
    from videoaudiotext.subtitle.cps import enforce_cps_limits
    from videoaudiotext.subtitle.wrap_reveal import wrap_reveal_enabled

    work = strip_trailing_em_dash_at_segment_ends(
        cues,
        speech_segment_bounds=speech_segment_bounds,
    )
    work, cps_warn = enforce_cps_limits(
        work,
        speech_segment_bounds=speech_segment_bounds,
        allow_merge=not wrap_reveal_enabled(),
    )
    fixed, overlap_warn = enforce_cue_non_overlap(work)
    return fixed, cps_warn + overlap_warn + collect_cue_quality_warnings(fixed)


def collect_segment_level_cues(
    sentences: List[str],
    durations: List[float],
    *,
    speed: float = 1.0,
) -> List[Cue]:
    """One cue per pipeline segment (speech span on the master timeline)."""
    from videoaudiotext.audio.gaps import compute_absolute_timeline

    if len(sentences) != len(durations):
        raise ValueError("sentences and durations length mismatch")
    timeline = compute_absolute_timeline(sentences, durations, speed=speed)
    return [
        (str(entry.text).strip(), float(entry.speech_start), float(entry.speech_end))
        for entry in timeline
    ]


def collect_all_cues(
    sentences: List[str],
    durations: List[float],
    gaps: List[float] | None = None,
    *,
    segment_video_durations: List[float] | None = None,
    media_paths: List[Path] | None = None,
    shot_boundaries: List[bool] | None = None,
    timeline: List | None = None,
) -> List[Cue]:
    if len(sentences) != len(durations):
        raise ValueError("sentences and durations length mismatch")
    if gaps is None:
        gaps = [0.0] * len(sentences)
    if len(gaps) != len(sentences):
        raise ValueError("gaps and sentences length mismatch")

    from videoaudiotext.audio.gaps import (
        compute_absolute_timeline,
        timeline_video_content_durations,
        xfade_at_boundary_from_shots,
    )

    if timeline is None:
        xfade_at = None
        if shot_boundaries is not None:
            xfade_at = xfade_at_boundary_from_shots(shot_boundaries, len(sentences))
        timeline = compute_absolute_timeline(
            sentences,
            durations,
            xfade_at_boundary=xfade_at,
        )

    if segment_video_durations is None:
        segment_video_durations = timeline_video_content_durations(timeline)
    if len(segment_video_durations) != len(sentences):
        raise ValueError("segment_video_durations and sentences length mismatch")

    from videoaudiotext.audio.timeline import (
        load_timelines,
        timeline_raw_cues,
        timelines_matching_sentences,
    )

    timelines = timelines_matching_sentences(load_timelines(), sentences)
    bounds, content_rects = _subtitle_layout_context(segment_video_durations, media_paths)

    all_cues: List[Cue] = []
    for i, entry in enumerate(timeline, start=1):
        sentence = entry.text
        dur = float(entry.speech_duration)
        seg_start = float(entry.speech_start)
        seg_end = float(entry.speech_end)
        seg_rect = (
            content_rects[i - 1]
            if content_rects and i - 1 < len(content_rects)
            else None
        )
        vid_dur = float(segment_video_durations[i - 1])
        key = str(i)
        tl = timelines.get(key)
        from videoaudiotext.subtitle.text_timing import progressive_split_eligible

        use_progressive = (
            SUBTITLE_PROGRESSIVE
            and not subtitle_match_pipeline_segments()
            and progressive_split_eligible(sentence)
        )
        if use_progressive:
            if key in timelines:
                seg_cues = timeline_raw_cues(
                    timelines[key],
                    seg_start,
                    clip_duration=vid_dur,
                    speech_duration=dur,
                )
            else:
                seg_cues = _raw_cues_for_segment(sentence, seg_start, seg_end)
            if len(seg_cues) <= 1:
                use_progressive = False
        if use_progressive:
            if SUBTITLE_CONSTRAINTS_ENABLED:
                seg_cues = optimize_subtitle_pipeline(
                    seg_cues,
                    timeline_locked=True,
                    content_rect=seg_rect,
                )
            else:
                seg_cues = _finalize_cue_displays(seg_cues, content_rect=seg_rect)
            all_cues.extend(seg_cues)
        else:
            seg_cues = [(sentence.strip(), seg_start, seg_end)]
            from videoaudiotext.subtitle.wrap_reveal import wrap_reveal_enabled

            if wrap_reveal_enabled():
                all_cues.extend(seg_cues)
            elif SUBTITLE_CONSTRAINTS_ENABLED:
                seg_cues = optimize_subtitle_pipeline(
                    seg_cues,
                    timeline_locked=True,
                    content_rect=seg_rect,
                )
                all_cues.extend(seg_cues)
            else:
                seg_cues = _finalize_cue_displays(seg_cues, content_rect=seg_rect)
                all_cues.extend(seg_cues)

    all_cues, _ = enforce_cue_non_overlap(all_cues)
    return all_cues

# Re-exports for backward compatibility
from videoaudiotext.subtitle.display import (  # noqa: E402, F401
    _ass_enforce_max_chars_per_line,
    _clamp_display_lines,
    _comma_split_valid,
    _content_rect_for_cue,
    _core_char_len,
    _display_text_for_cue,
    _finalize_cue_displays,
    _format_balance_wrap,
    _join_phrase_units,
    _layout_for_cue,
    _max_line_for_cue,
    _normalize_subtitle_text,
    _PREFER_BREAK_AFTER,
    _PREFER_BREAK_BEFORE,
    _FORBID_BREAK_BEFORE,
    _split_plain_at_center,
    _wrap_at_comma_display,
    format_cue_display,
    subtitle_display_for_sentence,
    SubtitleLayoutContext,
    universal_subtitle_cleaner,
    wrap_text_for_screen,
)
from videoaudiotext.subtitle.export import (  # noqa: E402, F401
    _ass_event_text,
    _escape_sub_path,
    _format_srt_timestamp,
    _parse_srt_time,
    _prepare_export_cues,
    _video_duration_seconds,
    _write_ass_file,
    ass_play_resolution,
    build_ass,
    build_ass_from_srt,
    build_srt,
    build_subtitles,
    install_external_srt,
    parse_srt_file,
    resolve_embed_subtitle_path,
    seconds_to_ass_time,
    subtitle_force_style,
    subtitle_video_filter,
    sync_ass_to_video,
    validate_and_fix_srt,
    write_srt_file,
)
