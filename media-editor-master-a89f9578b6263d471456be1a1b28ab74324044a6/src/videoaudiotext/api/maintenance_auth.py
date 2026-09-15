"""Optional token gate for maintenance HTTP endpoints."""

from __future__ import annotations

import os

from fastapi import Header

from videoaudiotext.api.errors import ApiError


def maintenance_token_configured() -> bool:
    return bool(os.environ.get("MAINTENANCE_API_TOKEN", "").strip())


def verify_maintenance_token(
    x_maintenance_token: str | None = Header(
        None,
        alias="X-Maintenance-Token",
        include_in_schema=maintenance_token_configured(),
    ),
) -> None:
    expected = os.environ.get("MAINTENANCE_API_TOKEN", "").strip()
    if not expected:
        return
    if (x_maintenance_token or "").strip() != expected:
        raise ApiError(401, 40101, "maintenance_token_invalid")
