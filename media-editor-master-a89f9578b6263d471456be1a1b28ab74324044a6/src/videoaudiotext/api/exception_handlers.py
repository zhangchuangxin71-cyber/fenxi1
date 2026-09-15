"""FastAPI exception handlers for unified JSON error envelopes."""

from __future__ import annotations

import json
import os
import traceback

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from videoaudiotext.api.api_logging import log_error
from videoaudiotext.api.errors import ApiError
from videoaudiotext.api.responses import error_content, error_response


def register_exception_handlers(app) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError):
        log_error(
            "api_error",
            http_status=exc.http_status,
            code=exc.code,
            message=exc.message,
            data=exc.data or {},
        )
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        errors = json.loads(json.dumps(exc.errors(), default=str))
        log_error(
            "validation_error",
            http_status=422,
            code=40001,
            errors=errors,
        )
        return JSONResponse(
            status_code=422,
            content=error_content(
                code=40001,
                message="validation_error",
                data={"errors": errors},
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception):
        if isinstance(exc, ApiError):
            return error_response(exc)
        debug = os.environ.get("API_DEBUG", "").strip().lower() in ("1", "true", "yes")
        log_error(
            "internal_error",
            http_status=500,
            code=50001,
            exc_type=type(exc).__name__,
            detail=str(exc) if debug else None,
            traceback=traceback.format_exc() if debug else None,
        )
        data: dict = {}
        if debug:
            data["detail"] = str(exc)
        return JSONResponse(
            status_code=500,
            content=error_content(code=50001, message="internal_error", data=data),
        )
