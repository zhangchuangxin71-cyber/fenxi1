from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from app.adapter.service import AdapterService
from app.api.schemas import CancelRequest, ResponseRequest
from app.artifacts.repository import ArtifactRepository
from app.config import AppSettings, get_settings
from app.core.errors import AppError
from app.core.ids import prefixed_id
from app.llm.gateway import LLMGateway
from app.persistence.migrate import apply_migrations
from app.persistence.pool import create_pool
from app.runtime.admission import RunAdmissionController
from app.runtime.client import GraphRuntimeClient
from app.runtime.maintenance import MaintenanceWorkers


class SlidingWindowLimiter:
    def __init__(self, *, per_minute: int, burst: int, bucket_ttl_seconds: float) -> None:
        self.per_minute = per_minute
        self.burst = burst
        self.bucket_ttl_seconds = bucket_ttl_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._last_seen: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        now = monotonic()
        async with self._lock:
            bucket = self._buckets[key]
            while bucket and now - bucket[0] >= 60:
                bucket.popleft()
            if len(bucket) >= min(self.per_minute, self.burst):
                raise AppError(429, "REQUEST_RATE_LIMITED", "Too many requests.", True)
            bucket.append(now)
            self._last_seen[key] = now
            if len(self._last_seen) > 10_000:
                stale = [
                    value
                    for value, last_seen in self._last_seen.items()
                    if now - last_seen >= self.bucket_ttl_seconds
                ]
                for value in stale:
                    self._last_seen.pop(value, None)
                    self._buckets.pop(value, None)


@dataclass(slots=True)
class ApplicationRuntime:
    settings: AppSettings
    pool: Any
    artifacts: ArtifactRepository
    graph_runtime: GraphRuntimeClient
    llm: LLMGateway
    adapter: AdapterService
    admission: RunAdmissionController
    limiter: SlidingWindowLimiter
    maintenance: MaintenanceWorkers

    async def close(self) -> None:
        await self.maintenance.close()
        await self.adapter.close()
        await self.graph_runtime.close()
        await self.llm.close()
        await self.pool.close()


async def build_runtime(settings: AppSettings) -> ApplicationRuntime:
    await apply_migrations(settings.database_url)
    pool = create_pool(settings)
    await pool.open()
    artifacts = ArtifactRepository(
        pool,
        ttl_hours=settings.artifact_ttl_hours,
        max_retries=settings.db_max_retries,
    )
    graph_runtime = GraphRuntimeClient(
        url=str(settings.agent_server_url),
        assistant_id=settings.agent_server_assistant_id,
        thread_ttl_minutes=settings.artifact_ttl_hours * 60,
    )
    llm = LLMGateway(settings)
    admission = RunAdmissionController(
        max_active=settings.app_max_active_runs,
        max_queued=settings.app_max_queued_runs,
        wait_timeout=settings.app_admission_wait_timeout_seconds,
    )
    adapter = AdapterService(
        settings=settings,
        artifacts=artifacts,
        runtime=graph_runtime,
        llm=llm,
        admission=admission,
    )
    maintenance = MaintenanceWorkers(
        settings=settings,
        artifacts=artifacts,
        runtime=graph_runtime,
        adapter=adapter,
    )
    result = ApplicationRuntime(
        settings=settings,
        pool=pool,
        artifacts=artifacts,
        graph_runtime=graph_runtime,
        llm=llm,
        adapter=adapter,
        admission=admission,
        limiter=SlidingWindowLimiter(
            per_minute=settings.request_rate_limit_per_minute,
            burst=settings.request_rate_limit_burst,
            bucket_ttl_seconds=settings.request_rate_limit_bucket_ttl_seconds,
        ),
        maintenance=maintenance,
    )
    maintenance.start()
    return result


def create_app(settings: AppSettings | None = None) -> FastAPI:
    selected_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = await build_runtime(selected_settings)
        try:
            yield
        finally:
            await app.state.runtime.close()

    app = FastAPI(
        title="WeChat Article Agent",
        version="0.1.0",
        # API documentation is an access/contract surface, not the debug trace switch.
        # Keep it available for Apifox and other contract clients even when runtime
        # tracing is disabled.
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", prefixed_id("req"))
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.stable_payload(request_id=request_id)},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", prefixed_id("req"))
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "The request body does not match the API contract.",
                    "retryable": False,
                    "request_id": request_id,
                    "details": {"validation": exc.errors()},
                }
            },
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        request.state.request_id = request.headers.get("x-request-id") or prefixed_id("req")
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        return response

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request) -> dict[str, str]:
        runtime: ApplicationRuntime = request.app.state.runtime
        async with runtime.pool.connection() as connection:
            await connection.execute("SELECT 1")
        return {"status": "ready"}

    @app.post("/v1/responses")
    async def create_response(body: ResponseRequest, request: Request) -> StreamingResponse:
        runtime: ApplicationRuntime = request.app.state.runtime
        peer = request.client.host if request.client else "unknown"
        await runtime.limiter.check(f"{peer}:{body.context.user_id or body.context.session_id}")
        plan = await runtime.adapter.admit(body)
        return StreamingResponse(
            runtime.adapter.stream(plan),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/v1/responses/{response_id}/cancel")
    async def cancel_response(response_id: str, body: CancelRequest, request: Request) -> JSONResponse:
        runtime: ApplicationRuntime = request.app.state.runtime
        artifact, status_code = await runtime.adapter.cancel(
            session_id=body.context.session_id,
            response_id=response_id,
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "id": response_id,
                "object": "response",
                "status": artifact.status,
                "run_id": artifact.run_id,
                "artifact_id": artifact.artifact_id,
            },
        )

    return app
