from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.dependencies import ServiceContainer
from app.api.routes import router
from app.chat.orchestrator import ChatOrchestrator
from app.integrations.ark import ArkClient
from app.integrations.retrieval import RetrievalClient
from app.platform.auth import ApiKeyAuthenticator
from app.platform.errors import ApiError
from app.platform.rate_limit import InMemoryRateLimiter
from app.platform.settings import (
    ARK_TIMEOUT_SECONDS,
    MAX_REQUEST_BYTES,
    REQUEST_DEADLINE_SECONDS,
    REQUEST_FINALIZATION_RESERVE_SECONDS,
    Settings,
)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", f"req_{uuid.uuid4().hex}")


def create_app(
    *,
    settings: Settings | None = None,
    ark: Any | None = None,
    retrieval: Any | None = None,
    request_limiter: Any | None = None,
    ark_limiter: Any | None = None,
) -> FastAPI:
    config = settings or Settings()
    closeables: list[Any] = []
    if ark is None:
        ark = ArkClient(
            api_key=config.ark_api_key,
            base_url=config.ark_base_url,
            model=config.model_smart,
            thinking_type=config.doubao_thinking_type,
            timeout_seconds=ARK_TIMEOUT_SECONDS,
        )
        closeables.append(ark)
    if retrieval is None:
        retrieval = RetrievalClient(
            base_url=config.retrieval_url,
            timeout_seconds=config.rag_retrieval_timeout_seconds,
            max_return_tokens=config.rag_max_return_tokens,
        )
        closeables.append(retrieval)
    request_limiter = request_limiter or InMemoryRateLimiter(limit=config.request_rpm_limit)
    ark_limiter = ark_limiter or InMemoryRateLimiter(limit=config.ark_rpm_limit)
    orchestrator = ChatOrchestrator(
        ark=ark,
        retrieval=retrieval,
        ark_limiter=ark_limiter,
        ark_max_concurrency=config.ark_max_concurrency,
        debug_enabled=config.debug_enabled,
        deadline_seconds=max(
            REQUEST_DEADLINE_SECONDS,
            config.rag_retrieval_timeout_seconds + ARK_TIMEOUT_SECONDS + REQUEST_FINALIZATION_RESERVE_SECONDS,
        ),
    )
    services = ServiceContainer(
        settings=config,
        authenticator=ApiKeyAuthenticator(
            enabled=config.auth_enabled,
            allowed_keys=config.allowed_api_keys,
        ),
        request_limiter=request_limiter,
        ark_limiter=ark_limiter,
        orchestrator=orchestrator,
        closeables=tuple(closeables),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for resource in services.closeables:
            close = getattr(resource, "close", None)
            if close:
                await close()

    application = FastAPI(
        title="rag-knowledge-chat",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.services = services

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex}"
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_BYTES:
            error = ApiError(
                413,
                "request_too_large",
                "request body exceeds the configured size limit",
            )
            response = JSONResponse(
                status_code=error.status_code,
                content=error.payload(request.state.request_id),
            )
        else:
            response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @application.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.payload(_request_id(request)),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "location": [str(item) for item in error["loc"]],
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        error = ApiError(
            400,
            "invalid_request",
            "request validation failed",
            details={"validation_errors": details},
        )
        return JSONResponse(
            status_code=400,
            content=error.payload(_request_id(request)),
        )

    application.include_router(router)

    default_openapi = application.openapi

    def openapi_with_sse_contract() -> dict[str, Any]:
        schema = default_openapi()
        response = schema["paths"]["/v1/chat/completions"]["post"]["responses"]["200"]
        content = response.get("content", {})
        event_schema = content.pop("application/json", None)
        if event_schema is not None:
            content["text/event-stream"] = event_schema
        response["content"] = content
        return schema

    application.openapi = openapi_with_sse_contract
    return application


app = create_app()
