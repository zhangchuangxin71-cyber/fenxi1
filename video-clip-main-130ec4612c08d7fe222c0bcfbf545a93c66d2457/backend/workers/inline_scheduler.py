"""同进程任务调度：有界并发 + asyncio.to_thread，替代 Celery/Redis。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from backend.config import INLINE_MAX_JOBS

logger = logging.getLogger(__name__)


class QueueUnavailableError(RuntimeError):
    """调度器不可用或已满载拒绝。"""


class InlineScheduler:
    def __init__(self) -> None:
        self._max_jobs = max(1, int(INLINE_MAX_JOBS))
        self._sem: asyncio.Semaphore | None = None
        self._started = False
        self._shutting_down = False
        self._active = 0
        self._accepted = 0

    @property
    def max_jobs(self) -> int:
        return self._max_jobs

    def start(self) -> None:
        self._sem = asyncio.Semaphore(self._max_jobs)
        self._started = True
        self._shutting_down = False
        logger.info("inline scheduler started max_jobs=%s", self._max_jobs)

    def shutdown(self) -> None:
        self._shutting_down = True
        logger.info("inline scheduler shutting down active=%s", self._active)

    def stats(self) -> dict[str, Any]:
        return {
            "ok": bool(self._started and not self._shutting_down),
            "running": self._active,
            "max_jobs": self._max_jobs,
            "accepted": self._accepted,
            "mode": "inline",
        }

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        if not self._started or self._sem is None:
            raise QueueUnavailableError("任务调度器未启动")
        if self._shutting_down:
            raise QueueUnavailableError("服务正在关闭，拒绝新任务")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise QueueUnavailableError("任务调度器不可用：无事件循环") from exc

        # 简单背压：活跃任务达到上限时拒绝（信号量仍限制真正并行）
        if self._active >= self._max_jobs * 2:
            raise QueueUnavailableError(
                f"后台任务过载（运行中 {self._active}），请稍后重试"
            )

        token = str(uuid.uuid4())
        self._accepted += 1
        loop.create_task(self._run(fn, args, kwargs), name=f"inline-{token[:8]}")
        return token

    async def _run(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        assert self._sem is not None
        async with self._sem:
            self._active += 1
            try:
                await asyncio.to_thread(fn, *args, **kwargs)
            except Exception:
                logger.exception("inline task failed fn=%s", getattr(fn, "__name__", fn))
            finally:
                self._active = max(0, self._active - 1)


scheduler = InlineScheduler()
