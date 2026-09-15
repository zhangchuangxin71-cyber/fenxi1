from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v"}


def iter_videos(video_dir: Path):
    for path in sorted(video_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            yield path


def _decode_output(data: bytes | str | None) -> str:
    if not data:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def _ffmpeg_error_summary(details: str) -> str:
    for line in details.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("ffmpeg version") or stripped.startswith("Copyright"):
            continue
        if stripped.startswith("built with") or stripped.startswith("configuration:"):
            continue
        return stripped
    return "ffmpeg failed"


def _run_ffmpeg(command: list[str], video_path: Path):
    try:
        subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        details = _decode_output(exc.stderr or exc.stdout or b"").strip()
        logger.error("ffmpeg segment failed for %s:\n%s", video_path, details)
        summary = _ffmpeg_error_summary(details)
        raise RuntimeError(f"ffmpeg segment failed for {video_path}: {summary}") from exc


def segment_video(
    video_path: Path,
    output_dir: Path,
    segment_seconds: int,
    ffmpeg_binary: str = "ffmpeg",
    max_resolution: int = 1920,
) -> list[Path]:
    """
    Segment video into smaller clips.

    Args:
        video_path: Path to input video
        output_dir: Directory for output segments
        segment_seconds: Length of each segment in seconds
        ffmpeg_binary: Path to ffmpeg executable
        max_resolution: Maximum width/height (default 1920 for Full HD)
                       Videos larger than this will be downscaled to save memory

    Returns:
        List of segment file paths
    """
    if segment_seconds <= 0:
        raise ValueError("segment_seconds must be greater than 0")
    if max_resolution < 2:
        raise ValueError("max_resolution must be at least 2")

    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / f"{video_path.stem}_seg_%04d.mp4"

    # Base command with downscaling filter to prevent memory issues with 4K videos.
    # libx264 requires even dimensions, so we round the scaled width/height down
    # to the nearest even number and keep a minimum size of 2x2 to avoid failures
    # like "height not divisible by 2" or zero-sized outputs.
    safe_width = f"trunc(min({max_resolution},iw)/2)*2"
    safe_height = f"trunc(min({max_resolution},ih)/2)*2"
    command = [
        ffmpeg_binary,
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"scale='{safe_width}':'{safe_height}'",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        str(pattern),
    ]
    _run_ffmpeg(command, video_path)
    return sorted(output_dir.glob(f"{video_path.stem}_seg_*.mp4"))
