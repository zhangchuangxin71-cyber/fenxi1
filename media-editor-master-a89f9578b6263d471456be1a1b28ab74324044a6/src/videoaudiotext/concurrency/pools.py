"""Process-wide thread pools for API jobs and CLIP/ffmpeg parallel work."""

from __future__ import annotations

import atexit
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from videoaudiotext.concurrency.limits import (
    global_job_workers,
    per_process_clip_workers,
    per_process_job_workers,
)

T = TypeVar("T")
R = TypeVar("R")

_job_lock = threading.Lock()
_job_pool: ThreadPoolExecutor | None = None

_clip_lock = threading.Lock()
_clip_pool: ThreadPoolExecutor | None = None
_clip_pool_size = 0


def _clip_worker_limit() -> int:
    from videoaudiotext.config.output import CLIP_BUILD_WORKERS

    return CLIP_BUILD_WORKERS


def _get_job_pool() -> ThreadPoolExecutor:
    global _job_pool
    if _job_pool is not None:
        return _job_pool
    with _job_lock:
        if _job_pool is None:
            _job_pool = ThreadPoolExecutor(
                max_workers=per_process_job_workers(),
                thread_name_prefix="api-job",
            )
    return _job_pool


def _get_clip_pool() -> ThreadPoolExecutor:
    global _clip_pool, _clip_pool_size
    limit = per_process_clip_workers(_clip_worker_limit())
    if _clip_pool is not None and _clip_pool_size == limit:
        return _clip_pool
    with _clip_lock:
        if _clip_pool is not None and _clip_pool_size != limit:
            _clip_pool.shutdown(wait=False, cancel_futures=True)
            _clip_pool = None
        if _clip_pool is None:
            _clip_pool = ThreadPoolExecutor(
                max_workers=limit,
                thread_name_prefix="clip",
            )
            _clip_pool_size = limit
    return _clip_pool


def submit_api_job(
    fn: Callable[..., R],
    /,
    *args: object,
    job_id: str | None = None,
    should_run: Callable[[], bool] | None = None,
    **kwargs: object,
) -> None:
    """Schedule *fn* on the process job pool with optional cross-process slot gating."""
    from videoaudiotext.concurrency.coordination import global_job_slot

    def wrapped() -> None:
        slot_id = job_id or f"anon-{id(wrapped)}"
        with global_job_slot(slot_id, should_run=should_run) as active:
            if not active:
                return
            fn(*args, **kwargs)

    _get_job_pool().submit(wrapped)


def parallel_map(fn: Callable[[T], R], items: Sequence[T]) -> list[R]:
    if not items:
        return []
    if len(items) == 1 or _clip_worker_limit() <= 1:
        return [fn(x) for x in items]
    return list(_get_clip_pool().map(fn, items))


def parallel_starmap(fn: Callable[..., R], arg_tuples: Sequence[tuple]) -> list[R]:
    if not arg_tuples:
        return []
    if len(arg_tuples) == 1 or _clip_worker_limit() <= 1:
        return [fn(*args) for args in arg_tuples]
    return list(_get_clip_pool().map(lambda args: fn(*args), arg_tuples))


def _shutdown_pools() -> None:
    global _job_pool, _clip_pool, _clip_pool_size
    with _job_lock:
        if _job_pool is not None:
            _job_pool.shutdown(wait=False, cancel_futures=True)
            _job_pool = None
    with _clip_lock:
        if _clip_pool is not None:
            _clip_pool.shutdown(wait=False, cancel_futures=True)
            _clip_pool = None
            _clip_pool_size = 0


def _reset_for_tests() -> None:
    """Tear down singleton pools so tests can change env limits."""
    _shutdown_pools()


atexit.register(_shutdown_pools)

GLOBAL_JOB_WORKERS = global_job_workers()
