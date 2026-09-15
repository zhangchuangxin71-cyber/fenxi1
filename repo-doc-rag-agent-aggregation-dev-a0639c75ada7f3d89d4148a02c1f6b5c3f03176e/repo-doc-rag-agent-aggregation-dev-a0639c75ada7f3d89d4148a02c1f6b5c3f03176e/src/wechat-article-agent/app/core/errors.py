from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AppError(Exception):
    status_code: int
    code: str
    message: str
    retryable: bool = False
    stage: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def stable_payload(self, *, request_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": request_id,
        }
        if self.stage:
            payload["stage"] = self.stage
        if self.details:
            payload["details"] = self.details
        return payload


class CancelledRunError(AppError):
    def __init__(self, *, stage: str | None = None) -> None:
        super().__init__(499, "RUN_CANCELLED", "The workflow run was cancelled.", stage=stage)
