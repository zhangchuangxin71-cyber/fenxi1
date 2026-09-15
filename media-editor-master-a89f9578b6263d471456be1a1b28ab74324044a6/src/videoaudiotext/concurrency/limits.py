"""Concurrency limit configuration (env-driven, cluster-wide)."""

from __future__ import annotations

import os


def _default_job_workers() -> int:
    return min(4, os.cpu_count() or 2)


def uvicorn_workers() -> int:
    return max(1, min(16, int(os.environ.get("UVICORN_WORKERS", "1"))))


def global_job_workers() -> int:
    """Max concurrent API jobs across all uvicorn worker processes."""
    return max(
        1,
        min(32, int(os.environ.get("GLOBAL_JOB_WORKERS", str(_default_job_workers())))),
    )


def per_process_job_workers() -> int:
    """Thread pool size per uvicorn worker process."""
    return max(1, (global_job_workers() + uvicorn_workers() - 1) // uvicorn_workers())


def per_process_clip_workers(clip_limit: int) -> int:
    return max(1, (clip_limit + uvicorn_workers() - 1) // uvicorn_workers())
