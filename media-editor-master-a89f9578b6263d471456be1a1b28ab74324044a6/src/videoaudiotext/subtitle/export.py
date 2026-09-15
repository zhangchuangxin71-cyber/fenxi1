"""SRT/ASS export and FFmpeg subtitle embedding."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import List, Sequence

from videoaudiotext.config import (
    FONTS_DIR,
    get_output_height,
    get_output_width,
    scaled_subtitle_ass_font_size,
    scaled_subtitle_frame_lr_margin,
    scaled_subtitle_margin_bottom,
    scaled_subtitle_margin_lr,
    scaled_subtitle_srt_font_size,
    SUBTITLE_ASS,
    SUBTITLE_BREATH_MIN_SEC,
    SUBTITLE_CUE_MAX_DURATION,
    SUBTITLE_CUE_MIN_DURATION,
    SUBTITLE_CUE_SPLIT_GAP_SEC,
    get_subtitle_font_name,
    SUBTITLE_MIN_CUE_SEC,
    SUBTITLE_SCALE_X,
    SUBTITLE_SCALE_Y,
    SUBTITLE_SRT,
)
from videoaudiotext.config.subtitle import _iter_font_files
from videoaudiotext.subtitle.display import (
    _clamp_display_lines,
)
from videoaudiotext.subtitle.types import Cue

def _format_srt_timestamp(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def seconds_to_ass_time(seconds: float) -> str:
    cs = int(round(max(0.0, seconds) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
def write_srt_file(cues: List[Cue], out_path: Path) -> None:
    blocks: List[str] = []
    for idx, (text, start, end) in enumerate(cues, start=1):
        blocks.append(
            f"{idx}\n"
            f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n"
            f"{text}"
        )
    out_path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _speech_bounds_from_timeline(timeline) -> list[tuple[float, float]] | None:
    if not timeline:
        return None
    bounds: list[tuple[float, float]] = []
    for entry in timeline:
        bounds.append((float(entry.speech_start), float(entry.speech_end)))
    return bounds or None


def _prepare_export_cues(
    cues: List[Cue],
    *,
    sentences: List[str] | None = None,
    wav_paths: List[Path] | None = None,
    timeline: List | None = None,
    apply_breath: bool = True,
    try_asr_align: bool = True,
    segment_bounds: List[tuple[float, float]] | None = None,
    content_rects: list | None = None,
) -> tuple[List[Cue], list[str]]:
    """导出前统一处理：折行渐进、breath、段内音频微调、可选 ASR、去重叠与质量警告。"""
    from videoaudiotext.config import subtitle_preserve_tts_timing
    from videoaudiotext.subtitle.build import (
        apply_subtitle_breath,
        finalize_export_cues,
        _finalize_cue_displays,
    )
    from videoaudiotext.subtitle.wrap_reveal import (
        expand_wrap_reveal_cues,
        wrap_reveal_enabled,
    )

    quality_warn: list[str] = []

    if wrap_reveal_enabled() and sentences:
        cues, wr_warn = expand_wrap_reveal_cues(
            cues,
            sentences=sentences,
            wav_paths=wav_paths,
            timeline=timeline,
            segment_bounds=segment_bounds,
            content_rects=content_rects,
        )
        quality_warn.extend(wr_warn)
        cues = _finalize_cue_displays(
            cues,
            segment_bounds=segment_bounds,
            content_rects=content_rects,
        )

    if apply_breath and not subtitle_preserve_tts_timing():
        cues = apply_subtitle_breath(
            cues,
            min_duration=SUBTITLE_BREATH_MIN_SEC,
        )

    if (
        wav_paths
        and sentences
        and timeline
        and not wrap_reveal_enabled()
    ):
        from videoaudiotext.subtitle.progressive_refine import (
            refine_progressive_cues_from_audio,
        )

        cues, nudge_warn = refine_progressive_cues_from_audio(
            cues,
            wav_paths=wav_paths,
            sentences=sentences,
            timeline=timeline,
        )
        quality_warn.extend(nudge_warn)

    if (
        try_asr_align
        and wav_paths
        and sentences
        and not subtitle_preserve_tts_timing()
        and not wrap_reveal_enabled()
        and len(wav_paths) == len(cues)
    ):
        from videoaudiotext.subtitle.align import align_cues_with_audio

        cues = align_cues_with_audio(
            cues, wav_paths, reference_texts=sentences
        )
    speech_bounds = _speech_bounds_from_timeline(timeline)
    if speech_bounds is None and len(cues) == len(sentences or []):
        speech_bounds = [(float(s), float(e)) for _, s, e in cues]
    fixed, export_warn = finalize_export_cues(
        cues,
        speech_segment_bounds=speech_bounds,
    )
    return fixed, quality_warn + export_warn


def build_subtitles(
    sentences: List[str],
    durations: List[float],
    srt_path: Path,
    ass_path: Path,
    gaps: List[float] | None = None,
    *,
    segment_video_durations: List[float] | None = None,
    wav_paths: List[Path] | None = None,
    media_paths: List[Path] | None = None,
    shot_boundaries: List[bool] | None = None,
    timeline: List | None = None,
    apply_breath: bool = True,
    try_asr_align: bool = True,
) -> List[Cue]:
    """收集 cue 一次，导出 SRT + ASS（共用 _prepare_export_cues）。"""
    from videoaudiotext.subtitle.build import (
        collect_all_cues,
        _resolve_segment_video_durations,
        _subtitle_layout_context,
    )

    seg_durs = _resolve_segment_video_durations(
        sentences,
        durations,
        gaps,
        timeline=timeline,
        segment_video_durations=segment_video_durations,
    )
    bounds, content_rects = _subtitle_layout_context(seg_durs, media_paths)
    cues = collect_all_cues(
        sentences,
        durations,
        gaps,
        segment_video_durations=seg_durs,
        media_paths=media_paths,
        shot_boundaries=shot_boundaries,
        timeline=timeline,
    )
    cues, quality_warn = _prepare_export_cues(
        cues,
        sentences=sentences,
        wav_paths=wav_paths,
        timeline=timeline,
        apply_breath=apply_breath,
        try_asr_align=try_asr_align,
        segment_bounds=bounds,
        content_rects=content_rects,
    )
    for w in quality_warn:
        print(f"  [subtitle] {w}", flush=True)
    write_srt_file(cues, srt_path)
    _write_ass_file(cues, ass_path, bounds=bounds, content_rects=content_rects)
    return cues


def build_segment_subtitles(
    sentences: List[str],
    durations: List[float],
    srt_path: Path,
    ass_path: Path | None = None,
    gaps: List[float] | None = None,
    *,
    speed: float = 1.0,
) -> List[Cue]:
    """
    Export segment-level SRT (and optionally ASS) for one cue per audio segment.

    Audio stage only needs SRT timing; ASS styling is generated at preview/compose.
    Display line splitting (wrap-reveal) is deferred to compose.
    """
    from videoaudiotext.audio.gaps import segment_video_durations
    from videoaudiotext.subtitle.build import (
        _subtitle_layout_context,
        collect_segment_level_cues,
    )

    cues = collect_segment_level_cues(sentences, durations, speed=speed)
    write_srt_file(cues, srt_path)
    if ass_path is not None:
        if gaps is None:
            from videoaudiotext.audio.gaps import compute_sentence_gaps

            gaps = compute_sentence_gaps(sentences, speed=speed)
        seg_durs = segment_video_durations(durations, gaps)
        bounds, content_rects = _subtitle_layout_context(seg_durs, media_paths=None)
        _write_ass_file(cues, ass_path, bounds=bounds, content_rects=content_rects)
    return cues


def build_display_ass_for_compose(
    srt_path: Path,
    ass_path: Path,
    *,
    segments: List[str],
    durations: List[float],
    gaps: List[float],
    wav_paths: List[Path] | None = None,
    media_paths: List[Path] | None = None,
) -> Path:
    """
    Expand segment-level SRT into display cues and write burn-in ASS.

    Used at compose time; segment timing comes from the audio deliverable SRT.
    """
    from videoaudiotext.audio.gaps import compute_absolute_timeline, segment_video_durations
    from videoaudiotext.subtitle.build import _subtitle_layout_context

    cues = parse_srt_file(srt_path)
    if not cues:
        raise ValueError("SRT has no cues")

    if len(cues) != len(segments):
        return build_ass_from_srt(srt_path, ass_path, update_srt=False)

    seg_durs = segment_video_durations(durations, gaps)
    bounds, content_rects = _subtitle_layout_context(seg_durs, media_paths)
    timeline = compute_absolute_timeline(segments, durations)
    display_cues, quality_warn = _prepare_export_cues(
        cues,
        sentences=segments,
        wav_paths=wav_paths,
        timeline=timeline,
        apply_breath=False,
        try_asr_align=False,
        segment_bounds=bounds,
        content_rects=content_rects,
    )
    for w in quality_warn:
        print(f"  [subtitle] {w}", flush=True)
    return _write_ass_file(
        display_cues,
        ass_path,
        bounds=bounds,
        content_rects=content_rects,
    )


def build_srt(
    sentences: List[str],
    durations: List[float],
    out_path: Path,
    gaps: List[float] | None = None,
    *,
    segment_video_durations: List[float] | None = None,
    wav_paths: List[Path] | None = None,
    media_paths: List[Path] | None = None,
    shot_boundaries: List[bool] | None = None,
    timeline: List | None = None,
    apply_breath: bool = True,
    try_asr_align: bool = True,
    cues: List[Cue] | None = None,
) -> List[Cue]:
    from videoaudiotext.subtitle.build import (
        collect_all_cues,
        _resolve_segment_video_durations,
        _subtitle_layout_context,
    )

    seg_durs = _resolve_segment_video_durations(
        sentences,
        durations,
        gaps,
        timeline=timeline,
        segment_video_durations=segment_video_durations,
    )
    bounds, content_rects = _subtitle_layout_context(seg_durs, media_paths)
    if cues is None:
        cues = collect_all_cues(
            sentences,
            durations,
            gaps,
            segment_video_durations=seg_durs,
            media_paths=media_paths,
            shot_boundaries=shot_boundaries,
            timeline=timeline,
        )
    cues, quality_warn = _prepare_export_cues(
        cues,
        sentences=sentences,
        wav_paths=wav_paths,
        timeline=timeline,
        apply_breath=apply_breath,
        try_asr_align=try_asr_align,
        segment_bounds=bounds,
        content_rects=content_rects,
    )
    for w in quality_warn:
        print(f"  [subtitle] {w}", flush=True)
    write_srt_file(cues, out_path)
    return cues
def _ass_event_text(
    text: str,
    *,
    margin_prefix: str = "",
    font_size: int | None = None,
) -> str:
    """ASS 硬换行 + \\q2；单行用 Style 字号，双行可 \\fs 略放大。"""
    body = _clamp_display_lines(text.replace("\\N", "\n")).replace("\n", "\\N")
    line_count = body.count("\\N") + 1 if body.strip() else 1
    if font_size is None:
        from videoaudiotext.config.subtitle import ass_font_size_for_line_count

        font_size = ass_font_size_for_line_count(line_count)
    fs_tag = f"\\fs{font_size}" if font_size else ""
    if margin_prefix:
        if margin_prefix.endswith("}"):
            prefix = margin_prefix[:-1] + fs_tag + "}"
        else:
            prefix = margin_prefix + fs_tag
    else:
        prefix = f"{{\\q2{fs_tag}}}"
    return f"{prefix}{body}"
def _write_ass_file(
    cues: List[Cue],
    out_path: Path,
    *,
    bounds=None,
    content_rects=None,
) -> Path:
    use_content_safe = bool(content_rects and bounds)
    ass_fs = scaled_subtitle_ass_font_size()
    from videoaudiotext.subtitle.wrap_reveal import wrap_reveal_enabled

    if wrap_reveal_enabled():
        from videoaudiotext.config.subtitle import scaled_subtitle_progressive_frame_lr_margin

        margin_lr = scaled_subtitle_progressive_frame_lr_margin()
    else:
        margin_lr = scaled_subtitle_frame_lr_margin()
    margin_b = 0 if use_content_safe else scaled_subtitle_margin_bottom()
    style = (
        f"Style: Default,{get_subtitle_font_name()},{ass_fs},"
        "&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "0,0,0,0,"
        f"{SUBTITLE_SCALE_X},{SUBTITLE_SCALE_Y},"
        "0,0,1,2,0,"
        f"2,{margin_lr},{margin_lr},{margin_b},1"
    )
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {get_output_width()}
PlayResY: {get_output_height()}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{style}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    from videoaudiotext.subtitle.safe_area import ass_margin_override_prefix, segment_index_for_cue

    for text, start, end in cues:
        margin_prefix = ""
        if use_content_safe:
            si = segment_index_for_cue(start, end, bounds)
            si = min(si, len(content_rects) - 1)
            margin_prefix = ass_margin_override_prefix(content_rects[si])
        else:
            if wrap_reveal_enabled():
                from videoaudiotext.config.subtitle import (
                    scaled_subtitle_progressive_frame_lr_margin,
                )

                side = scaled_subtitle_progressive_frame_lr_margin()
            else:
                side = scaled_subtitle_frame_lr_margin()
            cx = get_output_width() // 2
            from videoaudiotext.config.subtitle import apply_subtitle_y_to_cy

            cy = apply_subtitle_y_to_cy(
                get_output_height() - scaled_subtitle_margin_bottom()
            )
            margin_prefix = (
                f"{{\\an2\\q2\\pos({cx},{cy})\\margl{side}\\margr{side}}}"
            )
        ass_text = _ass_event_text(
            text,
            margin_prefix=margin_prefix,
        )
        lines.append(
            f"Dialogue: 0,{seconds_to_ass_time(start)},{seconds_to_ass_time(end)},"
            f"Default,,0,0,0,,{ass_text}\n"
        )
    out_path.write_text("".join(lines), encoding="utf-8-sig")
    return out_path


def _load_subtitle_timeline_context(
    video_path: Path | None = None,
) -> dict:
    """从 segment_timeline + 底片分辨率加载 rebake / SRT→ASS 所需上下文。"""
    from videoaudiotext.config import AUDIO_DIR, SEGMENT_TIMELINE_JSON, SOURCE_MEDIA_DIR
    from videoaudiotext.config import set_active_output_dimensions
    from videoaudiotext.media.dimensions import probe_media_dimensions

    empty: dict = {
        "segments": None,
        "durations": None,
        "gaps": None,
        "timeline": None,
        "bounds": None,
        "content_rects": None,
        "wav_paths": None,
    }
    if video_path and video_path.is_file():
        vw, vh = probe_media_dimensions(video_path)
        if vw > 0 and vh > 0:
            set_active_output_dimensions(vw, vh)
    if not SEGMENT_TIMELINE_JSON.is_file():
        return empty

    from videoaudiotext.audio.gaps import compute_absolute_timeline
    from videoaudiotext.audio.timeline import load_timelines
    from videoaudiotext.media.clip import media_for_sentences
    from videoaudiotext.subtitle.build import _subtitle_layout_context
    from videoaudiotext.tts.audio import list_sentence_wavs

    timelines = load_timelines()
    if not timelines:
        return empty

    keys = sorted(timelines.keys(), key=lambda k: int(k))
    segments = [str(timelines[k].get("text") or "") for k in keys]
    durations = [float(timelines[k].get("speech_duration") or 0.0) for k in keys]
    clip_durations = [
        float(timelines[k].get("video_duration") or durations[i])
        for i, k in enumerate(keys)
    ]
    media_paths = None
    try:
        media_paths = media_for_sentences(len(segments), SOURCE_MEDIA_DIR)
    except ValueError:
        pass
    timeline = compute_absolute_timeline(segments, durations)
    bounds, content_rects = _subtitle_layout_context(clip_durations, media_paths)
    wavs = list_sentence_wavs(AUDIO_DIR)
    wav_paths = wavs if len(wavs) == len(segments) else None
    from videoaudiotext.audio.gaps import compute_sentence_gaps

    return {
        "segments": segments,
        "durations": durations,
        "gaps": compute_sentence_gaps(segments),
        "timeline": timeline,
        "bounds": bounds,
        "content_rects": content_rects,
        "wav_paths": wav_paths,
    }


def _cue_has_merged_lines(text: str) -> bool:
    return bool(text.strip()) and (
        "\n" in text.replace("\\N", "\n") or "\\N" in text
    )


def _rebake_prepare_cues(
    cues: List[Cue],
    ctx: dict,
) -> tuple[List[Cue], list[str]]:
    """
    rebake / SRT→ASS 专用：
    - 段级 SRT（条数=段数）→ 整段折行渐进；
    - 已拆条 SRT → 保留时间轴，仅修复仍含 \\N/换行的合并 cue；
    - 已拆好的单行 cue 只刷新显示样式。
    """
    from videoaudiotext.subtitle.build import _finalize_cue_displays, finalize_export_cues
    from videoaudiotext.subtitle.wrap_reveal import expand_wrap_reveal_cues, wrap_reveal_enabled

    if not wrap_reveal_enabled():
        return list(cues), []

    segments = ctx.get("segments")
    wav_paths = ctx.get("wav_paths")
    bounds = ctx.get("bounds")
    content_rects = ctx.get("content_rects")
    timeline = ctx.get("timeline")
    quality_warn: list[str] = []

    if segments and len(cues) == len(segments):
        return _prepare_export_cues(
            cues,
            sentences=segments,
            wav_paths=wav_paths,
            timeline=timeline,
            apply_breath=False,
            try_asr_align=False,
            segment_bounds=bounds,
            content_rects=content_rects,
        )

    out: List[Cue] = []
    for text, start, end in cues:
        if not _cue_has_merged_lines(text):
            out.append((text, start, end))
            continue

        sentence = text.replace("\\N", "\n").replace("\n", "").strip()
        wav = None
        si = None
        if segments and bounds:
            from videoaudiotext.subtitle.safe_area import segment_index_for_cue

            si = segment_index_for_cue(float(start), float(end), bounds)
            if 0 <= si < len(segments):
                sentence = segments[si]
                if wav_paths and si < len(wav_paths):
                    wav = wav_paths[si]

        parts = [
            ln.strip()
            for ln in text.replace("\\N", "\n").split("\n")
            if ln.strip()
        ]
        if len(parts) == 2:
            from videoaudiotext.config import SUBTITLE_CUE_SPLIT_GAP_SEC
            from videoaudiotext.subtitle.wrap_reveal import align_wrap_reveal_boundary

            line1, line2 = parts
            gap = max(0.001, SUBTITLE_CUE_SPLIT_GAP_SEC)
            boundary = align_wrap_reveal_boundary(
                sentence,
                line1,
                line2,
                float(start),
                float(end),
                wav,
            )
            out.append((line1, float(start), boundary - gap / 2))
            out.append((line2, boundary + gap / 2, float(end)))
            quality_warn.append(
                f"rebake 折行：{boundary - float(start):.2f}s 后切第二行"
            )
            continue

        expanded, wr_warn = expand_wrap_reveal_cues(
            [(sentence, float(start), float(end))],
            sentences=[sentence],
            wav_paths=[wav] if wav else None,
            segment_bounds=bounds,
            content_rects=content_rects,
        )
        quality_warn.extend(wr_warn)
        out.extend(expanded)

    if any(_cue_has_merged_lines(t) for t, _, _ in cues):
        out = _finalize_cue_displays(
            out,
            segment_bounds=bounds,
            content_rects=content_rects,
        )
    fixed, export_warn = finalize_export_cues(
        out,
        speech_segment_bounds=_speech_bounds_from_timeline(timeline),
    )
    return fixed, quality_warn + export_warn


def build_ass_from_srt(
    srt_path: Path,
    out_path: Path,
    *,
    video_path: Path | None = None,
    update_srt: bool = False,
) -> Path:
    """从 SRT 时间轴生成 ASS（含折行渐进、双行 \\fs 放大等逐条覆盖）。"""
    cues = parse_srt_file(srt_path)
    ctx = _load_subtitle_timeline_context(video_path)

    cues, quality_warn = _rebake_prepare_cues(cues, ctx)
    for w in quality_warn:
        print(f"  [subtitle] {w}", flush=True)
    if update_srt:
        write_srt_file(cues, srt_path)

    return _write_ass_file(
        cues,
        out_path,
        bounds=ctx.get("bounds"),
        content_rects=ctx.get("content_rects"),
    )
def build_ass(
    sentences: List[str],
    durations: List[float],
    out_path: Path,
    gaps: List[float] | None = None,
    *,
    segment_video_durations: List[float] | None = None,
    wav_paths: List[Path] | None = None,
    media_paths: List[Path] | None = None,
    shot_boundaries: List[bool] | None = None,
    timeline: List | None = None,
    cues: List[Cue] | None = None,
) -> Path:
    from videoaudiotext.subtitle.build import (
        collect_all_cues,
        _resolve_segment_video_durations,
        _subtitle_layout_context,
    )

    seg_durs = _resolve_segment_video_durations(
        sentences,
        durations,
        gaps,
        timeline=timeline,
        segment_video_durations=segment_video_durations,
    )
    bounds, content_rects = _subtitle_layout_context(seg_durs, media_paths)
    if cues is None:
        cues = collect_all_cues(
            sentences,
            durations,
            gaps,
            segment_video_durations=seg_durs,
            media_paths=media_paths,
            shot_boundaries=shot_boundaries,
            timeline=timeline,
        )
    cues, _ = _prepare_export_cues(
        cues,
        sentences=sentences,
        wav_paths=wav_paths,
    )
    return _write_ass_file(cues, out_path, bounds=bounds, content_rects=content_rects)
def subtitle_force_style() -> str:
    return (
        f"Fontname={get_subtitle_font_name()},"
        f"FontSize={scaled_subtitle_srt_font_size()},"
        "PrimaryColour=&HFFFFFF&,"
        "OutlineColour=&H000000&,"
        "BorderStyle=1,"
        "Outline=1,"
        "Shadow=0,"
        "Alignment=2,"
        f"MarginV={scaled_subtitle_margin_bottom()},"
        f"MarginL={scaled_subtitle_margin_lr()},"
        f"MarginR={scaled_subtitle_margin_lr()},"
        f"WrapStyle=0"
    )
def _escape_sub_path(path: Path) -> str:
    p = str(path.resolve()).replace("\\", "/")
    return p.replace(":", "\\:").replace("'", "'\\''")
def _parse_srt_time(text: str) -> float:
    import re

    m = re.match(
        r"(\d+):(\d+):(\d+)[,.](\d+)",
        text.strip(),
    )
    if not m:
        raise ValueError(f"invalid SRT timestamp: {text!r}")
    h, mi, s, ms = m.groups()
    frac = ms.ljust(3, "0")[:3]
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(frac) / 1000.0
def _video_duration_seconds(video_path: Path) -> float | None:
    import json
    import subprocess

    if not video_path.is_file():
        return None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return None
def parse_srt_file(srt_path: Path) -> List[Cue]:
    """解析 SRT 为 (text, start, end) 列表。"""
    cues: List[Cue] = []
    text = srt_path.read_text(encoding="utf-8")
    for block in text.strip().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        start_s, end_s = [p.strip() for p in lines[1].split("-->", 1)]
        body = "\n".join(lines[2:]).strip()
        cues.append((body, _parse_srt_time(start_s), _parse_srt_time(end_s)))
    return cues


def parse_ass_dialogues(ass_path: Path) -> List[tuple[float, float, str]]:
    """解析 ASS Dialogue 行为 (start_sec, end_sec, event_text)。"""
    dialogues: List[tuple[float, float, str]] = []
    if not ass_path.is_file():
        return dialogues
    for line in ass_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        body = line[len("Dialogue:") :].strip()
        parts = body.split(",", 9)
        if len(parts) < 10:
            continue
        dialogues.append(
            (_parse_srt_time(parts[1]), _parse_srt_time(parts[2]), parts[9])
        )
    return dialogues


def ass_header_text(ass_path: Path) -> str:
    """ASS 文件头（含 Style），不含任何 Dialogue 行。"""
    lines: List[str] = []
    for line in ass_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("Dialogue:"):
            break
        lines.append(line)
    return "\n".join(lines) + "\n"


def write_preview_ass_from_workspace(
    source_ass: Path,
    out_ass: Path,
    *,
    segment_index: int = 1,
    bounds: Sequence[tuple[float, float]] | None = None,
) -> bool:
    """
    从 workspace 全量 ASS 提取某句首条 progressive cue，写入短时预览 ASS。
    保留与成片相同的 \\pos / 边距覆盖，时间轴 remap 到 0–3s。
    """
    from videoaudiotext.subtitle.safe_area import segment_index_for_cue

    dialogues = parse_ass_dialogues(source_ass)
    if not dialogues:
        return False
    target_si = max(0, int(segment_index) - 1)
    picked: str | None = None
    if bounds:
        for start, end, text in dialogues:
            if segment_index_for_cue(start, end, bounds) == target_si:
                picked = text
                break
    if picked is None:
        picked = dialogues[min(target_si, len(dialogues) - 1)][2]
    out_ass.parent.mkdir(parents=True, exist_ok=True)
    out_ass.write_text(
        ass_header_text(source_ass)
        + f"Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,{picked}\n",
        encoding="utf-8-sig",
    )
    return True


def validate_and_fix_srt(
    srt_path: Path,
    *,
    max_duration: float | None = None,
    min_gap: float = 0.001,
    preserve_timeline: bool = False,
) -> tuple[Path, list[str]]:
    """
    重烧前防御性校验：修正重叠、逆序、越界片长、end<=start。
    preserve_timeline=True 时仅警告重叠，不调整 start（rebake 保留时间轴）。
    """
    warnings: list[str] = []
    cues = parse_srt_file(srt_path)
    if not cues:
        return srt_path, warnings

    gap = max(min_gap, SUBTITLE_CUE_SPLIT_GAP_SEC if preserve_timeline else min_gap)
    fixed: List[Cue] = []
    prev_end = 0.0
    min_cue = max(SUBTITLE_MIN_CUE_SEC, SUBTITLE_CUE_MIN_DURATION * 0.5)

    for i, (body, start, end) in enumerate(cues, start=1):
        if preserve_timeline:
            if start < prev_end - 1e-9:
                warnings.append(
                    f"cue {i}: start {start:.3f}s 与上条重叠（rebake 保留原时间轴）"
                )
        elif start < prev_end:
            start = prev_end + gap
            warnings.append(f"cue {i}: start 与上条重叠，已调整为 {start:.3f}s")
        if end <= start:
            if preserve_timeline:
                warnings.append(f"cue {i}: end<=start（rebake 保留原时间轴）")
                end = start + min_cue
            else:
                end = start + min_cue
                warnings.append(f"cue {i}: end<=start，已延长到 {end:.3f}s")

        if max_duration is not None:
            if start >= max_duration:
                if preserve_timeline:
                    warnings.append(
                        f"cue {i}: start 超出片长 {max_duration:.3f}s（rebake 保留）"
                    )
                else:
                    start = max(prev_end + gap, max_duration - min_cue * 3)
                    end = min(max(start + min_cue, end), max_duration - 0.02)
                    warnings.append(
                        f"cue {i}: start 超出片长 {max_duration:.3f}s，已收至 {start:.3f}s"
                    )
            elif end > max_duration:
                end = max(start + min_cue, max_duration - 0.02)
                warnings.append(f"cue {i}: end 超出片长，已截断为 {end:.3f}s")

        dur = end - start
        if dur > SUBTITLE_CUE_MAX_DURATION + 0.05:
            warnings.append(
                f"cue {i}: 单条 {dur:.1f}s > {SUBTITLE_CUE_MAX_DURATION}s（"
                + ("rebake 保留原时长" if preserve_timeline else "建议改短或重新生成")
                + "）"
            )

        prev_end = end
        fixed.append((body, start, end))

    if not preserve_timeline:
        write_srt_file(fixed, srt_path)
    return srt_path, warnings
def ass_play_resolution(ass_path: Path) -> tuple[int, int] | None:
    import re

    if not ass_path.is_file():
        return None
    m = re.search(
        r"PlayResX:\s*(\d+)\s*\nPlayResY:\s*(\d+)",
        ass_path.read_text(encoding="utf-8"),
    )
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))
def sync_ass_to_video(video_path: Path, ass_path: Path | None = None) -> bool:
    """
    若 ASS PlayRes 与底片分辨率不一致，按 segment_timeline 重生成 ASS。
    避免画布竖横不一致时字号/边距错位。
    """
    from videoaudiotext.config import SEGMENT_TIMELINE_JSON, SOURCE_MEDIA_DIR, set_active_output_dimensions
    from videoaudiotext.media.dimensions import probe_media_dimensions

    target = ass_path or SUBTITLE_ASS
    if not target.is_file():
        return False

    vw, vh = probe_media_dimensions(video_path)
    if vw <= 0 or vh <= 0:
        return False
    set_active_output_dimensions(vw, vh)

    playres = ass_play_resolution(target)
    if playres == (vw, vh):
        return False
    if not SEGMENT_TIMELINE_JSON.is_file():
        return False

    from videoaudiotext.media.clip import media_for_sentences
    from videoaudiotext.audio.timeline import load_timelines

    timelines = load_timelines()
    if not timelines:
        return False

    keys = sorted(timelines.keys(), key=lambda k: int(k))
    segments = [str(timelines[k].get("text") or "") for k in keys]
    durations = [float(timelines[k].get("speech_duration") or 0.0) for k in keys]
    clip_durations = [
        float(timelines[k].get("video_duration") or durations[i])
        for i, k in enumerate(keys)
    ]
    if not segments or not any(s.strip() for s in segments):
        return False

    media_paths = None
    try:
        media_paths = media_for_sentences(len(segments), SOURCE_MEDIA_DIR)
    except ValueError:
        pass

    from videoaudiotext.audio.gaps import compute_absolute_timeline

    timeline = compute_absolute_timeline(segments, durations)

    build_ass(
        segments,
        durations,
        target,
        timeline=timeline,
        media_paths=media_paths,
    )
    return True
def resolve_embed_subtitle_path(
    custom: str | Path | None = None,
    *,
    prefer_ass: bool = False,
) -> Path:
    from videoaudiotext.config import SUBTITLE_ASS, SUBTITLE_SRT

    if custom:
        p = Path(custom)
        if p.is_file():
            if p.suffix.lower() not in {".srt", ".ass"}:
                raise ValueError(f"字幕文件须为 .srt 或 .ass：{p}")
            return p.resolve()
        raise FileNotFoundError(f"字幕文件不存在：{p}")
    if prefer_ass and SUBTITLE_ASS.is_file():
        return SUBTITLE_ASS
    if SUBTITLE_SRT.is_file():
        return SUBTITLE_SRT
    if SUBTITLE_ASS.is_file():
        return SUBTITLE_ASS
    raise FileNotFoundError("未找到 subtitle.srt / subtitle.ass，请先生成成片或上传字幕")
def install_external_srt(src: str | Path, dest: Path | None = None) -> Path:
    dest = dest or SUBTITLE_SRT
    src_path = Path(src)
    if not src_path.is_file():
        raise FileNotFoundError(f"字幕文件不存在：{src_path}")
    if src_path.suffix.lower() != ".srt":
        raise ValueError("当前仅支持 .srt 替换（可用字幕软件导出 SRT）")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, dest)
    return dest
def subtitle_video_filter(sub_path: Path) -> str:
    esc = _escape_sub_path(sub_path)
    ow, oh = get_output_width(), get_output_height()
    fonts_clause = ""
    if FONTS_DIR.is_dir():
        fonts_clause = f":fontsdir='{_escape_sub_path(FONTS_DIR)}'"
        if not _iter_font_files():
            import logging

            logging.getLogger(__name__).warning(
                "FONTS_DIR=%s 下未找到字体文件，硬字幕可能回退系统字体",
                FONTS_DIR,
            )
    base = f"subtitles='{esc}':original_size={ow}x{oh}{fonts_clause}"
    if sub_path.suffix.lower() == ".srt":
        style = subtitle_force_style().replace("'", "\\'")
        return f"{base}:force_style='{style}'"
    # ASS 使用内嵌 Style 与逐条 \\fs/\\marg* 覆盖；force_style 会冲掉边距导致字幕巨大溢出
    return base
