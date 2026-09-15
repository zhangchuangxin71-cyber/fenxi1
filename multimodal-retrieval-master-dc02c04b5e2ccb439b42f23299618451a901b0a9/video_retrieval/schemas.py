from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SearchMode = Literal["clip", "hybrid", "both"]
PipelineStep = Literal["clip", "hybrid", "both"]


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    mode: SearchMode = Field(default="both")
    top_k: int = Field(default=10, ge=1, le=50)
    sparse_top_k: int | None = Field(default=None, ge=1, le=500)
    dense_top_k: int | None = Field(default=None, ge=1, le=500)
    rrf_k: int | None = Field(default=None, ge=1, le=500)


class SearchResultBlock(BaseModel):
    mode: str
    profile: str | None = None
    index_count: int = 0
    elapsed_ms: float = 0.0
    ready: bool = True
    error: str | None = None
    results: list[dict] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    mode: SearchMode
    top_k: int
    elapsed_ms: float
    clip: SearchResultBlock | None = None
    hybrid: SearchResultBlock | None = None
