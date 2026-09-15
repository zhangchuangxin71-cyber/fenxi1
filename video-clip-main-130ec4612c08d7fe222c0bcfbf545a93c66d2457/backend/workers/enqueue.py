"""API 投递后台任务（同进程 inline scheduler）。"""

from __future__ import annotations

from typing import Optional

from backend.models.schemas import SegmentInput
from backend.workers.inline_scheduler import QueueUnavailableError, scheduler
from backend.workers.import_runner import run_import
from backend.workers.task_runner import run_auto_cut, run_auto_detect, run_manual_task

__all__ = [
    "QueueUnavailableError",
    "enqueue_import_video",
    "enqueue_manual",
    "enqueue_auto_detect",
    "enqueue_auto_cut",
]


def _to_segments(segments: list[SegmentInput] | None) -> list[SegmentInput] | None:
    return segments


def enqueue_import_video(video_id: str) -> str:
    return scheduler.submit(run_import, video_id)


def enqueue_manual(task_id: str, segments: list[SegmentInput]) -> str:
    return scheduler.submit(run_manual_task, task_id, segments)


def enqueue_auto_detect(
    task_id: str,
    detector: str,
    threshold: float,
    min_scene_len: float,
    auto_cut: bool = False,
    sample_fps: Optional[float] = None,
) -> str:
    return scheduler.submit(
        run_auto_detect,
        task_id,
        detector,
        threshold,
        min_scene_len,
        auto_cut,
        sample_fps,
    )


def enqueue_auto_cut(
    task_id: str,
    segments: list[SegmentInput] | None = None,
    cut_fps: float | None = None,
) -> str:
    return scheduler.submit(
        run_auto_cut,
        task_id,
        _to_segments(segments),
        cut_fps,
    )
