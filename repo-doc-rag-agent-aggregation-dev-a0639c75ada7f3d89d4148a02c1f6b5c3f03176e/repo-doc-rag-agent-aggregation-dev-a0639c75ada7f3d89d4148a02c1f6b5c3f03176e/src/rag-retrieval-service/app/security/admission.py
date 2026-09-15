from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.core.errors import ApiError


@dataclass(slots=True)
class _Ticket:
    user_id: str
    ready: asyncio.Future[None]
    granted: bool = False
    released: bool = False


class RetrievalAdmissionController:
    """Bound active retrievals and fairly queue users within one process."""

    def __init__(self, *, max_active: int, max_queued: int, wait_timeout_seconds: float) -> None:
        self.max_active = max(1, int(max_active))
        self.max_queued = max(1, int(max_queued))
        self.wait_timeout_seconds = max(0.01, float(wait_timeout_seconds))
        self._lock = asyncio.Lock()
        self._active_count = 0
        self._queued_count = 0
        self._queues: dict[str, deque[_Ticket]] = {}
        self._rotation: deque[str] = deque()

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def queued_count(self) -> int:
        return self._queued_count

    async def acquire(self, user_id: str) -> _Ticket:
        loop = asyncio.get_running_loop()
        ticket = _Ticket(user_id=user_id, ready=loop.create_future())
        async with self._lock:
            if self._active_count < self.max_active and self._queued_count == 0:
                ticket.granted = True
                self._active_count += 1
                ticket.ready.set_result(None)
                return ticket
            if self._queued_count >= self.max_queued:
                raise ApiError(
                    503,
                    "SERVICE_OVERLOADED",
                    "retrieval execution and waiting capacity are full",
                    retryable=True,
                    details={"retry_after": 1},
                )
            queue = self._queues.get(user_id)
            if queue is None:
                queue = deque()
                self._queues[user_id] = queue
                self._rotation.append(user_id)
            queue.append(ticket)
            self._queued_count += 1

        try:
            await asyncio.wait_for(asyncio.shield(ticket.ready), timeout=self.wait_timeout_seconds)
            return ticket
        except TimeoutError as exc:
            await self._withdraw(ticket)
            raise ApiError(
                503,
                "ADMISSION_TIMEOUT",
                "retrieval request timed out while waiting for execution capacity",
                retryable=True,
                details={"retry_after": 1},
            ) from exc
        except asyncio.CancelledError:
            await self._withdraw(ticket)
            raise

    async def release(self, ticket: _Ticket) -> None:
        async with self._lock:
            if ticket.released:
                return
            ticket.released = True
            if ticket.granted:
                ticket.granted = False
                self._active_count = max(0, self._active_count - 1)
            self._dispatch_locked()

    @asynccontextmanager
    async def admit(self, user_id: str) -> AsyncIterator[None]:
        ticket = await self.acquire(user_id)
        try:
            yield
        finally:
            await self.release(ticket)

    async def _withdraw(self, ticket: _Ticket) -> None:
        async with self._lock:
            if ticket.released:
                return
            ticket.released = True
            if ticket.granted:
                ticket.granted = False
                self._active_count = max(0, self._active_count - 1)
                self._dispatch_locked()
                return
            queue = self._queues.get(ticket.user_id)
            if queue is not None:
                try:
                    queue.remove(ticket)
                    self._queued_count = max(0, self._queued_count - 1)
                except ValueError:
                    pass
                if not queue:
                    self._queues.pop(ticket.user_id, None)
                    self._rotation = deque(
                        value for value in self._rotation if value != ticket.user_id
                    )

    def _dispatch_locked(self) -> None:
        while self._active_count < self.max_active and self._rotation:
            user_id = self._rotation.popleft()
            queue = self._queues.get(user_id)
            if not queue:
                self._queues.pop(user_id, None)
                continue
            ticket = queue.popleft()
            self._queued_count = max(0, self._queued_count - 1)
            if queue:
                self._rotation.append(user_id)
            else:
                self._queues.pop(user_id, None)
            if ticket.released or ticket.ready.cancelled():
                continue
            ticket.granted = True
            self._active_count += 1
            if not ticket.ready.done():
                ticket.ready.set_result(None)
