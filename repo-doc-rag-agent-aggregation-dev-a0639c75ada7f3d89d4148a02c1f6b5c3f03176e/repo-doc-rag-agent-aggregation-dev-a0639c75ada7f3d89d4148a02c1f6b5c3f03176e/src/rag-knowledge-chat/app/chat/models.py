from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RouteQueryText = Annotated[str, Field(max_length=2000)]


class RouteQuery(BaseModel):
    """One independently retrievable query produced by the route model."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["resolved", "ambiguous"]
    reason: str = Field(min_length=1, max_length=1000)
    rewrite_query: RouteQueryText = Field(min_length=1)

    @field_validator("reason", "rewrite_query")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("route query text must not be blank")
        return value


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    needs_retrieval: bool
    queries: list[RouteQuery] = Field(default_factory=list)
    # Kept only so older in-process callers/tests can construct a decision while
    # the wire contract transitions from `query` to `queries`.
    query: RouteQueryText | list[RouteQueryText] = Field(default_factory=list, exclude=True)
    reason_code: Literal["identity", "chitchat", "general_knowledge", "knowledge_base", "clarification"]

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_query(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "queries" not in data:
            legacy = data.get("query", [])
            legacy_items = [] if isinstance(legacy, str) and not legacy.strip() else legacy
            legacy_items = [legacy_items] if isinstance(legacy_items, str) else legacy_items
            if legacy_items is None:
                legacy_items = []
            data["queries"] = [
                {
                    "status": "resolved",
                    "reason": "legacy route decision",
                    "rewrite_query": item,
                }
                for item in legacy_items
            ]
        return data

    @model_validator(mode="after")
    def validate_route_contract(self) -> RouteDecision:
        queries = self.query_items
        if self.needs_retrieval:
            if self.reason_code != "knowledge_base" or not self.queries:
                raise ValueError("retrieval routes require non-empty queries and reason_code=knowledge_base")
        elif self.reason_code == "knowledge_base" or (
            (queries or self.ambiguous_query_items) and self.reason_code != "clarification"
        ):
            raise ValueError("non-retrieval routes require an empty query")
        self.query = [item.rewrite_query for item in self.queries if item.status == "resolved"]
        return self

    @property
    def query_items(self) -> list[str]:
        return list(
            dict.fromkeys(
                item.rewrite_query.strip()
                for item in self.queries
                if item.status == "resolved" and item.rewrite_query.strip()
            )
        )

    @property
    def ambiguous_query_items(self) -> list[str]:
        return list(
            dict.fromkeys(
                item.rewrite_query.strip()
                for item in self.queries
                if item.status == "ambiguous" and item.rewrite_query.strip()
            )
        )


class RetrievalStatus(StrEnum):
    SUCCESS = "success"
    NO_RESULT = "no_result"
    ERROR = "error"


class AnswerBasis(StrEnum):
    KNOWLEDGE_BASE = "knowledge_base"
    GENERAL_NO_RETRIEVAL = "general_no_retrieval"
    GENERAL_NO_RESULT = "general_no_result"
    GENERAL_RETRIEVAL_ERROR = "general_retrieval_error"


@dataclass(slots=True)
class RetrievalResult:
    status: RetrievalStatus
    chunks: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    debug: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    request_id: str | None = None
