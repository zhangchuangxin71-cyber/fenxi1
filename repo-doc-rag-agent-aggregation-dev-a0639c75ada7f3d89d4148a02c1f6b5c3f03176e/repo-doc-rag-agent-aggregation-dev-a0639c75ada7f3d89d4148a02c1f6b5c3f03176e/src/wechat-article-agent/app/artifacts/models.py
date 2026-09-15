from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ArtifactStatus = Literal[
    "running",
    "waiting_for_input",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "superseded",
]
ApprovedStage = Literal["none", "docs_research", "task_spec", "outline", "article"]
CoverageMode = Literal["best_effort", "all_required"]


class ImageArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    caption: str = ""
    insertion_position: dict[str, Any]


class ArticleArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    artifact_id: str
    session_id: str
    run_id: str
    current_response_id: str
    active_runtime_run_id: str | None = None
    revision: int
    parent_artifact_id: str | None = None
    status: ArtifactStatus
    current_stage: str
    approved_through_stage: ApprovedStage = "none"
    last_error: dict[str, Any] | None = None
    doc_ids: list[str] = Field(default_factory=list)
    temp_doc_ids: list[str] = Field(default_factory=list)
    relevant_doc_ids: list[str] = Field(default_factory=list)
    document_coverage_mode: CoverageMode = "best_effort"
    research_direction: dict[str, str] | None = None
    material_library: list[dict[str, Any]] | None = None
    task_spec: dict[str, Any] | None = None
    outline: dict[str, Any] | None = None
    article_markdown: str | None = None
    images: list[ImageArtifact] | None = None
    final_html: str | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class RevisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    session_id: str
    run_id: str
    current_response_id: str
    parent_artifact_id: str | None = None
    entry_stage: Literal["docs_research", "task_spec", "outline", "article"]
    doc_ids: list[str]
    temp_doc_ids: list[str]
    document_coverage_mode: CoverageMode = "best_effort"
