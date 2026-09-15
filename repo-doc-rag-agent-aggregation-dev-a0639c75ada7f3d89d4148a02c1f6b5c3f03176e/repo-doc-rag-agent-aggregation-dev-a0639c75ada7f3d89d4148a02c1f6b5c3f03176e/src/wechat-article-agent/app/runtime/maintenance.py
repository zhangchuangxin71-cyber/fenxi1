from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from langgraph_sdk.errors import NotFoundError

from app.adapter.service import AdapterService
from app.artifacts.repository import ArtifactRepository
from app.config import AppSettings
from app.core.ids import thread_id_for_session
from app.runtime.client import GraphRuntimeClient

logger = logging.getLogger(__name__)


class MaintenanceWorkers:
    def __init__(
        self,
        *,
        settings: AppSettings,
        artifacts: ArtifactRepository,
        runtime: GraphRuntimeClient,
        adapter: AdapterService,
    ) -> None:
        self.settings = settings
        self.artifacts = artifacts
        self.runtime = runtime
        self.adapter = adapter
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        self._start(self._cancellation_loop(), "cancellation-reconciler")
        self._start(self._cleanup_loop(), "ttl-cleanup")

    async def close(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reconcile_cancellations_once(self) -> None:
        artifacts = await self.artifacts.list_cancelling(limit=self.settings.cancel_reconcile_batch_size)
        for artifact in artifacts:
            try:
                await self.adapter.cancel(
                    session_id=artifact.session_id,
                    response_id=artifact.current_response_id,
                )
            except Exception:
                logger.exception(
                    "cancellation reconciliation failed",
                    extra={"artifact_id": artifact.artifact_id},
                )

    async def cleanup_expired_once(self) -> None:
        sessions = await self.artifacts.expired_sessions(limit=self.settings.ttl_cleanup_batch_size)
        for session_id in sessions:
            thread_id = thread_id_for_session(self.settings.wechat_agent_thread_namespace, session_id)
            try:
                async with self.artifacts.session_lock(thread_id) as connection:
                    current = await self.artifacts.latest(session_id, connection=connection)
                    if current is None or current.expires_at >= datetime.now(UTC):
                        continue
                    try:
                        await self.runtime.delete_thread(thread_id)
                    except NotFoundError:
                        pass
                    await self.artifacts.delete_session(session_id, connection=connection)
            except Exception:
                # Artifact rows intentionally remain when thread deletion fails so a later
                # pass can retry without leaving business/checkpoint state out of sync.
                logger.exception("expired session cleanup failed", extra={"session_id": session_id})

    def _start(self, coroutine: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _cancellation_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.cancel_reconcile_interval_seconds)
            await self.reconcile_cancellations_once()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.ttl_cleanup_interval_seconds)
            await self.cleanup_expired_once()
