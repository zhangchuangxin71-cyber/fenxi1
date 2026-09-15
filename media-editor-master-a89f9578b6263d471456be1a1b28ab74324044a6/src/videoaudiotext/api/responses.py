"""Unified JSON envelope."""

from __future__ import annotations

from typing import Any, TypeVar

from fastapi.responses import JSONResponse

from videoaudiotext.api.errors import ApiError
from videoaudiotext.api.request_context import data_with_request_id

T = TypeVar("T")


def ok(data: Any, *, public: bool = True) -> dict[str, Any]:
    from videoaudiotext.api.task_ids import to_public_api

    payload = to_public_api(data) if public and data is not None else data
    return {"code": 0, "message": "ok", "data": payload}


def error_response(exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "code": exc.code,
            "message": exc.message,
            "data": data_with_request_id(exc.data),
        },
    )


def error_content(
    *,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "data": data_with_request_id(data),
    }
