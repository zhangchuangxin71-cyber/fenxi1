"""统一错误体：{code: int, message, data}；请求头 X-Request-Id。"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, NoReturn, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


# 稳定业务错误码（整数）
CODE_BAD_REQUEST = 40000
CODE_JOB_NOT_READY = 40001
CODE_INVALID_SEGMENT_IDS = 40002
CODE_IMPORT_FAILED = 40003
CODE_VIDEO_NOT_FOUND = 40401
CODE_JOB_NOT_FOUND = 40402
CODE_SEGMENT_NOT_FOUND = 40403
CODE_NOT_FOUND = 40400
CODE_CONFLICT = 40901
CODE_VALIDATION = 42201
CODE_INTERNAL = 50000
CODE_OSS_DISABLED = 50301
CODE_QUEUE_UNAVAILABLE = 50302
CODE_SERVICE_UNAVAILABLE = 50300


def _status_to_code(status: int) -> int:
    return {
        400: CODE_BAD_REQUEST,
        404: CODE_NOT_FOUND,
        409: CODE_CONFLICT,
        422: CODE_VALIDATION,
        503: CODE_SERVICE_UNAVAILABLE,
        500: CODE_INTERNAL,
    }.get(status, status * 100 if status < 1000 else status)


def fail_body(
    *,
    code: int,
    message: str,
    request_id: str | None = None,
    detail: Any = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if request_id:
        data["request_id"] = request_id
    if detail is not None:
        data["detail"] = detail
    return {"code": code, "message": message, "data": data}


def raise_api_error(
    status: int,
    code: int,
    message: str,
    detail: Any = None,
) -> NoReturn:
    """抛出带整型业务 code 的 HTTPException。"""
    body: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        body["detail"] = detail
    raise HTTPException(status_code=status, detail=body)


def _validation_errors_jsonable(exc: RequestValidationError) -> list[Any]:
    try:
        return json.loads(exc.json())
    except Exception:
        out: list[Any] = []
        for err in exc.errors():
            item = {k: v for k, v in err.items() if k != "ctx"}
            out.append(item)
        return out


def _first_validation_message(errors: list[Any], fallback: str) -> str:
    if not errors:
        return fallback
    first = errors[0]
    if isinstance(first, dict):
        msg = first.get("msg") or first.get("message")
        if isinstance(msg, str) and msg:
            if msg.startswith("Value error, "):
                return msg[len("Value error, ") :]
            return msg
    return fallback


def _request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if isinstance(rid, str) and rid:
        return rid
    return request.headers.get("X-Request-Id") or str(uuid.uuid4())


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        rid = _request_id(request)
        detail = exc.detail
        headers = dict(getattr(exc, "headers", None) or {})
        headers["X-Request-Id"] = rid

        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            code = detail["code"]
            if isinstance(code, str):
                # 兼容旧字符串 code
                code = _status_to_code(exc.status_code)
            content = fail_body(
                code=int(code),
                message=str(detail["message"]),
                request_id=rid,
                detail=detail.get("detail"),
            )
        elif isinstance(detail, str):
            content = fail_body(
                code=_status_to_code(exc.status_code),
                message=detail,
                request_id=rid,
            )
        else:
            content = fail_body(
                code=_status_to_code(exc.status_code),
                message="请求失败",
                request_id=rid,
                detail=detail,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        rid = _request_id(request)
        errors = _validation_errors_jsonable(exc)
        return JSONResponse(
            status_code=422,
            content=fail_body(
                code=CODE_VALIDATION,
                message=_first_validation_message(errors, "请求参数校验失败"),
                request_id=rid,
                detail=errors,
            ),
            headers={"X-Request-Id": rid},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        rid = _request_id(request)
        logger.exception("unhandled error path=%s request_id=%s", request.url.path, rid)
        return JSONResponse(
            status_code=500,
            content=fail_body(
                code=CODE_INTERNAL,
                message="服务内部错误",
                request_id=rid,
            ),
            headers={"X-Request-Id": rid},
        )
