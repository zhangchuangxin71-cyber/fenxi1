from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas import RetrievalWarning
from app.core.models import CandidateChunk


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageDecision(StrictModel):
    page_number: int = Field(description="输入批次中已有的文档页码。")
    grade: Literal["accept", "possible", "reject"] = Field(
        description="页面对问题组的证据价值：直接命中、可能有帮助或无关。"
    )


class SelectedPageDecision(StrictModel):
    page_number: int = Field(description="输入批次中被选中的已有文档页码。")
    grade: Literal["accept", "possible"] = Field(
        description="被选中页面的证据价值：直接命中或可能有帮助。"
    )


class PageDecisionBatch(StrictModel):
    decisions: list[SelectedPageDecision] = Field(
        description=(
            "从输入批次中选出的 accept/possible 页面；未列出的 page_number 由代码视为 reject。"
        )
    )


class PageInspectionResult(StrictModel):
    decisions: list[PageDecision]
    evaluated_pages: list[int]
    decision_source: Literal["llm", "mixed", "rule_fallback"]
    chunks: list[CandidateChunk] = Field(default_factory=list)
    llm_request_count: int = 0


class FocusedSearchResult(StrictModel):
    group_ref: str
    document_id: str
    chunks: list[CandidateChunk]
    warnings: list[RetrievalWarning] = Field(default_factory=list)
    inspected_node_count: int = 0
    inspected_page_count: int = 0
    llm_request_count: int = 0
    degraded: bool = False
