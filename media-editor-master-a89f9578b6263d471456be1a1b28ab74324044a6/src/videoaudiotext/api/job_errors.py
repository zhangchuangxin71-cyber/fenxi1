"""Structured errors persisted on compose jobs."""

from __future__ import annotations

from typing import Any

from videoaudiotext.api.errors import ApiError


class JobCancelled(Exception):
    """Compose worker observed a cooperative cancel request."""


def _category_for_http(http_status: int) -> str:
    if http_status == 502:
        return "upstream"
    if http_status == 504:
        return "timeout"
    if http_status >= 500:
        return "process"
    if http_status == 409:
        return "conflict"
    return "validation"


def job_error_dict(
    *,
    code: int,
    message: str,
    category: str,
    retryable: bool = False,
    phase: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "category": category,
        "retryable": retryable,
        "data": data or {},
    }
    if phase is not None:
        payload["phase"] = phase
    return payload


def job_error_from_api(exc: ApiError, *, phase: str | None = None) -> dict[str, Any]:
    category = _category_for_http(exc.http_status)
    retryable = exc.http_status in (502, 504) or exc.code in (50001, 50002, 50003)
    return job_error_dict(
        code=exc.code,
        message=exc.message,
        category=category,
        retryable=retryable,
        phase=phase,
        data=exc.data,
    )


def job_error_from_exception(exc: Exception, *, phase: str | None = None) -> dict[str, Any]:
    return job_error_dict(
        code=50001,
        message=str(exc) or "compose_failed",
        category="process",
        retryable=True,
        phase=phase,
    )


def job_error_cancelled(*, phase: str | None = None) -> dict[str, Any]:
    return job_error_dict(
        code=40903,
        message="job_cancelled",
        category="cancelled",
        retryable=True,
        phase=phase,
    )


def job_error_interrupted() -> dict[str, Any]:
    return job_error_dict(
        code=50003,
        message="service_interrupted",
        category="interrupted",
        retryable=True,
    )


def job_error_not_found() -> dict[str, Any]:
    return job_error_dict(
        code=40401,
        message="job not found",
        category="validation",
        retryable=False,
    )
