from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

type MaterialKind = Literal[
    "user_document",
    "factual",
    "resource",
    "creative_reference",
    "popular_culture",
]
type ExtractionMode = Literal["raw", "retrieve", "web_search"]
type ChunkType = Literal["full_text", "page", "web_search"]


class MaterialChunk(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(min_length=1)
    content: str = ""
    type: ChunkType
    path: str
    page_number: int | str | None = None
    ref: str
    title: str


class MaterialMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    extraction_mode: ExtractionMode
    queries: list[str] = Field(default_factory=list)
    coverage_complete: bool = False
    warnings: list[dict[str, Any]] = Field(default_factory=list)


class Material(BaseModel):
    model_config = ConfigDict(extra="ignore")

    material_id: str = Field(min_length=1)
    material_kind: MaterialKind
    source: str
    summary: str = ""
    active: bool = True
    metadata: MaterialMetadata
    orig_chunks: list[MaterialChunk] = Field(default_factory=list)
