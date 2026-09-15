from __future__ import annotations

import math
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.schemas import (
    DocumentMetaRequest,
    DocumentMetaResponse,
    DocumentRawRepairAccepted,
    DocumentRawRequest,
    DocumentRawResponse,
    DocumentRawStatusRequest,
    DocumentRawStatusResponse,
    DocumentRouteRequest,
    DocumentRouteResponse,
    HealthResponse,
    ReadyResponse,
    RetrieveRequest,
    RetrieveResponse,
)
from app.config.settings import Settings, get_settings
from app.container import AppContainer
from app.core.errors import ApiError


def _error(error: ApiError) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "details": error.details,
        }
    }


def create_app(
    *, settings: Settings | None = None, container: AppContainer | Any | None = None
) -> FastAPI:
    runtime = container or AppContainer.build(settings or get_settings())

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(
        title=runtime.settings.app_name, version=runtime.settings.app_version, lifespan=lifespan
    )
    app.state.container = runtime

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, error: ApiError) -> JSONResponse:
        headers: dict[str, str] = {}
        if error.code in {"RATE_LIMITED", "SERVICE_OVERLOADED", "ADMISSION_TIMEOUT"}:
            headers["Retry-After"] = str(math.ceil(float(error.details.get("retry_after", 1))))
        return JSONResponse(status_code=error.status_code, content=_error(error), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, error: RequestValidationError) -> JSONResponse:
        safe_errors = [
            {"loc": list(item.get("loc", ())), "msg": item.get("msg"), "type": item.get("type")}
            for item in error.errors()
        ]
        api_error = ApiError(
            422,
            "REQUEST_VALIDATION_ERROR",
            "request body validation failed",
            details={"errors": safe_errors},
        )
        return JSONResponse(status_code=422, content=_error(api_error))

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(
            ok=True,
            service=runtime.settings.app_name,
            version=runtime.settings.app_version,
            env=runtime.settings.app_env,
        )

    @app.get("/readyz", response_model=ReadyResponse)
    async def readyz() -> ReadyResponse:
        try:
            await runtime.ready()
        except Exception as exc:
            raise ApiError(
                503, "DATABASE_UNAVAILABLE", "database is not ready", retryable=True
            ) from exc
        return ReadyResponse(ok=True, service=runtime.settings.app_name, database="ok")

    @app.post(f"{runtime.settings.rag_api_prefix}/retrieve", response_model=RetrieveResponse)
    async def retrieve(body: RetrieveRequest) -> RetrieveResponse:
        _enforce_rate_limit(runtime, body.user_id)
        async with runtime.admission.admit(body.user_id):
            return await runtime.engine.retrieve(body)

    @app.post(
        f"{runtime.settings.rag_api_prefix}/documents/meta",
        response_model=DocumentMetaResponse,
    )
    async def documents_meta(body: DocumentMetaRequest) -> DocumentMetaResponse:
        _enforce_rate_limit(runtime, body.user_id)
        return await runtime.documents.meta(body)

    @app.post(
        f"{runtime.settings.rag_api_prefix}/documents/route",
        response_model=DocumentRouteResponse,
    )
    async def documents_route(body: DocumentRouteRequest) -> DocumentRouteResponse:
        _enforce_rate_limit(runtime, body.user_id)
        async with runtime.admission.admit(body.user_id):
            return await runtime.document_routes.route(body)

    @app.post(
        f"{runtime.settings.rag_api_prefix}/documents/raw",
        response_model=DocumentRawResponse | DocumentRawRepairAccepted,
    )
    async def documents_raw(body: DocumentRawRequest):
        _enforce_rate_limit(runtime, body.user_id)
        response = await runtime.documents.raw(body)
        if isinstance(response, DocumentRawRepairAccepted):
            return JSONResponse(status_code=202, content=response.model_dump(mode="json"))
        return response

    @app.get(
        f"{runtime.settings.rag_api_prefix}/documents/raw/status",
        response_model=DocumentRawStatusResponse,
    )
    async def documents_raw_status(
        query: Annotated[DocumentRawStatusRequest, Depends()],
    ) -> DocumentRawStatusResponse:
        _enforce_rate_limit(runtime, query.user_id)
        return await runtime.documents.raw_status(query)

    return app


def _enforce_rate_limit(runtime: Any, user_id: str) -> None:
    allowed, retry_after = runtime.limiter.allow(user_id)
    if not allowed:
        raise ApiError(
            429,
            "RATE_LIMITED",
            "user retrieval request rate exceeded",
            retryable=True,
            details={"retry_after": retry_after},
        )


app = create_app()
