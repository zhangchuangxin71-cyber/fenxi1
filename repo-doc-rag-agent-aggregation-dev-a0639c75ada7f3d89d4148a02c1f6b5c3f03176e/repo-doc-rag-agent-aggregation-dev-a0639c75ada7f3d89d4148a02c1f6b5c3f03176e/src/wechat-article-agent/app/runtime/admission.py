from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.core.errors import AppError


@dataclass(slots=True)
class RunPermit:
    controller: RunAdmissionController
    released: bool = False

    async def release(self) -> None:
        if not self.released:
            self.released = True
            await self.controller.release()


class RunAdmissionController:
    """Bound active Agent Server fragments and queue excess local requests."""

    def __init__(self, *, max_active: int, max_queued: int, wait_timeout: float) -> None:
        self.max_active = max_active
        self.max_queued = max_queued
        self.wait_timeout = wait_timeout
        self.active = 0
        self.queued = 0
        self._condition = asyncio.Condition()

    async def acquire(self) -> RunPermit:
        async with self._condition:
            if self.active < self.max_active:
                self.active += 1
                return RunPermit(self)
            if self.max_queued == 0 or self.queued >= self.max_queued:
                raise AppError(503, "RUN_QUEUE_FULL", "The workflow run queue is full.", True)
            self.queued += 1
            try:
                async with asyncio.timeout(self.wait_timeout):
                    while self.active >= self.max_active:
                        await self._condition.wait()
                self.active += 1
                return RunPermit(self)
            except TimeoutError as exc:
                raise AppError(
                    503,
                    "RUN_QUEUE_TIMEOUT",
                    "The workflow waited too long for an execution slot.",
                    True,
                ) from exc
            finally:
                self.queued -= 1

    async def release(self) -> None:
        async with self._condition:
            self.active = max(0, self.active - 1)
            self._condition.notify_all()
