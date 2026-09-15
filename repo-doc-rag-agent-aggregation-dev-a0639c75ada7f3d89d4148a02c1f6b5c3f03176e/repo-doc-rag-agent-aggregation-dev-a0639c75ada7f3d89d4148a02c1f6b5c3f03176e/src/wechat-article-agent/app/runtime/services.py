from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.artifacts.repository import ArtifactRepository
from app.config import AppSettings, get_settings
from app.integrations.retrieval import RetrievalClient
from app.integrations.seedream import SeedreamClient
from app.llm.gateway import LLMGateway
from app.persistence.migrate import apply_migrations
from app.persistence.pool import create_pool


@dataclass(slots=True)
class GraphServices:
    settings: AppSettings
    artifacts: ArtifactRepository
    llm: LLMGateway
    retrieval: RetrievalClient
    seedream: SeedreamClient


_services: GraphServices | None = None
_services_lock = asyncio.Lock()


async def get_graph_services() -> GraphServices:
    global _services
    if _services is not None:
        return _services
    async with _services_lock:
        if _services is not None:
            return _services
        settings = get_settings()
        await apply_migrations(settings.database_url)
        pool = create_pool(settings)
        await pool.open()
        _services = GraphServices(
            settings=settings,
            artifacts=ArtifactRepository(
                pool,
                ttl_hours=settings.artifact_ttl_hours,
                max_retries=settings.db_max_retries,
            ),
            llm=LLMGateway(settings),
            retrieval=RetrievalClient(settings),
            seedream=SeedreamClient(settings),
        )
        return _services
