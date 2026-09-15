from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScopeToolPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: Literal["get_docs_metainfo", "get_docs_description"]
    metainfo_fields: list[Literal["doc_name", "page_count", "chapter_count"]] = Field(
        default_factory=list
    )
    enumeration_limit: int = Field(default=10, ge=1)


class ScopePlanError(ValueError):
    pass
