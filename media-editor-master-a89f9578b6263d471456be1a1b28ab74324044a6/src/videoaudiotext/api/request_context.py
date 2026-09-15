"""Per-request context (request ID) for logging and error envelopes."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token

_REQUEST_ID: ContextVar[str | None] = ContextVar("api_request_id", default=None)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


def generate_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


def normalize_request_id(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if _REQUEST_ID_RE.fullmatch(candidate):
        return candidate
    return None


def set_request_id(request_id: str) -> Token:
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token) -> None:
    _REQUEST_ID.reset(token)


def get_request_id() -> str | None:
    return _REQUEST_ID.get()


def data_with_request_id(data: dict | None = None) -> dict:
    payload = dict(data or {})
    request_id = get_request_id()
    if request_id and "request_id" not in payload:
        payload["request_id"] = request_id
    return payload
