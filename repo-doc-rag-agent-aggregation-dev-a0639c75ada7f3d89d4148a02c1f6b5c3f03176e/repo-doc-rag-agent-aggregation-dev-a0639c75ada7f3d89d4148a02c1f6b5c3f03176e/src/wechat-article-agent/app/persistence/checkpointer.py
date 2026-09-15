from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import get_settings


@asynccontextmanager
async def create_checkpointer(_config: object | None = None) -> AsyncIterator[AsyncPostgresSaver]:
    settings = get_settings()
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
        await saver.setup()
        yield saver
