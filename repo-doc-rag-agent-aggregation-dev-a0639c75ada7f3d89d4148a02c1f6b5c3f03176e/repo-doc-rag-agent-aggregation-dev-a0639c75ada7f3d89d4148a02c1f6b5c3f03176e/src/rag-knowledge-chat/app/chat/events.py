from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.chat.models import AnswerBasis


@dataclass(frozen=True, slots=True)
class StatusEvent:
    stage: str
    message: str


@dataclass(frozen=True, slots=True)
class TextDeltaEvent:
    delta: str


@dataclass(frozen=True, slots=True)
class ReasoningDeltaEvent:
    delta: str


@dataclass(frozen=True, slots=True)
class CompleteEvent:
    answer_basis: AnswerBasis
    retrieval: dict[str, Any]
    references: list[dict[str, Any]] = field(default_factory=list)
    chunks: list[dict[str, Any]] = field(default_factory=list)
    debug: dict[str, Any] | None = None
    finish_reason: str = "stop"


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    code: str
    message: str
    retryable: bool
    debug: dict[str, Any] | None = None


ChatEvent = StatusEvent | ReasoningDeltaEvent | TextDeltaEvent | CompleteEvent | ErrorEvent
