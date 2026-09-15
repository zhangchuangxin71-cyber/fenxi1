"""Step 4–5: Merge clips, concat, embed subtitles."""

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from videoaudiotext.concurrency.pools import parallel_starmap
from videoaudiotext.config import (
    AUDIO_CODEC,
    AUDIO_CROSSFADE_CURVE,
    AUDIO_DIR,
    AudioFadeParams,
    CLIP_DIR,
    CLIP_EDGE_FADE_SEC,
    CLIP_XFADE_SEC,
    FILELIST,
    NO_SUB_MP4,
    OUTPUT_MP4,
    SUBTITLE_ASS,
    SUBTITLE_MODE,
    SUBTITLE_SRT,
    SUBTITLE_X264_CRF,
    SUBTITLE_X264_PRESET,
    TEMP_DIR,
    VIDEO_SYNC_HARD_CUT,
    audio_fade_params_for_strength,
    audio_fade_strength_label,
    default_audio_fade_strength,
    normalize_audio_fade_strength,
)
from videoaudiotext.tts.audio import SEGMENTS_DIGEST_FILE
from videoaudiotext.audio.gaps import (
    SegmentTimelineEntry,
    shot_output_durations,
    timeline_output_durations,
    timeline_planned_clip_durations,
    timeline_xfade_transitions,
)
from videoaudiotext.subtitle.build import subtitle_video_filter
from videoaudiotext.core.ffmpeg_util import libx264_video_args, run_ffmpeg
from videoaudiotext.core.timing import format_elapsed, now
from videoaudiotext.tts.audio import concat_wav_files, get_audio_duration_seconds, pad_audio_with_gap


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def ensure_mp4_web_streamable(path: Path) -> None:
    """
    将 moov 移到文件头，浏览器/Gradio 可边下边播。
    硬字幕 libx264 默认不写 faststart，28MB 成片会表现为「无法播放」。
    """
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != ".mp4":
        return
    data = path.read_bytes()
    moov, mdat = data.find(b"moov"), data.find(b"mdat")
    if moov >= 0 and mdat >= 0 and moov < mdat:
        return
    tmp = path.with_name(f"_{path.stem}_faststart{path.suffix}")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    run_ffmpeg(cmd, label="mp4-faststart")
    tmp.replace(path)


def merge_video_audio(temp_mp4: Path, wav: Path, out: Path) -> None:
    _ensure_dir(out.parent)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(temp_mp4),
        "-i",
        str(wav),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        AUDIO_CODEC,
        "-shortest",
        str(out),
    ]
    run_ffmpeg(cmd, label="merge-video-audio")


def _clear_work_dir_files(directory: Path) -> int:
    """Remove all files under temp/ or clip/ (incl. .wav leftovers from crashed runs)."""
    if not directory.is_dir():
        return 0
    removed = 0
    for p in directory.iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)
            removed += 1
    return removed


def clear_temp_dir(temp_dir: Path = TEMP_DIR) -> int:
    """Remove all pipeline work files under temp/."""
    return _clear_work_dir_files(temp_dir)


def clear_clip_dir(clip_dir: Path = CLIP_DIR) -> int:
    """Remove all pipeline work files under clip/."""
    return _clear_work_dir_files(clip_dir)


def _clear_concat_artifacts(*, list_path: Path = FILELIST) -> int:
    """Remove root-level FFmpeg concat list (stale paths after clip/ cleanup)."""
    if list_path.is_file():
        list_path.unlink(missing_ok=True)
        return 1
    return 0


def _is_audio_work_dir(name: str) -> bool:
    return name == "_ab_compare" or name == "_probe" or name.startswith("_test_")


def clear_audio_intermediates(audio_dir: Path = AUDIO_DIR) -> int:
    """Remove TTS wav、混音产物、digest 与 audio/ 下测试目录。"""
    if not audio_dir.is_dir():
        return 0
    removed = 0
    for wav in audio_dir.glob("*.wav"):
        wav.unlink(missing_ok=True)
        removed += 1
    digest = audio_dir / SEGMENTS_DIGEST_FILE
    if digest.is_file():
        digest.unlink(missing_ok=True)
        removed += 1
    for entry in list(audio_dir.iterdir()):
        if entry.is_dir() and _is_audio_work_dir(entry.name):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def clear_retrieval_thumb_cache(cache_dir: Path | None = None) -> int:
    """No-op (legacy CLIP preview cache removed)."""
    return 0


def clear_no_sub_mp4(path: Path = NO_SUB_MP4) -> int:
    if path.is_file():
        path.unlink(missing_ok=True)
        return 1
    return 0


def clear_subtitle_exports(
    *,
    srt_path: Path = SUBTITLE_SRT,
    ass_path: Path = SUBTITLE_ASS,
) -> int:
    """Remove exported subtitle files (burn-in already applied to output.mp4)."""
    removed = 0
    for path in (srt_path, ass_path):
        if path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    return removed


_SOURCE_MEDIA_JSON_CACHE = (
    "media_choices.json",
    "pipeline_plan.json",
    "clip_offsets.json",
    "clip_parts.json",
    "segment_timeline.json",
    "shot_tree.json",
)
_SOURCE_MEDIA_PART_STEM = re.compile(r"^\d+_part\d+$")


def clear_source_media_run_cache(source_dir: Path | None = None) -> int:
    """删除 source_media/ 下与本次成片绑定的 JSON 缓存（保留 1.mp4 等编号素材）。"""
    from videoaudiotext.config import SOURCE_MEDIA_DIR

    root = source_dir or SOURCE_MEDIA_DIR
    removed = 0
    for name in _SOURCE_MEDIA_JSON_CACHE:
        path = root / name
        if path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def clear_source_media_derived_files(
    source_dir: Path | None = None,
    *,
    segment_count: int | None = None,
    clear_numbered: bool = False,
) -> int:
    """
    清理 source_media/ 派生/杂项文件：
    - {N}.dual_scene、{N}_part{K}.*
    - 非编号孤儿素材（上传/检索遗留）
    - 段数变少时多余的 {N}.mp4（N > segment_count）
    - clear_numbered=True 时删除全部编号素材
    """
    from videoaudiotext.config import SOURCE_MEDIA_DIR
    from videoaudiotext.media.clip import MEDIA_EXTS

    root = source_dir or SOURCE_MEDIA_DIR
    if not root.is_dir():
        return 0
    removed = 0
    for path in list(root.iterdir()):
        if not path.is_file():
            continue
        stem = path.stem
        ext = path.suffix.lower()
        if ext == ".dual_scene":
            path.unlink(missing_ok=True)
            removed += 1
            continue
        if ext == ".json":
            continue
        if ext not in MEDIA_EXTS:
            continue
        if _SOURCE_MEDIA_PART_STEM.match(stem):
            path.unlink(missing_ok=True)
            removed += 1
            continue
        if stem.isdigit():
            idx = int(stem)
            if clear_numbered or (
                segment_count is not None and idx > segment_count
            ):
                path.unlink(missing_ok=True)
                removed += 1
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def clear_cover_jpg(path: Path | None = None) -> int:
    from videoaudiotext.config import COVER_JPG

    cover = path or COVER_JPG
    if cover.is_file():
        cover.unlink(missing_ok=True)
        return 1
    return 0


@dataclass(frozen=True)
class CleanupCounts:
    temp: int = 0
    clip: int = 0
    filelist: int = 0
    audio: int = 0
    cache_thumbs: int = 0
    source_media_cache: int = 0
    source_media_extras: int = 0
    cover: int = 0
    no_sub: int = 0
    subtitles: int = 0

    def summary(self) -> str:
        parts: list[str] = []
        if self.temp:
            parts.append(f"temp/ {self.temp}")
        if self.clip:
            parts.append(f"clip/ {self.clip}")
        if self.filelist:
            parts.append("filelist.txt")
        if self.audio:
            parts.append(f"audio/ {self.audio}")
        if self.cache_thumbs:
            parts.append(f"cache/retrieval_thumbs/ {self.cache_thumbs}")
        if self.source_media_cache:
            parts.append(f"source_media 缓存 {self.source_media_cache}")
        if self.source_media_extras:
            parts.append(f"source_media 杂项 {self.source_media_extras}")
        if self.cover:
            parts.append("cover.jpg")
        if self.no_sub:
            parts.append("no_sub.mp4")
        if self.subtitles:
            parts.append(f"subtitle.srt/ass {self.subtitles}")
        return "、".join(parts) if parts else "无"


def cleanup_run_artifacts(
    *,
    temp_dir: Path = TEMP_DIR,
    clip_dir: Path = CLIP_DIR,
    list_path: Path = FILELIST,
    audio_dir: Path = AUDIO_DIR,
    cache_dir: Path | None = None,
    source_media_dir: Path | None = None,
    no_sub_path: Path = NO_SUB_MP4,
    srt_path: Path = SUBTITLE_SRT,
    ass_path: Path = SUBTITLE_ASS,
    cover_path: Path | None = None,
    clear_source_media_cache: bool = True,
    clear_source_media_numbered: bool | None = None,
    clear_cover: bool | None = None,
    clear_no_sub: bool = True,
    clear_subtitles: bool = True,
    segment_count: int | None = None,
) -> CleanupCounts:
    """成片后删除中间产物（默认保留 output.mp4、text.txt）。"""
    from videoaudiotext.config import (
        CLEANUP_COVER,
        CLEANUP_SOURCE_MEDIA_NUMBERED,
        COVER_JPG,
        SOURCE_MEDIA_DIR,
    )

    if clear_source_media_numbered is None:
        clear_source_media_numbered = CLEANUP_SOURCE_MEDIA_NUMBERED
    if clear_cover is None:
        clear_cover = CLEANUP_COVER

    sm_root = source_media_dir or SOURCE_MEDIA_DIR
    sm_cache = clear_source_media_run_cache(sm_root) if clear_source_media_cache else 0
    sm_extras = clear_source_media_derived_files(
        sm_root,
        segment_count=segment_count,
        clear_numbered=clear_source_media_numbered,
    )
    return CleanupCounts(
        temp=clear_temp_dir(temp_dir),
        clip=clear_clip_dir(clip_dir),
        filelist=_clear_concat_artifacts(list_path=list_path),
        audio=clear_audio_intermediates(audio_dir),
        cache_thumbs=clear_retrieval_thumb_cache(cache_dir),
        source_media_cache=sm_cache,
        source_media_extras=sm_extras,
        cover=clear_cover_jpg(cover_path or COVER_JPG) if clear_cover else 0,
        no_sub=clear_no_sub_mp4(no_sub_path) if clear_no_sub else 0,
        subtitles=(
            clear_subtitle_exports(srt_path=srt_path, ass_path=ass_path)
            if clear_subtitles
            else 0
        ),
    )


def cleanup_temp_and_clip(
    *,
    temp_dir: Path = TEMP_DIR,
    clip_dir: Path = CLIP_DIR,
    list_path: Path = FILELIST,
) -> tuple[int, int, int]:
    """向后兼容别名；实际调用 cleanup_run_artifacts。"""
    counts = cleanup_run_artifacts(
        temp_dir=temp_dir,
        clip_dir=clip_dir,
        list_path=list_path,
    )
    return counts.temp, counts.clip, counts.filelist


def measure_clip_durations(clips: List[Path]) -> List[float]:
    """ffprobe 实测各 clip 时长。"""
    from videoaudiotext.subtitle.build import _video_duration_seconds

    out: List[float] = []
    for clip in clips:
        dur = _video_duration_seconds(clip)
        if dur is None or dur <= 0:
            raise RuntimeError(f"无法读取 clip 时长：{clip}")
        out.append(float(dur))
    return out


def apply_clip_edge_fade(clip_path: Path, fade_sec: float = CLIP_EDGE_FADE_SEC) -> None:
    """段首尾短淡入淡出（无 xfade 时使用）。"""
    if fade_sec <= 0.001:
        return
    from videoaudiotext.subtitle.build import _video_duration_seconds

    dur = _video_duration_seconds(clip_path)
    if dur is None or dur <= fade_sec * 3:
        return
    st_out = max(0.0, dur - fade_sec)
    tmp = clip_path.with_name(f"_{clip_path.stem}_fade{clip_path.suffix}")
    vf = f"fade=t=in:st=0:d={fade_sec},fade=t=out:st={st_out:.3f}:d={fade_sec}"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(clip_path),
        "-vf",
        vf,
        "-an",
        *libx264_video_args(),
        str(tmp),
    ]
    run_ffmpeg(cmd, label=f"clip-edge-fade:{clip_path.name}")
    tmp.replace(clip_path)


def export_silent_video_clip(
    temp_mp4: Path,
    content_duration: float,
    out: Path,
    *,
    xfade_extend: float = 0.0,
) -> None:
    """导出无声画面 clip：内容区 content_duration，可选尾帧延长供 xfade。"""
    _ensure_dir(out.parent)
    dur = max(0.05, float(content_duration))
    if xfade_extend > 0.001:
        vf = (
            f"trim=duration={dur:.3f},setpts=PTS-STARTPTS,"
            f"tpad=stop_mode=clone:stop_duration={xfade_extend:.3f}"
        )
    else:
        vf = f"trim=duration={dur:.3f},setpts=PTS-STARTPTS"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(temp_mp4),
        "-vf",
        vf,
        "-an",
        *libx264_video_args(),
        str(out),
    ]
    run_ffmpeg(cmd, label=f"export-silent-clip:{out.name}")


def extend_video_tail_for_xfade(clip_path: Path, extend_sec: float) -> None:
    """仅画面尾帧延长（音轨已解耦，不再 apad）。"""
    if extend_sec <= 0.001:
        return
    tmp = clip_path.with_name(f"_{clip_path.stem}_xpad{clip_path.suffix}")
    vf = f"tpad=stop_mode=clone:stop_duration={extend_sec:.3f}"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(clip_path),
        "-vf",
        vf,
        "-an",
        *libx264_video_args(),
        str(tmp),
    ]
    run_ffmpeg(cmd, label=f"clip-xfade-pad:{clip_path.name}")
    tmp.replace(clip_path)


def _adapt_fade_duration(requested: float, speech_duration: float) -> float:
    """缩短淡化，避免短句整段被包络吃光。"""
    if requested <= 0.001 or speech_duration <= 0.001:
        return 0.0
    return min(requested, speech_duration * 0.49)


def _speech_fade_filter_chain(
    index: int,
    count: int,
    speech_duration: float,
    params: AudioFadeParams,
) -> str | None:
    """
    等长音量包络：在每人声段内部/边界做淡入淡出，不改变样本长度。
    段间过渡 = 下一句开头 qsin 淡入（上一句尾 gap 仍为纯静音）。
    """
    if count <= 0 or speech_duration <= 0.001:
        return None

    curve = AUDIO_CROSSFADE_CURVE
    parts: List[str] = []

    if index == 0:
        fade_in = _adapt_fade_duration(params.fade_in_sec, speech_duration)
        if fade_in > 0.001:
            parts.append(f"afade=t=in:st=0:d={fade_in:.4f}:curve={curve}")
    else:
        fade_in = _adapt_fade_duration(params.crossfade_sec, speech_duration)
        if fade_in > 0.001:
            parts.append(f"afade=t=in:st=0:d={fade_in:.4f}:curve={curve}")

    if index == count - 1:
        fade_out = _adapt_fade_duration(params.fade_out_sec, speech_duration)
        if fade_out > 0.001:
            st = max(0.0, speech_duration - fade_out)
            parts.append(f"afade=t=out:st={st:.4f}:d={fade_out:.4f}:curve={curve}")
    elif params.edge_micro_sec > 0.001:
        edge = _adapt_fade_duration(params.edge_micro_sec, speech_duration)
        if edge > 0.001 and speech_duration > edge * 3:
            st = max(0.0, speech_duration - edge)
            parts.append(f"afade=t=out:st={st:.4f}:d={edge:.4f}:curve=tri")

    return ",".join(parts) if parts else None


def _build_master_audio_hard(
    wav_files: List[Path],
    gaps: Sequence[float],
    out: Path,
    *,
    work_dir: Path | None = None,
) -> None:
    work = work_dir or out.parent
    chunks: List[Path] = []
    for i, wav in enumerate(wav_files):
        gap_f = float(gaps[i]) if i < len(gaps) else 0.0
        if gap_f > 0.001:
            padded = work / f"_master_pad_{i + 1}.wav"
            pad_audio_with_gap(wav, gap_f, padded)
            chunks.append(padded)
        else:
            chunks.append(wav)
    concat_wav_files(chunks, out)
    for p in chunks:
        if p.name.startswith("_master_pad_"):
            p.unlink(missing_ok=True)


def _build_master_audio_with_fades(
    wav_files: List[Path],
    gaps: Sequence[float],
    out: Path,
    params: AudioFadeParams,
) -> None:
    """单次 filter_complex：人声淡化 + apad + concat，总时长与硬切一致。"""
    n = len(wav_files)
    if n == 0:
        raise ValueError("build_master_audio: no wav files")

    speech_durations = [get_audio_duration_seconds(w) for w in wav_files]
    expected = sum(speech_durations) + sum(
        float(gaps[i]) if i < len(gaps) else 0.0 for i in range(n)
    )

    cmd = ["ffmpeg", "-y"]
    for wav in wav_files:
        cmd.extend(["-i", str(wav)])

    filter_parts: List[str] = []
    concat_inputs: List[str] = []

    for i in range(n):
        gap_f = float(gaps[i]) if i < len(gaps) else 0.0
        chain = _speech_fade_filter_chain(i, n, speech_durations[i], params)
        src = f"[{i}:a]"
        mid = f"[s{i}]"
        if chain:
            filter_parts.append(f"{src}{chain}{mid}")
        else:
            filter_parts.append(f"{src}anull{mid}")

        if gap_f > 0.001:
            dst = f"[p{i}]"
            filter_parts.append(f"{mid}apad=pad_dur={gap_f:.4f}{dst}")
            concat_inputs.append(dst)
        else:
            concat_inputs.append(mid)

    concat_n = len(concat_inputs)
    concat_in = "".join(concat_inputs)
    filter_parts.append(f"{concat_in}concat=n={concat_n}:v=0:a=1[outa]")
    filter_complex = ";".join(filter_parts)

    cmd.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[outa]",
            "-c:a",
            "pcm_s16le",
            str(out),
        ]
    )
    run_ffmpeg(cmd, label="master-audio-fade")

    actual = get_audio_duration_seconds(out)
    if abs(actual - expected) > 0.02:
        raise RuntimeError(
            f"主音轨时长漂移：期望 {expected:.3f}s，实际 {actual:.3f}s"
        )


def build_master_audio(
    wav_files: List[Path],
    gaps: Sequence[float],
    out: Path,
    *,
    work_dir: Path | None = None,
    audio_fade_strength: str | None = None,
    enable_fades: bool | None = None,
) -> Path:
    """TTS + 段尾 gap 拼接为整条音轨（主轴）；可选等长淡化包络。"""
    t0 = now()
    _ensure_dir(out.parent)
    if enable_fades is not None and audio_fade_strength is None:
        strength = (
            default_audio_fade_strength()
            if enable_fades
            else normalize_audio_fade_strength("off")
        )
    else:
        strength = normalize_audio_fade_strength(audio_fade_strength)
    params = audio_fade_params_for_strength(strength)
    label = audio_fade_strength_label(strength)
    if params is not None:
        _build_master_audio_with_fades(wav_files, gaps, out, params)
        mode = f"淡化{label} {params.log_summary()}"
    else:
        _build_master_audio_hard(wav_files, gaps, out, work_dir=work_dir)
        mode = "硬切"
    wall = now() - t0
    print(
        f"  [audio] master.wav ({len(wav_files)} 段 · {mode}) · {format_elapsed(wall)}",
        flush=True,
    )
    return out


def build_all_video_clips(
    temp_videos: List[Path],
    timeline: Sequence[SegmentTimelineEntry],
    clip_dir: Path = CLIP_DIR,
) -> List[Path]:
    """逐段无声画面 clip，时长对齐 timeline。"""
    _ensure_dir(clip_dir)
    clear_clip_dir(clip_dir)
    use_xfade = CLIP_XFADE_SEC > 0.001 and len(timeline) > 1
    n = len(timeline)

    def _render_one(i: int, tv: Path, entry: SegmentTimelineEntry) -> tuple[int, Path, str]:
        out = clip_dir / f"{i}.mp4"
        xf = entry.xfade_out if use_xfade and i < n else 0.0
        t0 = now()
        export_silent_video_clip(
            tv,
            entry.video_content_duration,
            out,
            xfade_extend=xf,
        )
        wall = now() - t0
        if xf > 0.001:
            line = (
                f"  [video] clip/{i}.mp4 {entry.video_clip_duration:.2f}s "
                f"(+xfade {xf:.2f}s) · {format_elapsed(wall)}"
            )
        else:
            line = (
                f"  [video] clip/{i}.mp4 {entry.video_content_duration:.2f}s · "
                f"{format_elapsed(wall)}"
            )
        if not use_xfade:
            apply_clip_edge_fade(out)
        return i, out, line

    jobs = [
        (i, tv, entry)
        for i, (tv, entry) in enumerate(zip(temp_videos, timeline), start=1)
    ]
    rows = parallel_starmap(_render_one, jobs)

    rows.sort(key=lambda row: row[0])
    clips: List[Path] = []
    for _, out, line in rows:
        print(line)
        clips.append(out)
    return clips


def _build_shot_audio_chunks(
    sub_segments: list,
    wav_files: List[Path],
    gaps: Sequence[float],
    work_dir: Path,
    shot_id: int,
) -> List[Path]:
    chunks: List[Path] = []
    for sub in sub_segments:
        idx = sub.segment_index - 1
        wav = wav_files[idx]
        gap = float(gaps[idx]) if idx < len(gaps) else 0.0
        if gap > 0.001:
            padded = work_dir / f"_super{shot_id}_seg{sub.segment_index}_pad.wav"
            pad_audio_with_gap(wav, gap, padded)
            chunks.append(padded)
        else:
            chunks.append(wav)
    return chunks


def build_super_video_clips(
    shots: list,
    temp_videos: List[Path],
    timeline: Sequence[SegmentTimelineEntry],
    clip_dir: Path = CLIP_DIR,
) -> List[Path]:
    """大分镜无声 clip：时长 = 子段 video_content 之和 + shot 尾 xfade。"""
    _ensure_dir(clip_dir)
    clear_clip_dir(clip_dir)
    use_xfade = CLIP_XFADE_SEC > 0.001 and len(shots) > 1
    n = len(shots)

    def _render_one(i: int, shot, tv: Path) -> tuple[int, Path, str]:
        out = clip_dir / f"{i}.mp4"
        idxs = shot.segment_indices
        content_dur = sum(
            timeline[j - 1].video_content_duration for j in idxs
        )
        last_idx = idxs[-1] - 1
        xf = (
            timeline[last_idx].xfade_out
            if use_xfade and i < n and last_idx < len(timeline)
            else 0.0
        )
        t0 = now()
        export_silent_video_clip(tv, content_dur, out, xfade_extend=xf)
        wall = now() - t0
        if len(idxs) == 1:
            label = f"段{idxs[0]}"
        else:
            label = f"段{idxs[0]}-{idxs[-1]}"
        line = (
            f"  [video] super_clip/{i}.mp4 ({label}) {content_dur + xf:.2f}s · "
            f"{format_elapsed(wall)}"
        )
        if not use_xfade:
            apply_clip_edge_fade(out)
        return i, out, line

    jobs = [
        (i, shot, tv)
        for i, (shot, tv) in enumerate(zip(shots, temp_videos), start=1)
    ]
    rows = parallel_starmap(_render_one, jobs)

    rows.sort(key=lambda row: row[0])
    clips: List[Path] = []
    for _, out, line in rows:
        print(line)
        clips.append(out)
    return clips


def build_all_clips(
    temp_videos: List[Path],
    wav_files: List[Path],
    clip_dir: Path = CLIP_DIR,
    gaps: Optional[List[float]] = None,
    *,
    timeline: Sequence[SegmentTimelineEntry] | None = None,
) -> List[Path]:
    """兼容入口：返回无声画面 clips（音轨由 build_master_audio 单独生成）。"""
    if timeline is None:
        from videoaudiotext.audio.gaps import compute_absolute_timeline

        timeline = compute_absolute_timeline(
            [""] * len(wav_files),
            [0.0] * len(wav_files),
        )
    return build_all_video_clips(temp_videos, timeline, clip_dir)


def build_super_clips(
    shots: list,
    temp_videos: List[Path],
    wav_files: List[Path],
    clip_dir: Path = CLIP_DIR,
    gaps: Optional[List[float]] = None,
    *,
    timeline: Sequence[SegmentTimelineEntry] | None = None,
) -> List[Path]:
    if timeline is None:
        from videoaudiotext.audio.gaps import compute_absolute_timeline

        timeline = compute_absolute_timeline(
            [""] * len(wav_files),
            [0.0] * len(wav_files),
        )
    return build_super_video_clips(shots, temp_videos, timeline, clip_dir)


def write_filelist(clips: List[Path], list_path: Path = FILELIST) -> None:
    lines = []
    for c in clips:
        p = str(c.resolve()).replace("'", "'\\''")
        lines.append(f"file '{p}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def predict_xfade_chain_duration(
    durations: Sequence[float],
    xfade_secs: Sequence[float],
    *,
    xfade_offsets: Sequence[float] | None = None,
) -> float:
    """链式 xfade 成片时长（offset=acc−xf 时 ≈ Σclip−Σxf）。"""
    if not durations:
        return 0.0
    if len(durations) == 1:
        return float(durations[0])
    if len(xfade_secs) != len(durations) - 1:
        raise ValueError("xfade_secs length mismatch")
    if xfade_offsets is not None:
        if len(xfade_offsets) != len(durations) - 1:
            raise ValueError("xfade_offsets length mismatch")
        return float(xfade_offsets[-1]) + float(durations[-1])
    total = float(durations[0])
    for i in range(1, len(durations)):
        total += float(durations[i]) - float(xfade_secs[i - 1])
    return total


def _build_xfade_filter_complex_video(
    durations: List[float],
    xfade_secs: List[float],
    *,
    trim_inputs: bool = False,
    xfade_offsets: Sequence[float] | None = None,
) -> tuple[str, str]:
    """链式 video xfade；每段过渡可用不同叠化时长。"""
    n = len(durations)
    if n <= 1:
        raise ValueError("xfade requires at least 2 clips")
    if len(xfade_secs) != n - 1:
        raise ValueError("xfade_secs length mismatch")
    if xfade_offsets is not None and len(xfade_offsets) != n - 1:
        raise ValueError("xfade_offsets length mismatch")

    parts: List[str] = []
    input_labels: List[str] = []
    norm = _concat_canvas_vf() if trim_inputs else ""
    if trim_inputs:
        for i, dur in enumerate(durations):
            label = f"[vt{i}]"
            parts.append(
                f"[{i}:v]trim=duration={float(dur):.3f},setpts=PTS-STARTPTS,"
                f"{norm}{label}"
            )
            input_labels.append(label)
    else:
        input_labels = [f"[{i}:v]" for i in range(n)]

    prev_v = input_labels[0]
    accumulated = float(durations[0])

    for i in range(1, n):
        xf = max(0.0, float(xfade_secs[i - 1]))
        out_v = f"[v{i}]"
        if xf <= 0.001:
            raise ValueError("mixed zero/non-zero xfade in one chain")
        if xfade_offsets is not None:
            offset = max(0.0, float(xfade_offsets[i - 1]))
        else:
            # 标准 xfade：offset=acc-xf，成片时长 ≈ Σclip−Σxf
            offset = max(0.0, accumulated - xf)
        parts.append(
            f"{prev_v}{input_labels[i]}xfade=transition=fade:duration={xf:.3f}"
            f":offset={offset:.3f}{out_v}"
        )
        if xfade_offsets is None:
            accumulated = accumulated + float(durations[i]) - xf
        else:
            accumulated = offset + float(durations[i])
        prev_v = out_v

    return ";".join(parts), prev_v


def _video_stream_duration(path: Path) -> float | None:
    """读取视频轨实际时长（秒），与容器 format.duration 区分。"""
    import json
    import subprocess

    if not path.is_file():
        return None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        streams = json.loads(out).get("streams") or []
        if not streams:
            return None
        return float(streams[0]["duration"])
    except Exception:
        return None


def _repair_burned_output_video_covers_audio(out: Path, audio_dur: float) -> bool:
    """
    硬字幕成片若视频轨短于音轨，尾帧定格补齐（避免字幕/口播末段画面卡住）。
    返回 True 表示执行了修复。
    """
    v_dur = _video_stream_duration(out)
    if v_dur is None or v_dur + 0.04 >= audio_dur:
        return False
    pad = audio_dur - v_dur
    tmp = out.with_name(f"_{out.stem}_vpad{out.suffix}")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(out),
        "-vf",
        f"tpad=stop_mode=clone:stop_duration={pad:.3f}",
        *libx264_video_args(
            preset=SUBTITLE_X264_PRESET,
            crf=SUBTITLE_X264_CRF,
        ),
        "-c:a",
        "copy",
        "-t",
        f"{audio_dur:.3f}",
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    run_ffmpeg(cmd, label="repair-video-pad")
    tmp.replace(out)
    print(
        f"  [repair] 视频轨 {v_dur:.2f}s → {audio_dur:.2f}s "
        f"(+{pad:.2f}s 尾帧，对齐音轨/字幕)",
        flush=True,
    )
    return True


def _concat_canvas_vf() -> str:
    """拼接前强制统一为当前成片画布（兼容续跑缓存的旧分辨率 clip）。"""
    from videoaudiotext.media.fill import strict_canvas_pad_filter

    return strict_canvas_pad_filter()


def _concat_video_trimmed(
    clips: List[Path],
    durations: Sequence[float],
    out: Path,
) -> None:
    """硬切拼接：每段 trim 到计划时长后 concat。"""
    n = len(clips)
    if n != len(durations):
        raise ValueError("clips and durations length mismatch")
    norm = _concat_canvas_vf()
    parts: List[str] = []
    for i, dur in enumerate(durations):
        parts.append(
            f"[{i}:v]trim=duration={float(dur):.3f},setpts=PTS-STARTPTS,"
            f"{norm}[v{i}]"
        )
    labels = "".join(f"[v{i}]" for i in range(n))
    parts.append(f"{labels}concat=n={n}:v=1:a=0[vout]")
    fc = ";".join(parts)
    cmd = ["ffmpeg", "-y"]
    for clip in clips:
        cmd.extend(["-i", str(clip.resolve())])
    cmd.extend(
        [
            "-filter_complex",
            fc,
            "-map",
            "[vout]",
            "-an",
            *libx264_video_args(),
            str(out),
        ]
    )
    run_ffmpeg(cmd, label="concat-video-trimmed")


def _concat_video_hard(clips: List[Path], out: Path) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        for c in clips:
            p = str(c.resolve()).replace("'", "'\\''")
            f.write(f"file '{p}'\n")
        list_path = Path(f.name)
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-an",
            *libx264_video_args(),
            str(out),
        ]
        run_ffmpeg(cmd, label="concat-video-hard")
    finally:
        list_path.unlink(missing_ok=True)


def concat_video_clips(
    clips: List[Path],
    out: Path,
    *,
    xfade_secs: Sequence[float] | None = None,
    planned_durations: Sequence[float] | None = None,
    xfade_offsets: Sequence[float] | None = None,
) -> float:
    """拼接无声画面；返回成片视频轨时长（秒）。"""
    if len(clips) <= 1:
        if clips:
            import shutil

            shutil.copy2(clips[0], out)
            return measure_clip_durations(clips)[0]
        raise ValueError("concat_video_clips: no clips")

    measured = measure_clip_durations(clips)
    if planned_durations is not None:
        if len(planned_durations) != len(clips):
            raise ValueError("planned_durations length mismatch")
        xfade_durs = [float(d) for d in planned_durations]
        trim_inputs = True
    else:
        xfade_durs = measured
        trim_inputs = False

    if xfade_secs is None:
        xfade_secs = [CLIP_XFADE_SEC] * (len(clips) - 1)

    if all(x <= 0.001 for x in xfade_secs):
        if trim_inputs:
            _concat_video_trimmed(clips, xfade_durs, out)
        else:
            _concat_video_hard(clips, out)
        return sum(xfade_durs)

    fc, map_v = _build_xfade_filter_complex_video(
        xfade_durs,
        list(xfade_secs),
        trim_inputs=trim_inputs,
        xfade_offsets=xfade_offsets,
    )
    cmd = ["ffmpeg", "-y"]
    for clip in clips:
        cmd.extend(["-i", str(clip.resolve())])
    cmd.extend(
        [
            "-filter_complex",
            fc,
            "-map",
            map_v,
            "-an",
            *libx264_video_args(),
            str(out),
        ]
    )
    run_ffmpeg(cmd, label="concat-video-xfade")
    return _media_duration(out)


def _media_duration(path: Path) -> float:
    from videoaudiotext.subtitle.build import _video_duration_seconds

    dur = _video_duration_seconds(path)
    if dur is None or dur <= 0:
        raise RuntimeError(f"无法读取媒体时长: {path}")
    return float(dur)


def _pad_video_to_match_audio(video: Path, audio: Path) -> Path:
    """画面短于音轨时尾帧定格补齐，保证音轨主轴不被截断。"""
    v_dur = _video_stream_duration(video) or _media_duration(video)
    a_dur = _media_duration(audio)
    if v_dur + 0.04 >= a_dur:
        return video
    pad = a_dur - v_dur
    tmp = video.with_name(f"_{video.stem}_apad{video.suffix}")
    vf = f"tpad=stop_mode=clone:stop_duration={pad:.3f}"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-vf",
        vf,
        "-an",
        *libx264_video_args(),
        str(tmp),
    ]
    run_ffmpeg(cmd, label="pad-video-to-audio")
    print(
        f"  [mux-pad] 画面 {v_dur:.2f}s → {a_dur:.2f}s "
        f"(+{pad:.2f}s 尾帧，保留完整音轨)",
        flush=True,
    )
    return tmp


def mux_video_audio(video: Path, audio: Path, out: Path) -> None:
    """音轨主轴 + 画面轨 mux（以音轨时长为准）。"""
    padded = _pad_video_to_match_audio(video, audio)
    a_dur = _media_duration(audio)
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(padded),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            AUDIO_CODEC,
            "-t",
            f"{a_dur:.3f}",
            "-movflags",
            "+faststart",
            str(out),
        ]
        run_ffmpeg(cmd, label="mux-video-audio")
    finally:
        if padded != video:
            padded.unlink(missing_ok=True)


def _planned_durations_for_clips(
    timeline: Sequence[SegmentTimelineEntry],
    video_clips: List[Path],
    *,
    hard_cut: bool,
    shots: Sequence | None = None,
) -> List[float]:
    if shots is not None and len(shots) == len(video_clips):
        return shot_output_durations(timeline, shots, hard_cut=hard_cut)
    planned = timeline_output_durations(timeline, hard_cut=hard_cut)
    if len(planned) != len(video_clips):
        raise ValueError("planned_durations length mismatch")
    return planned


def concat_timeline_output(
    video_clips: List[Path],
    master_audio: Path,
    timeline: Sequence[SegmentTimelineEntry],
    out: Path = NO_SUB_MP4,
    *,
    xfade_secs: Sequence[float] | None = None,
    shots: Sequence | None = None,
) -> None:
    """P1 成片：画面 xfade + 音轨硬切 mux。"""
    if xfade_secs is None:
        xfade_secs = timeline_xfade_transitions(timeline)
    if VIDEO_SYNC_HARD_CUT and len(video_clips) > 1:
        xfade_secs = [0.0] * (len(video_clips) - 1)
    hard_cut = VIDEO_SYNC_HARD_CUT and len(video_clips) > 1
    video_tmp = out.with_name(f"_{out.stem}_video{out.suffix}")

    if len(video_clips) <= 1 and video_clips:
        import shutil

        t0 = now()
        shutil.copy2(video_clips[0], video_tmp)
        xfade_wall = now() - t0
        print(
            f"  [xfade-video] 单段复制 → {video_tmp.name} · {format_elapsed(xfade_wall)}",
            flush=True,
        )
    else:
        t0 = now()
        vdur = concat_video_clips(
            video_clips,
            video_tmp,
            xfade_secs=xfade_secs,
            planned_durations=_planned_durations_for_clips(
                timeline,
                video_clips,
                hard_cut=hard_cut,
                shots=shots,
            ),
        )
        xfade_wall = now() - t0
        xf_total = sum(xfade_secs)
        print(
            f"  [xfade-video] {len(video_clips)} 段叠化 → {video_tmp.name} "
            f"(≈{vdur:.2f}s, Σxf={xf_total:.2f}s) · {format_elapsed(xfade_wall)}",
            flush=True,
        )

    t0 = now()
    mux_video_audio(video_tmp, master_audio, out)
    mux_wall = now() - t0
    video_tmp.unlink(missing_ok=True)
    print(
        f"  [mux] 音轨主轴 + 画面 → {out.name} · {format_elapsed(mux_wall)}",
        flush=True,
    )


def _prepare_hard_subtitle_embed(
    sub_path: Path,
    reference_video: Path | None,
) -> Path:
    from videoaudiotext.config import set_active_output_dimensions
    from videoaudiotext.media.dimensions import probe_media_dimensions
    from videoaudiotext.subtitle.build import sync_ass_to_video

    if reference_video is not None and reference_video.is_file():
        vw, vh = probe_media_dimensions(reference_video)
        if vw > 0 and vh > 0:
            set_active_output_dimensions(vw, vh)
    if sub_path.suffix.lower() == ".ass" and reference_video is not None:
        if reference_video.is_file():
            sync_ass_to_video(reference_video, sub_path)
    return sub_path


def _subtitle_chain_filter(input_label: str, sub_path: Path) -> tuple[str, str]:
    out_label = "[vfinal]"
    body = subtitle_video_filter(sub_path)
    return f"{input_label}{body}{out_label}", out_label


def concat_timeline_output_with_hard_subtitles(
    video_clips: List[Path],
    master_audio: Path,
    subtitle_file: Path,
    out: Path,
    *,
    xfade_secs: Sequence[float] | None = None,
    timeline: Sequence[SegmentTimelineEntry] | None = None,
    shots: Sequence | None = None,
) -> None:
    """画面 xfade → 硬字幕烧录 → 音轨 mux（分步编码，避免叠化+字幕同滤镜链缩短视频轨）。"""
    if not video_clips:
        raise ValueError("concat_timeline_output_with_hard_subtitles: no clips")
    if not master_audio.is_file():
        raise FileNotFoundError(f"主音轨不存在：{master_audio}")
    if not subtitle_file.is_file():
        raise FileNotFoundError(f"字幕文件不存在：{subtitle_file}")

    ref = video_clips[0]
    sub = _prepare_hard_subtitle_embed(subtitle_file, ref)
    audio_dur = _media_duration(master_audio)
    n_clips = len(video_clips)

    if xfade_secs is None:
        xfade_secs = [CLIP_XFADE_SEC] * max(0, n_clips - 1)
    if timeline is not None and VIDEO_SYNC_HARD_CUT and n_clips > 1:
        xfade_secs = [0.0] * (n_clips - 1)
    hard_cut = bool(timeline is not None and VIDEO_SYNC_HARD_CUT and n_clips > 1)

    planned = (
        _planned_durations_for_clips(
            timeline,
            video_clips,
            hard_cut=hard_cut,
            shots=shots,
        )
        if timeline is not None
        else None
    )

    video_tmp = out.with_name(f"_{out.stem}_vonly{out.suffix}")
    sub_tmp = out.with_name(f"_{out.stem}_subv{out.suffix}")

    t0 = now()
    if n_clips == 1:
        import shutil

        shutil.copy2(video_clips[0], video_tmp)
        vdur = (
            float(planned[0])
            if planned
            else measure_clip_durations(video_clips)[0]
        )
    else:
        if len(xfade_secs) != n_clips - 1:
            raise ValueError("xfade_secs length mismatch")
        vdur = concat_video_clips(
            video_clips,
            video_tmp,
            xfade_secs=xfade_secs,
            planned_durations=planned,
        )
    xfade_wall = now() - t0

    padded = _pad_video_to_match_audio(video_tmp, master_audio)
    if padded != video_tmp:
        video_tmp.unlink(missing_ok=True)
        video_tmp = padded

    t1 = now()
    embed_subtitles(video_tmp, sub, sub_tmp, mode="hard")
    sub_wall = now() - t1
    video_tmp.unlink(missing_ok=True)

    t2 = now()
    mux_video_audio(sub_tmp, master_audio, out)
    mux_wall = now() - t2
    sub_tmp.unlink(missing_ok=True)

    xf_total = sum(xfade_secs) if n_clips > 1 else 0.0
    vdur_out = _video_stream_duration(out) or _media_duration(out)
    print(
        f"  [xfade+subtitle] {n_clips} 段叠化(≈{vdur:.2f}s) + 硬字幕 + mux "
        f"→ {out.name} (≈{vdur_out:.2f}s, Σxf={xf_total:.2f}s) · "
        f"{format_elapsed(xfade_wall + sub_wall + mux_wall)}",
        flush=True,
    )
    ensure_mp4_web_streamable(out)


def concat_clips_xfade(
    clips: List[Path],
    out: Path = NO_SUB_MP4,
    *,
    xfade_sec: float = CLIP_XFADE_SEC,
) -> None:
    """遗留入口：clip 自带音轨时，视频 xfade + 音频硬切。"""
    if len(clips) <= 1:
        concat_clips_hard(clips, out=out)
        return
    video_tmp = out.with_name(f"_{out.stem}_vonly{out.suffix}")
    audio_tmp = out.with_name(f"_{out.stem}_audio.wav")
    xfade_secs = [xfade_sec] * (len(clips) - 1)
    concat_video_clips(clips, video_tmp, xfade_secs=xfade_secs)
    cmd = ["ffmpeg", "-y"]
    for clip in clips:
        cmd.extend(["-i", str(clip.resolve())])
    n = len(clips)
    fc = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[outa]"
    cmd.extend(
        [
            "-filter_complex",
            fc,
            "-map",
            "[outa]",
            "-c:a",
            "pcm_s16le",
            str(audio_tmp),
        ]
    )
    try:
        run_ffmpeg(cmd, label="concat-audio-hard")
    except RuntimeError:
        video_tmp.unlink(missing_ok=True)
        raise
    mux_video_audio(video_tmp, audio_tmp, out)
    video_tmp.unlink(missing_ok=True)
    audio_tmp.unlink(missing_ok=True)


def concat_clips_hard(
    clips: List[Path] | None = None,
    list_path: Path = FILELIST,
    out: Path = NO_SUB_MP4,
) -> None:
    if clips is not None:
        write_filelist(clips, list_path)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        str(out),
    ]
    run_ffmpeg(cmd, label="concat-clips-copy")
    print(f"  [concat] → {out.name}", flush=True)


def concat_clips(
    list_path: Path = FILELIST,
    out: Path = NO_SUB_MP4,
    *,
    clips: List[Path] | None = None,
    master_audio: Path | None = None,
    timeline: Sequence[SegmentTimelineEntry] | None = None,
    xfade_secs: Sequence[float] | None = None,
    shots: Sequence | None = None,
) -> None:
    clip_list = clips
    if clip_list is None:
        clip_list = []
        if list_path.is_file():
            for line in list_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("file "):
                    raw = line[5:].strip().strip("'")
                    clip_list.append(Path(raw))

    if master_audio is not None and timeline is not None and clip_list:
        concat_timeline_output(
            clip_list,
            master_audio,
            timeline,
            out=out,
            xfade_secs=xfade_secs,
            shots=shots,
        )
        return

    if CLIP_XFADE_SEC > 0.001 and len(clip_list) > 1:
        concat_clips_xfade(clip_list, out=out, xfade_sec=CLIP_XFADE_SEC)
    else:
        concat_clips_hard(clips=clip_list if clip_list else None, list_path=list_path, out=out)


def embed_subtitles(
    video: Path,
    subtitle_file: Path,
    out: Path = OUTPUT_MP4,
    mode: str = SUBTITLE_MODE,
    *,
    x264_preset: str | None = None,
) -> None:
    if mode == "soft":
        sub = subtitle_file if subtitle_file.is_file() else SUBTITLE_SRT
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-i",
            str(sub),
            "-c",
            "copy",
            "-c:s",
            "mov_text",
            str(out),
        ]
        t0 = now()
        run_ffmpeg(cmd, label="subtitle-soft")
        wall = now() - t0
        print(
            f"  [subtitle] → {out.name} · {format_elapsed(wall)}",
            flush=True,
        )
    else:
        sub = subtitle_file
        if not sub.is_file() and SUBTITLE_SRT.is_file():
            sub = SUBTITLE_SRT
        elif not sub.is_file() and SUBTITLE_ASS.is_file():
            sub = SUBTITLE_ASS
        from videoaudiotext.config import set_active_output_dimensions
        from videoaudiotext.subtitle.build import sync_ass_to_video
        from videoaudiotext.media.dimensions import probe_media_dimensions

        vw, vh = probe_media_dimensions(video)
        if vw > 0 and vh > 0:
            set_active_output_dimensions(vw, vh)
        if sub.suffix.lower() == ".ass":
            sync_ass_to_video(video, sub)
        vf = subtitle_video_filter(sub)
        preset = (x264_preset or SUBTITLE_X264_PRESET).strip() or SUBTITLE_X264_PRESET
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            vf,
            *libx264_video_args(
                preset=preset,
                crf=SUBTITLE_X264_CRF,
            ),
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(out),
        ]
        print(
            f"  [subtitle] 硬字幕烧录 libx264 preset={preset} "
            f"crf={SUBTITLE_X264_CRF}（CPU 较慢）…",
            flush=True,
        )
        t0 = now()
        run_ffmpeg(cmd, label="subtitle-burn")
        wall = now() - t0
        print(
            f"  [subtitle] → {out.name} · {format_elapsed(wall)}",
            flush=True,
        )
    ensure_mp4_web_streamable(out)


def export_cover_frame(
    video: Path,
    out: Path | None = None,
    *,
    at_sec: float | None = None,
) -> Path | None:
    """从成片截取封面帧（默认 COVER_AT_SEC，跳过纯黑首帧）。"""
    from videoaudiotext.config import COVER_AT_SEC, COVER_JPG

    video = Path(video)
    if not video.is_file():
        return None
    target = Path(out) if out is not None else COVER_JPG
    target.parent.mkdir(parents=True, exist_ok=True)
    seek = float(COVER_AT_SEC if at_sec is None else at_sec)
    seek = max(0.0, seek)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{seek:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(target),
    ]
    run_ffmpeg(cmd, label="cover-frame")
    return target if target.is_file() else None


def export_preview_cover_frame(
    media_path: Path,
    sentence: str,
    out: Path | None = None,
    *,
    start_sec: float = 0.0,
    ass_path: Path | None = None,
    preview_segment_index: int = 1,
    segment_bounds: Sequence[tuple[float, float]] | None = None,
) -> Path | None:
    """
    素材 + 字幕样式烧录一帧。
    若提供 ass_path，则与成片同源 ASS（首条 progressive cue）；否则回退 UI 封面逻辑。
    调用前须已 set_active_output_dimensions / set_active_subtitle_font_scale。
    """
    from videoaudiotext.config import COVER_JPG
    from videoaudiotext.media.clip import IMAGE_EXTS, VIDEO_EXTS
    from videoaudiotext.media.dimensions import probe_media_dimensions
    from videoaudiotext.media.fill import build_ffmpeg_video_filter
    from videoaudiotext.core.ffmpeg_util import libx264_video_args
    from videoaudiotext.subtitle.display import subtitle_display_for_sentence
    from videoaudiotext.subtitle.export import _write_ass_file, subtitle_video_filter
    from videoaudiotext.subtitle.safe_area import content_rect_for_media

    media_path = Path(media_path)
    if not media_path.is_file():
        return None
    text = (sentence or "").strip()
    if not text:
        return None
    ext = media_path.suffix.lower()
    if ext not in IMAGE_EXTS | VIDEO_EXTS:
        return None

    target = Path(out) if out is not None else COVER_JPG
    target.parent.mkdir(parents=True, exist_ok=True)
    sw, sh = probe_media_dimensions(media_path)
    vf = build_ffmpeg_video_filter(sw, sh)

    with tempfile.TemporaryDirectory(prefix="cover_preview_") as tmp_name:
        tmp = Path(tmp_name)
        base_mp4 = tmp / "base.mp4"
        preview_ass = tmp / "preview.ass"

        cmd = ["ffmpeg", "-y"]
        if ext in IMAGE_EXTS:
            cmd.extend(["-loop", "1", "-i", str(media_path), "-t", "0.04"])
        else:
            start = max(0.0, float(start_sec))
            cmd.extend(["-ss", f"{start:.3f}", "-i", str(media_path), "-t", "0.04"])
        cmd.extend(["-vf", vf, *libx264_video_args(), "-an", str(base_mp4)])
        run_ffmpeg(cmd, label="preview-cover-base")

        from videoaudiotext.subtitle.export import (
            write_preview_ass_from_workspace,
        )

        used_workspace_ass = False
        if ass_path is not None and ass_path.is_file():
            used_workspace_ass = write_preview_ass_from_workspace(
                ass_path,
                preview_ass,
                segment_index=preview_segment_index,
                bounds=segment_bounds,
            )

        if not used_workspace_ass:
            rect = content_rect_for_media(media_path)
            from videoaudiotext.subtitle.wrap_reveal import (
                preview_wrap_reveal_cues,
                wrap_reveal_enabled,
            )

            if wrap_reveal_enabled():
                cue_list = preview_wrap_reveal_cues(
                    text, 0.0, 3.0, content_rect=rect, wav_path=None
                )
            else:
                cue_list = []
            if not cue_list:
                display = subtitle_display_for_sentence(text, content_rect=rect)
                cue_list = [(display, 0.0, 3.0)]

            _write_ass_file(
                cue_list,
                preview_ass,
                bounds=[(0.0, 3.0)],
                content_rects=[rect],
            )
        cmd2 = [
            "ffmpeg",
            "-y",
            "-i",
            str(base_mp4),
            "-vf",
            subtitle_video_filter(preview_ass),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(target),
        ]
        run_ffmpeg(cmd2, label="preview-cover-burn")

    return target if target.is_file() else None
