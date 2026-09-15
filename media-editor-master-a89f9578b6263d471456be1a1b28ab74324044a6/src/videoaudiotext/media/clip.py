"""Step 3: Clip source images/videos — one asset per sentence."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Sequence, Tuple

from videoaudiotext.concurrency.pools import parallel_map, parallel_starmap
from videoaudiotext.config import (
    CLIP_AVOID_STREAM_LOOP,
    CLIP_DIR,
    CLIP_OFFSETS_JSON,
    CLIP_PARTS_JSON,
    CLIP_XFADE_SEC,
    comma_multi_material_enabled,
    get_output_height,
    get_output_width,
    SOURCE_MEDIA_DIR,
    TEMP_DIR,
    VIDEO_FPS,
)
from videoaudiotext.core.timing import format_elapsed, now
from videoaudiotext.core.ffmpeg_util import libx264_video_args, run_ffmpeg
from videoaudiotext.media.dimensions import probe_media_dimensions
from videoaudiotext.media.fill import scale_filter_plain

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}


def _video_avail_from_start(src_dur: float | None, start_sec: float) -> float | None:
    if src_dur is None:
        return None
    return max(0.0, float(src_dur) - max(0.0, float(start_sec)))


def _needs_stream_loop(src_dur: float | None, start_sec: float, need_sec: float) -> bool:
    avail = _video_avail_from_start(src_dur, start_sec)
    if avail is None:
        return False
    return avail + 0.05 < max(0.05, float(need_sec))


def _append_ffmpeg_video_encode(
    cmd: list[str],
    *,
    vf: str,
    out: Path,
) -> None:
    cmd.extend(
        [
            "-vf",
            vf,
            "-r",
            str(VIDEO_FPS),
            "-vsync",
            "cfr",
            *libx264_video_args(),
            "-an",
            str(out),
        ]
    )


def _extend_ffmpeg_video_input(
    cmd: list[str],
    src: Path,
    *,
    start_sec: float,
    need_sec: float,
    src_dur: float | None,
    vf: str,
    out: Path,
    label: str,
) -> None:
    """裁切视频到 need_sec；默认 stream_loop 循环，仅 CLIP_AVOID_STREAM_LOOP 时 tpad。"""
    start = max(0.0, float(start_sec))
    need = max(0.05, float(need_sec))
    avail = _video_avail_from_start(src_dur, start)
    need_loop = _needs_stream_loop(src_dur, start, need)

    if need_loop and CLIP_AVOID_STREAM_LOOP and avail is not None and avail > 0.05:
        pad = need - avail
        cmd.extend(
            [
                "-ss",
                str(start),
                "-t",
                f"{avail:.3f}",
                "-i",
                str(src),
            ]
        )
        vf_out = f"{vf},tpad=stop_mode=clone:stop_duration={pad:.3f}"
        _append_ffmpeg_video_encode(cmd, vf=vf_out, out=out)
        print(
            f"  [clip-pad] {src.name} from {start:.1f}s need {need:.2f}s "
            f"(avail {avail:.2f}s) → tpad +{pad:.2f}s",
            flush=True,
        )
        _run_clip_ffmpeg(cmd, label=label)
        return

    if need_loop:
        cmd.extend(
            [
                "-ss",
                str(start),
                "-stream_loop",
                "-1",
                "-i",
                str(src),
                "-t",
                f"{need:.3f}",
            ]
        )
        print(
            f"  [clip-loop] {src.name} from {start:.1f}s need {need:.2f}s "
            f"(avail {avail:.2f}s) → stream_loop",
            flush=True,
        )
    else:
        cmd.extend(
            [
                "-ss",
                str(start),
                "-t",
                f"{need:.3f}",
                "-i",
                str(src),
            ]
        )
    _append_ffmpeg_video_encode(cmd, vf=vf, out=out)
    _run_clip_ffmpeg(cmd, label=label)
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _sort_key(path: Path) -> Tuple[int, int, str]:
    stem = path.stem
    if stem.isdigit():
        return (0, int(stem), path.name)
    m = re.match(r"^(\d+)", stem)
    if m:
        return (0, int(m.group(1)), path.name)
    return (1, 0, path.name)


def load_clip_offsets(source_dir: Path = SOURCE_MEDIA_DIR) -> dict[str, float]:
    path = CLIP_OFFSETS_JSON
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def list_source_media(source_dir: Path) -> List[Path]:
    if not source_dir.is_dir():
        return []
    files = [
        p
        for p in source_dir.iterdir()
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS
    ]
    return sorted(files, key=_sort_key)


def _indexed_source_media(source_dir: Path) -> dict[int, Path]:
    """Map segment index → representative media path under source_dir."""
    by_idx: dict[int, Path] = {}
    for p in list_source_media(source_dir):
        if p.stem.isdigit():
            by_idx[int(p.stem)] = p
            continue
        m = re.match(r"^(\d+)_part\d+$", p.stem)
        if m:
            idx = int(m.group(1))
            if idx not in by_idx:
                by_idx[idx] = p

    for key, spec in load_clip_parts(source_dir).items():
        try:
            idx = int(key)
        except ValueError:
            continue
        if idx in by_idx:
            continue
        parts = spec.get("parts") or []
        if parts:
            by_idx[idx] = Path(parts[0]["path"])
    return by_idx


def media_for_sentences(
    sentence_count: int,
    source_dir: Path = SOURCE_MEDIA_DIR,
    *,
    segment_indices: Sequence[int] | None = None,
) -> List[Path]:
    """Return one media path per segment, in timeline order.

    When ``segment_indices`` is set (e.g. ``[1, 2, 14]``), lookup uses those
    business indexes instead of assuming contiguous ``1..N``.
    """
    by_idx = _indexed_source_media(source_dir)
    indices = (
        [int(i) for i in segment_indices]
        if segment_indices is not None
        else list(range(1, sentence_count + 1))
    )
    if len(indices) != sentence_count:
        raise ValueError(
            f"segment_indices length {len(indices)} != sentence_count {sentence_count}"
        )
    missing = [i for i in indices if i not in by_idx]
    if missing and len(by_idx) == sentence_count:
        alt = sorted(by_idx.keys())
        if len(alt) == sentence_count:
            indices = alt
            missing = []
    if missing:
        raise ValueError(
            f"缺少 index={missing} 的素材，当前 source_media 仅有 index="
            f"{sorted(by_idx)}（共 {len(by_idx)} 个文件）"
        )
    return [by_idx[i] for i in indices]


def pad_missing_numbered_media(
    sentence_count: int,
    source_dir: Path = SOURCE_MEDIA_DIR,
) -> int:
    """
    若已有 1..N 编号素材但不足 sentence_count，复制最后一个补齐编号文件。
    返回补齐后可用数量。
    """
    if sentence_count <= 0:
        return 0
    source_dir.mkdir(parents=True, exist_ok=True)
    by_idx: dict[int, Path] = {}
    for p in list_source_media(source_dir):
        if p.stem.isdigit():
            by_idx[int(p.stem)] = p
    if not by_idx:
        return 0
    max_idx = max(by_idx)
    if max_idx >= sentence_count:
        return sentence_count
    template = by_idx[max_idx]
    for i in range(max_idx + 1, sentence_count + 1):
        dest = source_dir / f"{i}{template.suffix}"
        if not dest.is_file():
            shutil.copy2(template, dest)
    return sentence_count


def _scale_filter() -> str:
    """兼容旧调用：无素材尺寸时使用黑边 pad。"""
    from videoaudiotext.media.fill import finalize_video_filter, scale_filter_plain

    return finalize_video_filter(scale_filter_plain())


def _vf_for_media(media: Path) -> str:
    sw, sh = probe_media_dimensions(media)
    from videoaudiotext.media.fill import build_ffmpeg_video_filter

    return build_ffmpeg_video_filter(sw, sh)


def clip_matches_output_canvas(path: Path) -> bool:
    """续跑复用 clip 前校验：须与当前成片画布一致。"""
    from videoaudiotext.config import get_output_height, get_output_width

    if not path.is_file() or path.stat().st_size <= 0:
        return False
    w, h = probe_media_dimensions(path)
    if w <= 0 or h <= 0:
        return False
    return w == get_output_width() and h == get_output_height()


def _run_clip_ffmpeg(cmd: List[str], *, label: str = "clip") -> None:
    run_ffmpeg(cmd, label=label)


def image_to_silent_video(image: Path, duration: float, out: Path) -> None:
    _ensure_dir(out.parent)
    cmd = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(image),
        "-t",
        str(duration),
        "-vf",
        _vf_for_media(image),
        "-r",
        str(VIDEO_FPS),
        "-vsync",
        "cfr",
        *libx264_video_args(),
        "-an",
        str(out),
    ]
    _run_clip_ffmpeg(cmd, label="image-to-video")


def video_to_clip(
    src: Path,
    duration: float,
    out: Path,
    *,
    start_sec: float = 0.0,
) -> None:
    _ensure_dir(out.parent)
    dur = max(0.05, float(duration))
    from videoaudiotext.subtitle.build import _video_duration_seconds

    src_dur = _video_duration_seconds(src)
    vf = _vf_for_media(src)
    cmd = ["ffmpeg", "-y"]
    _extend_ffmpeg_video_input(
        cmd,
        src,
        start_sec=start_sec,
        need_sec=dur,
        src_dur=src_dur,
        vf=vf,
        out=out,
        label="video-to-clip",
    )


def load_clip_parts(source_dir: Path = SOURCE_MEDIA_DIR) -> dict[str, dict]:
    path = source_dir / "clip_parts.json"
    if source_dir != SOURCE_MEDIA_DIR:
        path = CLIP_PARTS_JSON if CLIP_PARTS_JSON.parent == source_dir else path
    else:
        path = CLIP_PARTS_JSON
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): v for k, v in data.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _build_one_clip(
    media: Path,
    duration: float,
    out: Path,
    *,
    start_sec: float = 0.0,
) -> float:
    ext = media.suffix.lower()
    t0 = now()
    if ext in IMAGE_EXTS:
        image_to_silent_video(media, duration, out)
    else:
        video_to_clip(media, duration, out, start_sec=start_sec)
    return now() - t0


def _concat_clip_parts(
    part_paths: List[Path],
    durations: List[float],
    out: Path,
    *,
    starts: List[float] | None = None,
) -> float:
    """将多段素材按 Timeline 时长 filter_complex 拼接并重编码（CFR）。"""
    t0 = now()
    _ensure_dir(out.parent)
    if len(part_paths) == 1:
        start = (starts or [0.0])[0]
        elapsed = _build_one_clip(part_paths[0], durations[0], out, start_sec=start)
        return elapsed

    starts = starts or [0.0] * len(part_paths)
    n = len(part_paths)
    inputs: List[str] = []
    for media, dur, start in zip(part_paths, durations, starts):
        inputs.extend(
            [
                "-ss",
                str(max(0.0, start)),
                "-t",
                str(max(0.05, dur)),
                "-r",
                str(VIDEO_FPS),
                "-i",
                str(media),
            ]
        )
    chains = [
        f"[{i}:v]fps={VIDEO_FPS},setpts=PTS-STARTPTS,{_vf_for_media(part_paths[i])}[v{i}]"
        for i in range(n)
    ]
    concat_in = "".join(f"[v{i}]" for i in range(n))
    filter_complex = ";".join(chains) + f";{concat_in}concat=n={n}:v=1:a=0[vout]"

    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        *libx264_video_args(),
        "-vsync",
        "cfr",
        "-an",
        str(out),
    ]
    _run_clip_ffmpeg(cmd, label="concat-clip-parts")
    return now() - t0


def _render_segment_stage(
    i: int,
    dur: float,
    out: Path,
    *,
    media_list: List[Path],
    offsets: dict,
    clip_parts: dict,
    timelines: dict,
    speech_durations: List[float] | None,
) -> tuple[float, str]:
    """按 segment 规则渲染中间或最终画面（不含 timeline trim/xfade）。"""
    key = str(i)
    tl = timelines.get(key)
    spec = clip_parts.get(key)
    media = media_list[i - 1]

    video_parts = (tl or {}).get("video_parts") or []
    multi_from_timeline = (
        comma_multi_material_enabled()
        and len(video_parts) > 1
        and all(p.get("path") for p in video_parts)
        and all(p.get("video_duration") for p in video_parts)
    )

    if multi_from_timeline:
        part_paths = [Path(p["path"]) for p in video_parts]
        part_durs = [float(p["video_duration"]) for p in video_parts]
        part_starts = [float(p.get("media_start") or 0.0) for p in video_parts]
        labels = [str(p.get("text") or "") for p in video_parts]
        if len(part_paths) == 1:
            wall = _build_one_clip(
                part_paths[0], part_durs[0], out, start_sec=part_starts[0]
            )
            line = (
                f"  [clip] 第{i}句 {dur:.2f}s · {format_elapsed(wall)} ← "
                f"{part_paths[0].name}"
            )
        else:
            wall = _concat_clip_parts(
                part_paths, part_durs, out, starts=part_starts
            )
            joined = " → ".join(
                (lb[:10] + "…") if len(lb) > 10 else lb for lb in labels[:4]
            )
            line = (
                f"  [clip] 第{i}句 {dur:.2f}s · {format_elapsed(wall)} ← "
                f"Timeline 逗号拼接 {joined}"
            )
        return wall, line

    if spec and spec.get("parts") and comma_multi_material_enabled():
        parts = spec["parts"]
        weights = spec.get("weights") or [1] * len(parts)
        total_w = sum(max(1, int(w)) for w in weights)
        speech = None
        if speech_durations and len(speech_durations) >= i:
            speech = max(0.0, float(speech_durations[i - 1]))
        part_paths: List[Path] = []
        part_durs: List[float] = []
        part_starts: List[float] = []
        elapsed_speech = 0.0
        for pi, part in enumerate(parts):
            path = Path(part["path"])
            w = max(1, int(weights[pi] if pi < len(weights) else 1))
            if speech is not None and len(parts) > 1 and pi < len(parts) - 1:
                pdur = speech * (w / total_w)
            elif pi == len(parts) - 1:
                pdur = max(0.1, dur - elapsed_speech)
            else:
                pdur = dur * (w / total_w)
            if pi < len(parts) - 1:
                elapsed_speech += pdur
            part_paths.append(path)
            part_durs.append(pdur)
            part_starts.append(float(part.get("start") or 0.0))

        if len(part_paths) == 1:
            wall = _build_one_clip(
                part_paths[0], part_durs[0], out, start_sec=part_starts[0]
            )
            line = (
                f"  [clip] 第{i}句 {dur:.2f}s · {format_elapsed(wall)} ← "
                f"{part_paths[0].name}"
            )
        else:
            wall = _concat_clip_parts(
                part_paths, part_durs, out, starts=part_starts
            )
            labels = spec.get("labels") or [f"段{j+1}" for j in range(len(part_paths))]
            joined = " → ".join(
                (lb[:10] + "…") if len(lb) > 10 else lb for lb in labels[:4]
            )
            line = (
                f"  [clip] 第{i}句 {dur:.2f}s · {format_elapsed(wall)} ← "
                f"逗号拼接 {joined}"
            )
        return wall, line

    start = float(offsets.get(key, 0.0))
    wall = _build_one_clip(media, dur, out, start_sec=start)
    if start > 0:
        line = (
            f"  [clip] 第{i}句 {dur:.2f}s · {format_elapsed(wall)} ← "
            f"{media.name} (from {start:.1f}s)"
        )
    else:
        line = f"  [clip] 第{i}句 {dur:.2f}s · {format_elapsed(wall)} ← {media.name}"
    return wall, line


def build_temp_videos(
    durations: List[float],
    source_dir: Path,
    temp_dir: Path = TEMP_DIR,
    *,
    speech_durations: List[float] | None = None,
    resolution_mode: str | None = None,
    custom_width: int | float | None = None,
    custom_height: int | float | None = None,
    log: Callable[[str], None] | None = None,
) -> List[Path]:
    from videoaudiotext.audio.timeline import load_timelines

    media_list = media_for_sentences(len(durations), source_dir)
    from videoaudiotext.media.dimensions import configure_output_from_media

    configure_output_from_media(
        media_list,
        mode=resolution_mode,
        custom_width=custom_width,
        custom_height=custom_height,
        log=log,
    )
    offsets = load_clip_offsets(source_dir)
    clip_parts = load_clip_parts(source_dir)
    timelines = load_timelines(source_dir=source_dir)
    _ensure_dir(temp_dir)

    def _render_one(i: int, dur: float) -> tuple[int, Path, str]:
        out = temp_dir / f"{i}.mp4"
        wall, line = _render_segment_stage(
            i,
            dur,
            out,
            media_list=media_list,
            offsets=offsets,
            clip_parts=clip_parts,
            timelines=timelines,
            speech_durations=speech_durations,
        )
        return i, out, line

    jobs = list(enumerate(durations, start=1))
    rows = parallel_starmap(_render_one, jobs)

    rows.sort(key=lambda row: row[0])
    temps: List[Path] = []
    for _, out, line in rows:
        print(line)
        temps.append(out)
    return temps


def build_super_temp_videos(
    shots: List,
    temp_dir: Path = TEMP_DIR,
    *,
    resolution_mode: str | None = None,
    custom_width: int | float | None = None,
    custom_height: int | float | None = None,
    log: Callable[[str], None] | None = None,
) -> List[Path]:
    """按分镜树生成大分镜 silent temp（每 shot 一次 FFmpeg 切刀）。"""
    from videoaudiotext.media.shot_tree import Shot

    if not shots:
        return []

    media_list = [s.video_path for s in shots]
    from videoaudiotext.media.dimensions import configure_output_from_media

    configure_output_from_media(
        media_list,
        mode=resolution_mode,
        custom_width=custom_width,
        custom_height=custom_height,
        log=log,
    )
    _ensure_dir(temp_dir)

    def _render_shot(shot: Shot) -> tuple[int, Path, str]:
        out = temp_dir / f"super_{shot.shot_id}.mp4"
        dur = max(0.05, float(shot.shot_duration))
        wall = _build_one_clip(
            shot.video_path,
            dur,
            out,
            start_sec=float(shot.shot_start_offset),
        )
        idxs = shot.segment_indices
        if len(idxs) == 1:
            label = f"段{idxs[0]}"
        else:
            label = f"段{idxs[0]}-{idxs[-1]}（{len(idxs)}句）"
        if shot.shot_start_offset > 0 and shot.kind == "video":
            line = (
                f"  [super-clip] {label} {dur:.2f}s · {format_elapsed(wall)} ← "
                f"{shot.video_path.name} (from {shot.shot_start_offset:.1f}s)"
            )
        else:
            line = (
                f"  [super-clip] {label} {dur:.2f}s · {format_elapsed(wall)} ← "
                f"{shot.video_path.name}"
            )
        return shot.shot_id, out, line

    rows = parallel_map(_render_shot, shots)

    rows.sort(key=lambda row: row[0])
    temps: List[Path] = []
    for _, out, line in rows:
        print(line)
        temps.append(out)
    return temps


def _build_silent_clip(
    media: Path,
    content_duration: float,
    out: Path,
    *,
    start_sec: float = 0.0,
    tail_pad: float = 0.0,
) -> float:
    """一次编码：缩放/裁切到 content_duration，可选尾帧延长供 xfade。"""
    t0 = now()
    _ensure_dir(out.parent)
    content = max(0.05, float(content_duration))
    pad = max(0.0, float(tail_pad))
    ext = media.suffix.lower()
    vf = _vf_for_media(media)
    if pad > 0.001:
        vf = f"{vf},tpad=stop_mode=clone:stop_duration={pad:.3f}"

    if ext in IMAGE_EXTS:
        cmd = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(media),
            "-t",
            f"{content:.3f}",
            "-vf",
            vf,
            "-r",
            str(VIDEO_FPS),
            "-vsync",
            "cfr",
            *libx264_video_args(),
            "-an",
            str(out),
        ]
        _run_clip_ffmpeg(cmd, label="silent-clip-image")
        return now() - t0

    start = max(0.0, start_sec)
    from videoaudiotext.subtitle.build import _video_duration_seconds

    src_dur = _video_duration_seconds(media)
    cmd = ["ffmpeg", "-y"]
    _extend_ffmpeg_video_input(
        cmd,
        media,
        start_sec=start,
        need_sec=content,
        src_dur=src_dur,
        vf=vf,
        out=out,
        label="silent-clip-video",
    )
    return now() - t0


def _segment_needs_temp_intermediate(
    i: int,
    *,
    timelines: dict,
    clip_parts: dict,
) -> bool:
    key = str(i)
    tl = timelines.get(key)
    spec = clip_parts.get(key)
    video_parts = (tl or {}).get("video_parts") or []
    multi_from_timeline = (
        comma_multi_material_enabled()
        and len(video_parts) > 1
        and all(p.get("path") for p in video_parts)
        and all(p.get("video_duration") for p in video_parts)
    )
    if multi_from_timeline:
        return True
    if spec and spec.get("parts") and comma_multi_material_enabled():
        return len(spec.get("parts") or []) > 1
    return False


def build_super_clips_direct(
    shots: List,
    timeline: Sequence,
    clip_dir: Path = CLIP_DIR,
    *,
    resolution_mode: str | None = None,
    custom_width: int | float | None = None,
    custom_height: int | float | None = None,
    reuse_existing: bool = False,
    log: Callable[[str], None] | None = None,
) -> List[Path]:
    """大分镜一次编码直出 clip/（合并原步骤 3+4 的 libx264 轮次）。"""
    from videoaudiotext.core.compose import apply_clip_edge_fade, clear_clip_dir
    from videoaudiotext.media.shot_tree import Shot

    if not shots:
        return []

    media_list = [s.video_path for s in shots]
    from videoaudiotext.media.dimensions import configure_output_from_media

    configure_output_from_media(
        media_list,
        mode=resolution_mode,
        custom_width=custom_width,
        custom_height=custom_height,
        log=log,
    )
    if reuse_existing:
        _ensure_dir(clip_dir)
    else:
        clear_clip_dir(clip_dir)
        _ensure_dir(clip_dir)
    use_xfade = CLIP_XFADE_SEC > 0.001 and len(shots) > 1
    n = len(shots)

    def _render_shot(i: int, shot: Shot) -> tuple[int, Path, str]:
        out = clip_dir / f"{i}.mp4"
        if (
            reuse_existing
            and out.is_file()
            and out.stat().st_size > 0
            and clip_matches_output_canvas(out)
        ):
            line = f"  [clip-direct] super_clip/{i}.mp4 (复用缓存)"
            return i, out, line
        if reuse_existing and out.is_file() and out.stat().st_size > 0:
            if log:
                log(f"  clip/{i}.mp4 分辨率与当前画布不一致，重新编码")
        idxs = shot.segment_indices
        content_dur = sum(
            timeline[j - 1].video_content_duration for j in idxs
        )
        last_idx = idxs[-1] - 1
        tail_pad = (
            timeline[last_idx].xfade_out
            if use_xfade and i < n and last_idx < len(timeline)
            else 0.0
        )
        wall = _build_silent_clip(
            shot.video_path,
            content_dur,
            out,
            start_sec=float(shot.shot_start_offset),
            tail_pad=tail_pad,
        )
        if not use_xfade:
            apply_clip_edge_fade(out)
        if len(idxs) == 1:
            label = f"段{idxs[0]}"
        else:
            label = f"段{idxs[0]}-{idxs[-1]}"
        line = (
            f"  [clip-direct] super_clip/{i}.mp4 ({label}) "
            f"{content_dur + tail_pad:.2f}s · {format_elapsed(wall)} ← "
            f"{shot.video_path.name}"
        )
        return i, out, line

    jobs = [(i, shot) for i, shot in enumerate(shots, start=1)]
    rows = parallel_starmap(_render_shot, jobs)

    rows.sort(key=lambda row: row[0])
    clips: List[Path] = []
    for _, out, line in rows:
        if log:
            log(line)
        else:
            print(line)
        clips.append(out)
    return clips


def build_segment_clips_direct(
    durations: List[float],
    source_dir: Path,
    timeline: Sequence,
    clip_dir: Path = CLIP_DIR,
    *,
    speech_durations: List[float] | None = None,
    resolution_mode: str | None = None,
    custom_width: int | float | None = None,
    custom_height: int | float | None = None,
    reuse_existing: bool = False,
    log: Callable[[str], None] | None = None,
) -> List[Path]:
    """逐段一次编码直出 clip/；逗号多素材仍走 temp + trim 回退。"""
    from videoaudiotext.audio.timeline import load_timelines
    from videoaudiotext.core.compose import (
        apply_clip_edge_fade,
        clear_clip_dir,
        export_silent_video_clip,
    )

    media_list = media_for_sentences(len(durations), source_dir)
    from videoaudiotext.media.dimensions import configure_output_from_media

    configure_output_from_media(
        media_list,
        mode=resolution_mode,
        custom_width=custom_width,
        custom_height=custom_height,
        log=log,
    )
    offsets = load_clip_offsets(source_dir)
    clip_parts = load_clip_parts(source_dir)
    timelines = load_timelines(source_dir=source_dir)
    if reuse_existing:
        _ensure_dir(clip_dir)
    else:
        clear_clip_dir(clip_dir)
        _ensure_dir(clip_dir)
    use_xfade = CLIP_XFADE_SEC > 0.001 and len(timeline) > 1
    n = len(timeline)
    temp_dir = TEMP_DIR
    _ensure_dir(temp_dir)

    def _render_one(i: int, dur: float) -> tuple[int, Path, str]:
        out = clip_dir / f"{i}.mp4"
        if (
            reuse_existing
            and out.is_file()
            and out.stat().st_size > 0
            and clip_matches_output_canvas(out)
        ):
            line = f"  [clip-direct] clip/{i}.mp4 (复用缓存)"
            return i, out, line
        if reuse_existing and out.is_file() and out.stat().st_size > 0:
            if log:
                log(f"  clip/{i}.mp4 分辨率与当前画布不一致，重新编码")
        entry = timeline[i - 1]
        content_dur = float(entry.video_content_duration)
        tail_pad = entry.xfade_out if use_xfade and i < n else 0.0

        if _segment_needs_temp_intermediate(i, timelines=timelines, clip_parts=clip_parts):
            temp_out = temp_dir / f"_{i}_stage.mp4"
            stage_wall, _stage_line = _render_segment_stage(
                i,
                dur,
                temp_out,
                media_list=media_list,
                offsets=offsets,
                clip_parts=clip_parts,
                timelines=timelines,
                speech_durations=speech_durations,
            )
            t0 = now()
            export_silent_video_clip(temp_out, content_dur, out, xfade_extend=tail_pad)
            wall = stage_wall + (now() - t0)
            temp_out.unlink(missing_ok=True)
            line = (
                f"  [clip-direct] clip/{i}.mp4 {content_dur + tail_pad:.2f}s "
                f"(comma) · {format_elapsed(wall)}"
            )
        else:
            key = str(i)
            media = media_list[i - 1]
            start = float(offsets.get(key, 0.0))
            wall = _build_silent_clip(
                media,
                content_dur,
                out,
                start_sec=start,
                tail_pad=tail_pad,
            )
            if start > 0:
                line = (
                    f"  [clip-direct] clip/{i}.mp4 {content_dur + tail_pad:.2f}s · "
                    f"{format_elapsed(wall)} ← {media.name} (from {start:.1f}s)"
                )
            else:
                line = (
                    f"  [clip-direct] clip/{i}.mp4 {content_dur + tail_pad:.2f}s · "
                    f"{format_elapsed(wall)} ← {media.name}"
                )

        if not use_xfade:
            apply_clip_edge_fade(out)
        return i, out, line

    jobs = list(enumerate(durations, start=1))
    rows = parallel_starmap(_render_one, jobs)

    rows.sort(key=lambda row: row[0])
    clips: List[Path] = []
    for _, out, line in rows:
        if log:
            log(line)
        else:
            print(line)
        clips.append(out)
    return clips


def create_placeholder_image(
    path: Path,
    width: int | None = None,
    height: int | None = None,
    color: str = "0x1a1a2e",
) -> None:
    if width is None:
        width = get_output_width()
    if height is None:
        height = get_output_height()
    _ensure_dir(path.parent)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s={width}x{height}:d=1",
        "-frames:v",
        "1",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def create_numbered_placeholders(sentence_count: int, source_dir: Path = SOURCE_MEDIA_DIR) -> None:
    _ensure_dir(source_dir)
    colors = ["0x1a1a2e", "0x16213e", "0x0f3460", "0x533483", "0x2d6a4f", "0x6a040f"]
    for i in range(1, sentence_count + 1):
        out = source_dir / f"{i}.jpg"
        if out.is_file():
            continue
        create_placeholder_image(out, color=colors[(i - 1) % len(colors)])
