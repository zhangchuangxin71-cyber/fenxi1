"""Structured JSON logging for the Flow B API."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from videoaudiotext.api.request_context import get_request_id

_CONFIGURED = False


def configure_api_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.environ.get("API_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logger = logging.getLogger("videoaudiotext.api")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.propagate = False
    _CONFIGURED = True


def log_event(level: int, event: str, **fields: Any) -> None:
    configure_api_logging()
    payload: dict[str, Any] = {"event": event, **fields}
    request_id = get_request_id()
    if request_id:
        payload["request_id"] = request_id
    logging.getLogger("videoaudiotext.api").log(
        level,
        json.dumps(payload, ensure_ascii=False, default=str),
    )


def log_info(event: str, **fields: Any) -> None:
    log_event(logging.INFO, event, **fields)


def log_warning(event: str, **fields: Any) -> None:
    log_event(logging.WARNING, event, **fields)


def log_error(event: str, **fields: Any) -> None:
    log_event(logging.ERROR, event, **fields)


class RequestLogMiddleware:
    """Log one structured line per HTTP request (duration, status)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500
        method = scope.get("method", "")
        path = scope.get("path", "")

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            level = logging.INFO
            if status_code >= 500:
                level = logging.ERROR
            elif status_code >= 400:
                level = logging.WARNING
            log_event(
                level,
                "http_request",
                method=method,
                path=path,
                status=status_code,
                duration_ms=duration_ms,
            )
