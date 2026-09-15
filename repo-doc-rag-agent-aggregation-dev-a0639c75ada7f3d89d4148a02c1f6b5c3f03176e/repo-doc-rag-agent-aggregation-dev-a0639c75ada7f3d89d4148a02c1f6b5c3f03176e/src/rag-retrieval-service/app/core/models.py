from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas import RetrievalCategory, RetrievalWarning, SourceType


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateChunk(StrictModel):
    chunk_id: str
    document_id: str | None = None
    document_ids: list[str] = Field(default_factory=list)
    document_name: str | None = None
    page_number: int | None = None
    path: str
    content: str
    hint: str = ""
    source_type: SourceType
    category: RetrievalCategory
    group_matches: dict[str, Literal["accept", "possible"]] = Field(default_factory=dict)
    questions_by_group: dict[str, list[str]] = Field(default_factory=dict)
    rule_score: float | None = None
    route_score: float | None = None
    document_meta: dict | None = None
    chunk_meta: dict = Field(default_factory=dict)


class ToolOutput(StrictModel):
    chunks: list[CandidateChunk]
    warnings: list[RetrievalWarning] = Field(default_factory=list)
    coverage_complete: bool = True
    inspected_node_count: int = 0
    inspected_page_count: int = 0

    @property
    def chunk(self) -> CandidateChunk:
        if len(self.chunks) != 1:
            raise ValueError("tool output does not contain exactly one chunk")
        return self.chunks[0]
