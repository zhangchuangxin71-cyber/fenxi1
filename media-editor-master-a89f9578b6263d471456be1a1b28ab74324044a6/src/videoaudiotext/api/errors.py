"""API error codes aligned with docs/api-flow-b-production.md."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ApiError(Exception):
    http_status: int
    code: int
    message: str
    data: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "data": self.data or {},
        }


def bad_request(code: int, message: str, **data: Any) -> ApiError:
    return ApiError(400, code, message, data if data else None)


def not_found(message: str = "not found", **data: Any) -> ApiError:
    return ApiError(404, 40401, message, data if data else None)


def conflict(code: int, message: str, **data: Any) -> ApiError:
    return ApiError(409, code, message, data if data else None)


def upstream_failed(message: str, **data: Any) -> ApiError:
    return ApiError(502, 50201, message, data if data else None)


def process_failed(message: str, **data: Any) -> ApiError:
    return ApiError(500, 50002, message, data if data else None)


def internal_error(message: str = "internal_error", **data: Any) -> ApiError:
    return ApiError(500, 50001, message, data if data else None)

