import json
import os
import re
import subprocess
import tempfile
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from backend.config import CUT_MAX_WORKERS
from backend.utils.proc import run_text

SEEK_BUFFER = 2.0
# 时长探测与 AAC 填充常有小数偏差；以帧数为准，时长放宽到约 3 帧或 120ms
DURATION_TOLERANCE_FRAMES = 3
DURATION_TOLERANCE_SECONDS = 0.12
FRAME_COUNT_TOLERANCE = 2
DEFAULT_MAX_WORKERS = CUT_MAX_WORKERS
DEFAULT_FPS = 25.0

_VIDEO_ENCODER_ARGS: list[str] | None = None
_FFMPEG_BIN: str | None = None
_FFPROBE_BIN: str | None = None


def _candidate_bins(name: str) -> list[str]:
    env_key = "FFMPEG_BIN" if name == "ffmpeg" else "FFPROBE_BIN"
    candidates = [
        os.environ.get(env_key, ""),
        f"/usr/bin/{name}",
        f"/bin/{name}",
        name,
    ]
    return [c for c in candidates if c]


def _bin_supports_libx264(ffmpeg_bin: str) -> bool:
    result = run_text([ffmpeg_bin, "-hide_banner", "-encoders"])
    return "libx264" in (result.stdout + result.stderr)


def _resolve_ffmpeg() -> str:
    global _FFMPEG_BIN
    if _FFMPEG_BIN is not None:
        return _FFMPEG_BIN
    for candidate in _candidate_bins("ffmpeg"):
        try:
            if _bin_supports_libx264(candidate):
                _FFMPEG_BIN = candidate
                return _FFMPEG_BIN
        except FileNotFoundError:
            continue
    _FFMPEG_BIN = next(
        (c for c in _candidate_bins("ffmpeg") if Path(c).exists() or c == "ffmpeg"),
        "ffmpeg",
    )
    return _FFMPEG_BIN


def _resolve_ffprobe() -> str:
    global _FFPROBE_BIN
    if _FFPROBE_BIN is not None:
        return _FFPROBE_BIN
    ffmpeg = _resolve_ffmpeg()
    paired = str(Path(ffmpeg).with_name("ffprobe")) if Path(ffmpeg).is_absolute() else "ffprobe"
    for candidate in [paired, *_candidate_bins("ffprobe")]:
        try:
            subprocess.run([candidate, "-version"], capture_output=True, check=True)
            _FFPROBE_BIN = candidate
            return _FFPROBE_BIN
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    _FFPROBE_BIN = "ffprobe"
    return _FFPROBE_BIN


def _detect_video_encoder_args() -> list[str]:
    """Prefer libx264; fall back to libopenh264 on limited FFmpeg builds."""
    global _VIDEO_ENCODER_ARGS
    if _VIDEO_ENCODER_ARGS is not None:
        return _VIDEO_ENCODER_ARGS

    result = run_text([_resolve_ffmpeg(), "-hide_banner", "-encoders"])
    encoders = result.stdout + result.stderr
    if "libx264" in encoders:
        _VIDEO_ENCODER_ARGS = ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
    elif "libopenh264" in encoders:
        _VIDEO_ENCODER_ARGS = ["-c:v", "libopenh264", "-b:v", "2M"]
    else:
        _VIDEO_ENCODER_ARGS = ["-c:v", "mpeg4", "-q:v", "5"]
    return _VIDEO_ENCODER_ARGS


def _frame_duration(fps: float) -> float:
    return 1.0 / fps if fps > 0 else 1.0 / DEFAULT_FPS


def _time_to_frame(t: float, fps: float) -> int:
    return max(0, round(t * fps))


def _half_open_frame_range(start: float, end: float, fps: float) -> tuple[int, int, int]:
    """分镜 [start, end) —— end 为下一镜起点，不计入本段。

    Returns (start_frame, end_frame, frame_count).
    """
    start_frame = _time_to_frame(start, fps)
    end_frame = _time_to_frame(end, fps)
    if end_frame <= start_frame:
        end_frame = start_frame + 1
    return start_frame, end_frame, end_frame - start_frame


def _half_open_cut_duration(start: float, end: float, fps: float) -> float:
    _, _, frame_count = _half_open_frame_range(start, end, fps)
    return frame_count * _frame_duration(fps)


# 兼容旧调用名
def _exclusive_frame_count(start: float, end: float, fps: float) -> int:
    return _half_open_frame_range(start, end, fps)[2]


def _exclusive_cut_duration(start: float, end: float, fps: float) -> float:
    return _half_open_cut_duration(start, end, fps)


class FFmpegError(Exception):
    pass


class CutValidationError(Exception):
    pass


def _redact_signed_urls(text: str) -> str:
    return re.sub(r"(https?://[^?\s]+)\?[^\s]+", r"\1?<redacted>", text or "")


def _run_command(cmd: list[str]) -> subprocess.CompletedProcess:
    result = run_text(cmd)
    if result.returncode != 0:
        raise FFmpegError(
            _redact_signed_urls(result.stderr.strip()) or "FFmpeg command failed"
        )
    return result


def probe_duration(video_path: Path) -> float:
    """优先返回视频流时长，避免 AAC 填充把容器时长撑过画面。"""
    cmd = [
        _resolve_ffprobe(),
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    result = _run_command(cmd)
    data = json.loads(result.stdout)
    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if video_stream and video_stream.get("duration"):
        return float(video_stream["duration"])
    return float(data.get("format", {}).get("duration", 0))


def probe_frame_count(video_path: Path) -> int | None:
    cmd = [
        _resolve_ffprobe(),
        "-v",
        "quiet",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        result = _run_command(cmd)
        data = json.loads(result.stdout)
        raw = data.get("streams", [{}])[0].get("nb_read_frames")
        return int(raw) if raw not in (None, "N/A") else None
    except (FFmpegError, ValueError, TypeError, IndexError):
        return None


def get_video_info(video_path: Path) -> dict:
    cmd = [
        _resolve_ffprobe(),
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    result = _run_command(cmd)
    data = json.loads(result.stdout)

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if not video_stream:
        raise FFmpegError("No video stream found")

    duration = float(data.get("format", {}).get("duration", 0))
    # 优先视频流时长，避免 AAC 填充把容器时长撑过画面（胶片片尾会抽到黑帧）
    if video_stream.get("duration"):
        try:
            stream_dur = float(video_stream["duration"])
            if stream_dur > 0:
                duration = stream_dur if duration <= 0 else min(duration, stream_dur)
        except (TypeError, ValueError):
            pass
    # 优先 avg_frame_rate，对 CFR 与 SceneDetect 更一致
    rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1"
    fps_parts = rate.split("/")
    fps = float(fps_parts[0]) / float(fps_parts[1]) if float(fps_parts[1]) else 0.0
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))

    return {
        "duration": duration,
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "fps": round(fps, 3),
        "codec": video_stream.get("codec_name", "unknown"),
        "size_bytes": int(data.get("format", {}).get("size", 0)),
        "has_audio": has_audio,
    }


def _build_cut_command(
    input_path: Path | str,
    output_path: Path | str,
    start: float,
    end: float,
    has_audio: bool,
    fps: float = DEFAULT_FPS,
    precise_decode: bool = True,
    start_frame: int | None = None,
    end_frame: int | None = None,
    fragmented: bool = False,
) -> list[str]:
    if start_frame is not None and end_frame is not None and end_frame > start_frame:
        frame_count = end_frame - start_frame
        resolved_start = start_frame
    else:
        resolved_start, _, frame_count = _half_open_frame_range(start, end, fps)

    # 用帧号反推时间，避免 float 秒与帧边界错位
    start_time = resolved_start / fps
    video_duration = frame_count * _frame_duration(fps)
    cmd = [_resolve_ffmpeg(), "-y"]

    if precise_decode:
        cmd += ["-i", str(input_path), "-ss", f"{start_time:.6f}"]
    else:
        rough_start = max(0.0, start_time - SEEK_BUFFER)
        fine_start = start_time - rough_start
        cmd += [
            "-ss",
            f"{rough_start:.6f}",
            "-i",
            str(input_path),
            "-ss",
            f"{fine_start:.6f}",
        ]

    cmd += [
        "-frames:v",
        str(frame_count),
        "-t",
        f"{video_duration:.6f}",
        "-map",
        "0:v:0",
        *_detect_video_encoder_args(),
    ]

    if has_audio:
        # 不用 -shortest：音轨略短时会提前结束，导致画面少若干帧（常见 0.3~0.6s）
        cmd += [
            "-map",
            "0:a:0?",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-af",
            f"apad=whole_dur={video_duration:.6f}",
        ]
    else:
        cmd += ["-an"]

    cmd += ["-movflags"]
    cmd += [
        "+frag_keyframe+empty_moov+default_base_moof"
        if fragmented
        else "+faststart"
    ]
    cmd += ["-avoid_negative_ts", "make_zero"]
    if fragmented:
        cmd += ["-f", "mp4", "pipe:1"]
    else:
        cmd += [str(output_path)]
    return cmd


def _validate_cut(output_path: Path, expected_duration: float, expected_frames: int, fps: float) -> float:
    actual = probe_duration(output_path)
    frame_tol = max(
        DURATION_TOLERANCE_SECONDS,
        DURATION_TOLERANCE_FRAMES * _frame_duration(fps),
    )
    error = abs(actual - expected_duration)

    actual_frames = probe_frame_count(output_path)
    if actual_frames is not None:
        frame_delta = abs(actual_frames - expected_frames)
        if frame_delta <= FRAME_COUNT_TOLERANCE:
            # 帧数合格则接受；容器/音轨时长探测可与画面不完全一致
            return actual
        # 帧数差过大才判失败（比单纯看时长更可靠）
        if frame_delta > FRAME_COUNT_TOLERANCE and error > frame_tol:
            raise CutValidationError(
                f"时长偏差 {error:.3f}s（预期 {expected_duration:.3f}s，实际 {actual:.3f}s）；"
                f"帧数不符（预期 {expected_frames}，实际 {actual_frames}）"
            )
        if frame_delta > FRAME_COUNT_TOLERANCE:
            raise CutValidationError(
                f"帧数不符（预期 {expected_frames}，实际 {actual_frames}）"
            )

    if error > frame_tol:
        raise CutValidationError(
            f"时长偏差 {error:.3f}s（预期 {expected_duration:.3f}s，实际 {actual:.3f}s）"
        )
    return actual


def _clamp_frame_range_to_source(
    input_path: Path,
    start_frame: int,
    end_frame: int,
    fps: float,
) -> tuple[int, int, int]:
    """避免片尾段按元数据时长多要帧，导致实际少切。"""
    try:
        src_dur = probe_duration(input_path)
    except FFmpegError:
        return start_frame, end_frame, max(1, end_frame - start_frame)

    # 源片按帧估算；留 1 帧余量，避免最后半帧 round 溢出
    max_frame = max(1, int(src_dur * fps + 1e-6))
    sf = max(0, min(start_frame, max_frame - 1))
    ef = max(sf + 1, min(end_frame, max_frame))
    return sf, ef, ef - sf


def cut_segment(
    input_path: Path,
    output_path: Path,
    start: float,
    end: float,
    has_audio: bool = True,
    fps: float = DEFAULT_FPS,
    start_frame: int | None = None,
    end_frame: int | None = None,
) -> Path:
    """半开区间 [start, end) 按帧切割；有帧号时优先用帧号。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if start_frame is not None and end_frame is not None and end_frame > start_frame:
        frame_count = end_frame - start_frame
    else:
        start_frame, end_frame, frame_count = _half_open_frame_range(start, end, fps)

    start_frame, end_frame, frame_count = _clamp_frame_range_to_source(
        input_path, int(start_frame), int(end_frame), fps
    )
    expected_duration = frame_count * _frame_duration(fps)

    # 精解码 → 两段 seek → 必要时纯视频再试，避免音轨拖垮时长
    attempts: list[tuple[bool, bool]] = [
        (True, has_audio),
        (False, has_audio),
    ]
    if has_audio:
        attempts.append((True, False))

    last_error: Exception | None = None
    for precise_decode, use_audio in attempts:
        cmd = _build_cut_command(
            input_path,
            output_path,
            start,
            end,
            use_audio,
            fps=fps,
            precise_decode=precise_decode,
            start_frame=start_frame,
            end_frame=end_frame,
        )
        try:
            _run_command(cmd)
            _validate_cut(output_path, expected_duration, frame_count, fps)
            return output_path
        except (FFmpegError, CutValidationError) as exc:
            last_error = exc
            output_path.unlink(missing_ok=True)
            continue

    if last_error:
        raise last_error
    return output_path


def media_has_audio(path: Path) -> bool:
    """探测文件是否包含音轨。"""
    cmd = [
        _resolve_ffprobe(),
        "-v",
        "quiet",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(path),
    ]
    try:
        result = run_text(cmd)
    except FileNotFoundError as exc:
        raise FFmpegError("ffprobe not found") from exc
    return "audio" in (result.stdout or "").lower()


def _cut_segment_job(
    input_path: str,
    output_path: str,
    index: int,
    start: float,
    end: float,
    has_audio: bool,
    fps: float,
    start_frame: int | None = None,
    end_frame: int | None = None,
) -> tuple[int, bool, str]:
    try:
        video_out = Path(output_path)
        cut_segment(
            Path(input_path),
            video_out,
            start,
            end,
            has_audio=has_audio,
            fps=fps,
            start_frame=start_frame,
            end_frame=end_frame,
        )
        return index, True, ""
    except (FFmpegError, CutValidationError) as exc:
        return index, False, str(exc)[:300]


def _resolve_worker_count(segment_count: int, max_workers: int | None = None) -> int:
    limit = max_workers if max_workers is not None else DEFAULT_MAX_WORKERS
    cpu_count = os.cpu_count() or 1
    return max(1, min(limit, cpu_count, segment_count))


def cut_segments(
    input_path: Path,
    output_dir: Path,
    segments: list[tuple],
    on_progress=None,
    max_workers: int | None = None,
    fps: float | None = None,
) -> tuple[list[Path], dict]:
    """segments 项: (index, start, end) 或 (index, start, end, start_frame, end_frame)。"""
    info = get_video_info(input_path)
    has_audio = info.get("has_audio", True)
    cut_fps = fps or info.get("fps", DEFAULT_FPS) or DEFAULT_FPS
    output_dir.mkdir(parents=True, exist_ok=True)
    total = len(segments)
    failed: list[int] = []
    errors: dict[int, str] = {}
    outputs: list[Path] = []

    if total == 0:
        return outputs, {
            "total": 0,
            "success": 0,
            "failed": 0,
            "failed_indexes": [],
            "errors": {},
        }

    worker_count = _resolve_worker_count(total, max_workers)
    input_str = str(input_path)

    jobs = []
    for item in segments:
        if len(item) >= 5:
            index, start, end, sf, ef = item[0], item[1], item[2], item[3], item[4]
        else:
            index, start, end = item[0], item[1], item[2]
            sf, ef = None, None
        jobs.append(
            (
                input_str,
                str(output_dir / f"segment_{index:03d}.mp4"),
                index,
                start,
                end,
                has_audio,
                cut_fps,
                sf,
                ef,
            )
        )

    completed = 0

    def _report_progress():
        nonlocal completed
        completed += 1
        if on_progress:
            on_progress(completed / total * 100)

    if worker_count == 1:
        for job in jobs:
            index, ok, err = _cut_segment_job(*job)
            if ok:
                outputs.append(output_dir / f"segment_{index:03d}.mp4")
            else:
                failed.append(index)
                if err:
                    errors[index] = err
            _report_progress()
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_cut_segment_job, *job) for job in jobs]
            for future in as_completed(futures):
                index, ok, err = future.result()
                if ok:
                    outputs.append(output_dir / f"segment_{index:03d}.mp4")
                else:
                    failed.append(index)
                    if err:
                        errors[index] = err
                _report_progress()

    outputs.sort(key=lambda p: p.name)
    # 切割结束后并发生成封面，列表打开时直接命中缓存
    extract_posters_parallel(outputs)
    failed.sort()
    stats = {
        "total": total,
        "success": len(outputs),
        "failed": len(failed),
        "failed_indexes": failed,
        "errors": errors,
        "workers": worker_count,
        "fps": cut_fps,
    }
    return outputs, stats


def _cut_segment_to_oss_job(
    input_ref: str,
    task_id: str,
    index: int,
    start: float,
    end: float,
    has_audio: bool,
    fps: float,
    start_frame: int | None,
    end_frame: int | None,
    clips_prefix: str | None = None,
) -> tuple[int, bool, str, str | None, str | None]:
    """FFmpeg stdout → OSS multipart；返回 index/ok/error/video_key/thumb_key。"""
    from backend.config import OSS_PROCESS_SIGN_EXPIRES
    from backend.storage import oss_client

    if start_frame is None or end_frame is None or end_frame <= start_frame:
        start_frame, end_frame, _ = _half_open_frame_range(start, end, fps)
    start_frame, end_frame, frame_count = _clamp_frame_range_to_source(
        input_ref, int(start_frame), int(end_frame), fps
    )
    expected_duration = frame_count * _frame_duration(fps)
    filename = f"segment_{index:03d}.mp4"
    key = (
        f"{clips_prefix.rstrip('/')}/{filename}"
        if clips_prefix
        else oss_client.staging_object_key(task_id, filename)
    )
    attempts = [(True, has_audio), (False, has_audio)]
    if has_audio:
        attempts.append((True, False))
    last_error = "切割失败"

    for precise_decode, use_audio in attempts:
        oss_client.delete_object(key)
        cmd = _build_cut_command(
            input_ref,
            "pipe:1",
            start,
            end,
            use_audio,
            fps=fps,
            precise_decode=precise_decode,
            start_frame=start_frame,
            end_frame=end_frame,
            fragmented=True,
        )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        stderr_tail: deque[bytes] = deque(maxlen=40)

        def _drain_stderr() -> None:
            while True:
                line = proc.stderr.readline()
                if not line:
                    break
                stderr_tail.append(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()
        try:
            oss_client.multipart_upload_fileobj(
                key, proc.stdout, content_type="video/mp4"
            )
            proc.stdout.close()
            return_code = proc.wait(timeout=30)
            stderr_thread.join(timeout=2)
            proc.stderr.close()
            if return_code != 0:
                raise FFmpegError(
                    _redact_signed_urls(
                        b"".join(stderr_tail).decode("utf-8", errors="ignore")[-500:]
                    )
                    or "FFmpeg 流式切割失败"
                )
            staged_url = oss_client.sign_url(
                key, expires=OSS_PROCESS_SIGN_EXPIRES, prefer_cdn=False
            )
            _validate_cut(staged_url, expected_duration, frame_count, fps)

            thumb_key: str | None = None
            try:
                with tempfile.TemporaryDirectory(prefix="clip_thumb_") as tmp:
                    poster = Path(tmp) / f"segment_{index:03d}.jpg"
                    extract_poster(staged_url, poster)
                    thumb_key = str(Path(key).with_suffix(".jpg"))
                    oss_client.upload_file(poster, thumb_key)
            except Exception:
                thumb_key = None
            return index, True, "", key, thumb_key
        except Exception as exc:
            last_error = str(exc)[:500]
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            stderr_thread.join(timeout=2)
            proc.stdout.close()
            proc.stderr.close()
            oss_client.delete_object(key)

    return index, False, last_error, None, None


def cut_segments_to_oss(
    input_ref: str,
    task_id: str,
    segments: list[tuple],
    on_progress=None,
    max_workers: int | None = None,
    fps: float | None = None,
    clips_prefix: str | None = None,
) -> tuple[dict[int, dict[str, str | None]], dict]:
    """并行流式切割到 OSS 预存目录，不创建完整本地媒体文件。"""
    info = get_video_info(input_ref)
    has_audio = bool(info.get("has_audio", True))
    cut_fps = fps or info.get("fps", DEFAULT_FPS) or DEFAULT_FPS
    total = len(segments)
    results: dict[int, dict[str, str | None]] = {}
    errors: dict[int, str] = {}
    if not total:
        return results, {"total": 0, "success": 0, "failed": 0, "failed_indexes": [], "errors": {}}

    jobs = []
    for item in segments:
        index, start, end = item[:3]
        sf, ef = (item[3], item[4]) if len(item) >= 5 else (None, None)
        jobs.append((input_ref, task_id, index, start, end, has_audio, cut_fps, sf, ef, clips_prefix))

    worker_count = _resolve_worker_count(total, max_workers)
    completed = 0
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_cut_segment_to_oss_job, *job) for job in jobs]
        for future in as_completed(futures):
            index, ok, err, key, thumb_key = future.result()
            completed += 1
            if ok and key:
                results[index] = {"staging_oss_key": key, "thumb_oss_key": thumb_key}
            else:
                errors[index] = err
            if on_progress:
                on_progress(completed / total * 100)
    failed = sorted(errors)
    return results, {
        "total": total,
        "success": len(results),
        "failed": len(failed),
        "failed_indexes": failed,
        "errors": errors,
        "workers": worker_count,
    }


def segment_poster_path(video_path: Path) -> Path:
    """切割成品封面图路径（与 mp4 同目录同名 .jpg）。"""
    return Path(video_path).with_suffix(".jpg")


def extract_poster(video_path: Path | str, out_path: Path | None = None) -> Path:
    """抽取视频首帧作为封面 JPEG（小图、快速 seek）。已存在且非空则直接复用。"""
    source = str(video_path)
    is_remote = source.startswith(("http://", "https://"))
    src = Path(source) if not is_remote else None
    out = Path(out_path) if out_path else segment_poster_path(Path(source))
    if out.exists() and out.stat().st_size >= 32:
        return out
    if src is not None and not src.exists():
        raise FFmpegError(f"视频不存在: {src}")

    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _resolve_ffmpeg()
    # -ss 放在 -i 前：关键帧粗定位，封面生成更快；缩到约 480 宽减小体积
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "0",
        "-an",
        "-sn",
        "-i",
        source,
        "-vf",
        "scale=480:-2",
        "-frames:v",
        "1",
        "-q:v",
        "5",
        str(out),
    ]
    try:
        run_text(cmd, check=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError("抽取封面超时") from exc
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc)).strip()
        raise FFmpegError(f"抽取封面失败: {err[:300]}") from exc
    if not out.exists() or out.stat().st_size < 32:
        raise FFmpegError("抽取封面失败: 输出为空")
    return out


def extract_posters_parallel(video_paths: list[Path], max_workers: int = 4) -> None:
    """批量并发生成封面，切割完成后立刻暖好缓存。"""
    paths = [Path(p) for p in video_paths if Path(p).exists()]
    if not paths:
        return
    workers = max(1, min(max_workers, len(paths)))

    def _one(p: Path) -> None:
        try:
            extract_poster(p)
        except FFmpegError:
            pass

    if workers == 1:
        for p in paths:
            _one(p)
        return
    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(_one, paths))
