from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ServiceConfig
from .tasks import TaskManager

logger = logging.getLogger(__name__)

_active_workspaces: set[str] = set()
_workspace_lock = threading.Lock()

_STALE_LOG_GLOBS = (
    "ffmpeg_*.log",
    "benchmark_video_clip_*.log",
    "benchmark_video_clip_*.json",
    "video_clip_result_*.json",
)


@dataclass(frozen=True)
class CleanupStats:
    interrupted_jobs: int = 0
    tmp_entries_removed: int = 0
    tmp_bytes_freed: int = 0
    log_files_removed: int = 0
    log_bytes_freed: int = 0


def register_video_workspace(video_path: Path) -> None:
    """Protect in-flight video files from stale tmp sweeps."""
    video_path = video_path.resolve()
    parent = video_path.parent
    stem = video_path.stem
    paths = {
        str(video_path),
        str((parent / f"{stem}_segments").resolve()),
        str((parent / f"{stem}_frames").resolve()),
    }
    with _workspace_lock:
        _active_workspaces.update(paths)


def unregister_video_workspace(video_path: Path) -> None:
    video_path = video_path.resolve()
    parent = video_path.parent
    stem = video_path.stem
    paths = {
        str(video_path),
        str((parent / f"{stem}_segments").resolve()),
        str((parent / f"{stem}_frames").resolve()),
    }
    with _workspace_lock:
        _active_workspaces.difference_update(paths)


def _is_protected(path: Path) -> bool:
    resolved = str(path.resolve())
    with _workspace_lock:
        if resolved in _active_workspaces:
            return True
        for active in _active_workspaces:
            if resolved.startswith(f"{active}/"):
                return True
    return False


def _path_size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _remove_path(path: Path) -> int:
    try:
        size = _path_size_bytes(path)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return size
    except OSError as exc:
        logger.warning("Failed to remove %s: %s", path, exc)
        return 0


def sweep_stale_tmp(tmp_dir: Path, max_age_seconds: int) -> tuple[int, int]:
    """Remove orphaned files/dirs in media tmp older than max_age_seconds."""
    if not tmp_dir.exists():
        return 0, 0

    cutoff = time.time() - max_age_seconds
    removed = 0
    freed = 0
    for entry in tmp_dir.iterdir():
        if entry.name == ".gitkeep":
            continue
        if _is_protected(entry):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue

        freed += _remove_path(entry)
        removed += 1
        logger.info("Removed stale tmp entry: %s", entry.name)

    return removed, freed


def sweep_stale_logs(log_dir: Path, max_age_seconds: int) -> tuple[int, int]:
    """Remove old auxiliary logs; rotated embedding_service.log.* are also pruned."""
    if not log_dir.exists():
        return 0, 0

    cutoff = time.time() - max_age_seconds
    removed = 0
    freed = 0

    candidates: list[Path] = []
    for pattern in _STALE_LOG_GLOBS:
        candidates.extend(log_dir.glob(pattern))
    candidates.extend(log_dir.glob("embedding_service.log.*"))

    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            removed += 1
            freed += size
            logger.info("Removed stale log file: %s", path.name)
        except OSError as exc:
            logger.warning("Failed to remove log %s: %s", path, exc)

    return removed, freed


def run_runtime_cleanup(config: ServiceConfig, task_manager: TaskManager, *, on_startup: bool) -> CleanupStats:
    interrupted = task_manager.reset_interrupted_jobs() if on_startup else 0

    # On startup no in-flight workspaces are registered yet; remove all orphans immediately.
    tmp_max_age_seconds = 0 if on_startup else config.tmp_max_age_hours * 3600
    tmp_removed, tmp_freed = sweep_stale_tmp(
        Path(config.media_tmp_dir),
        tmp_max_age_seconds,
    )
    log_removed, log_freed = sweep_stale_logs(
        Path(config.log_dir),
        config.log_max_age_days * 86400,
    )
    return CleanupStats(
        interrupted_jobs=interrupted,
        tmp_entries_removed=tmp_removed,
        tmp_bytes_freed=tmp_freed,
        log_files_removed=log_removed,
        log_bytes_freed=log_freed,
    )


def start_periodic_cleanup(config: ServiceConfig, task_manager: TaskManager) -> None:
    interval_seconds = max(1, config.runtime_cleanup_interval_hours) * 3600

    def cleanup_loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                stats = run_runtime_cleanup(config, task_manager, on_startup=False)
                if any(
                    (
                        stats.tmp_entries_removed,
                        stats.log_files_removed,
                    )
                ):
                    logger.info(
                        "Runtime cleanup freed %.1f MB tmp / %.1f MB logs",
                        stats.tmp_bytes_freed / (1024 * 1024),
                        stats.log_bytes_freed / (1024 * 1024),
                    )
            except Exception as exc:
                logger.error("Runtime cleanup failed: %s", exc)

    thread = threading.Thread(target=cleanup_loop, daemon=True, name="RuntimeCleanup")
    thread.start()
