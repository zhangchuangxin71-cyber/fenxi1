from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import cv2
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from .config import ServiceConfig
from .errors import MediaDownloadError, ValidationError
from .user_errors import (
    REASON_DOWNLOAD_TIMEOUT,
    REASON_EMPTY_FILE,
    REASON_FILE_TOO_LARGE,
    REASON_OSS_NOT_CONFIGURED,
)
from .oss_client import create_oss_bucket
from .media_types import suffix_for_content_type
from .runtime_cleanup import register_video_workspace, unregister_video_workspace
from .schemas import OssMediaRef

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadedVideo:
    path: Path
    source: str
    content_type: str
    bucket: str | None
    object_key: str


def _download_oss_to_file(ref: OssMediaRef, path: Path, config: ServiceConfig) -> None:
    try:
        import oss2
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise MediaDownloadError(
            "oss2 is required for object_key access: pip install oss2",
            details={"reason": REASON_OSS_NOT_CONFIGURED},
        ) from exc

    try:
        bucket = create_oss_bucket(config)
    except ValueError as exc:
        raise ValidationError(str(exc), details={"reason": REASON_OSS_NOT_CONFIGURED}) from exc

    max_attempts = 5
    base_delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            bucket.get_object_to_file(ref.object_key, str(path))
            return
        except (ReadTimeout, RequestsConnectionError, TimeoutError) as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "OSS download timed out for %s (attempt %s/%s, timeout=%.1fs); retrying in %.1fs",
                ref.object_key,
                attempt,
                max_attempts,
                config.download_timeout,
                delay,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise MediaDownloadError(
        f"Failed to download OSS object after {max_attempts} attempts: {ref.object_key}",
        details={
            "reason": REASON_DOWNLOAD_TIMEOUT,
            "object_key": ref.object_key,
            "attempts": max_attempts,
            "timeout_seconds": config.download_timeout,
        },
    ) from last_exc


def load_video(ref: OssMediaRef, config: ServiceConfig) -> LoadedVideo:
    temp_dir = Path(config.media_tmp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = suffix_for_content_type(ref.content_type)
    fd, tmp_name = tempfile.mkstemp(suffix=suffix, dir=str(temp_dir))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        _download_oss_to_file(ref, tmp_path, config)
        size = tmp_path.stat().st_size
        if size == 0:
            raise MediaDownloadError(
                f"OSS object is empty: {ref.object_key}",
                details={
                    "reason": REASON_EMPTY_FILE,
                    "bucket": config.aliyun_oss_bucket,
                    "object_key": ref.object_key,
                    "size_bytes": 0,
                },
            )
        max_bytes = config.max_video_mb * 1024 * 1024
        if max_bytes > 0 and size > max_bytes:
            raise ValidationError(
                f"video exceeds max size: {size} bytes > {max_bytes} bytes",
                details={
                    "reason": REASON_FILE_TOO_LARGE,
                    "object_key": ref.object_key,
                    "size_bytes": size,
                    "max_bytes": max_bytes,
                    "max_video_mb": config.max_video_mb,
                },
            )
        register_video_workspace(tmp_path)
        return LoadedVideo(
            path=tmp_path,
            source="oss",
            content_type=ref.content_type,
            bucket=config.aliyun_oss_bucket,
            object_key=ref.object_key,
        )
    except Exception:
        unregister_video_workspace(tmp_path)
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def cleanup_video_workspace(video_path: Path) -> list[str]:
    """
    Clean up video file and associated directories.
    
    Returns:
        List of error messages (empty if all cleanup succeeded)
    """
    errors = []
    unregister_video_workspace(video_path)

    # Clean up video file
    try:
        if video_path.exists():
            video_path.unlink(missing_ok=True)
            logger.debug(f"Cleaned up video file: {video_path}")
    except Exception as exc:
        error_msg = f"Failed to delete video {video_path}: {exc}"
        errors.append(error_msg)
        logger.warning(error_msg)

    # Clean up frames directory
    frames_dir = video_path.parent / f"{video_path.stem}_frames"
    try:
        if frames_dir.exists():
            shutil.rmtree(frames_dir)
            logger.debug(f"Cleaned up frames directory: {frames_dir}")
    except Exception as exc:
        error_msg = f"Failed to delete frames dir {frames_dir}: {exc}"
        errors.append(error_msg)
        logger.warning(error_msg)

    # Clean up segments directory
    segments_dir = video_path.parent / f"{video_path.stem}_segments"
    try:
        if segments_dir.exists():
            shutil.rmtree(segments_dir)
            logger.debug(f"Cleaned up segments directory: {segments_dir}")
    except Exception as exc:
        error_msg = f"Failed to delete segments dir {segments_dir}: {exc}"
        errors.append(error_msg)
        logger.warning(error_msg)

    if errors:
        logger.error(f"Cleanup completed with {len(errors)} errors for {video_path}")
    
    return errors


@contextmanager
def video_workspace(ref: OssMediaRef, config: ServiceConfig):
    """Context manager for video processing workspace with automatic cleanup."""
    loaded = load_video(ref, config)
    try:
        yield loaded
    finally:
        cleanup_video_workspace(loaded.path)



def extract_video_frames(video_path: Path, *, sample_fps: float, max_frames: int) -> list[tuple[float, Path]]:
    capture = None
    try:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError(f"Failed to open video: {video_path}")
        fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
        step = max(1, int(round(fps / sample_fps))) if fps > 0 else 1
        temp_dir = video_path.parent / f"{video_path.stem}_frames"
        temp_dir.mkdir(parents=True, exist_ok=True)

        frames: list[tuple[float, Path]] = []
        frame_index = -1
        while len(frames) < max_frames:
            ok, frame = capture.read()
            if not ok:
                break
            frame_index += 1
            if frame_index % step != 0:
                continue
            frame_path = temp_dir / f"{video_path.stem}_f{frame_index:06d}.jpg"
            if not cv2.imwrite(str(frame_path), frame):
                raise RuntimeError(f"Failed to write frame: {frame_path}")
            timestamp = frame_index / fps if fps > 0 else 0.0
            frames.append((round(timestamp, 3), frame_path))
        
        return frames
    finally:
        if capture is not None:
            capture.release()
