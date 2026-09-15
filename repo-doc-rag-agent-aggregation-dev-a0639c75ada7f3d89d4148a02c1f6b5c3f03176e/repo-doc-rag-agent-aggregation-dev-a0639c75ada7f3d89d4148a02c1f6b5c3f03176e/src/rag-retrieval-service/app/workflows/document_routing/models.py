from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.repositories import DocumentProfile


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrefilterCandidate(StrictModel):
    document: DocumentProfile
    keyword_score: float
    components: dict[str, float]


class PrefilterResult(StrictModel):
    group_ref: str
    candidates: list[PrefilterCandidate]
    rejected_count: int


class DocumentRouteDecision(StrictModel):
    group_ref: str = Field(description="输入中已有的问题组引用 ID。")
    doc_id: str = Field(description="输入中已有的候选文档 ID。")
    grade: Literal["accept", "possible"] = Field(
        description="被选中文档与问题组目标的关系：明确匹配或可能匹配。"
    )


class DocumentRouteBatchOutput(StrictModel):
    decisions: list[DocumentRouteDecision] = Field(
        description=(
            "本批次从 required_pairs 中选出的 accept/possible 组合；未列出的组合由代码视为 reject。"
        )
    )


class RoutedDocument(StrictModel):
    document: DocumentProfile
    grade: Literal["accept", "possible"]
    keyword_score: float
    decision_source: Literal["llm", "rule_fallback"]


class DocumentRoute(StrictModel):
    group_ref: str
    accept_docs: list[RoutedDocument]
    possible_docs: list[RoutedDocument]
    rejected_document_count: int
    prefilter_candidate_count: int
    prefilter_rejected_count: int
    degraded: bool = False
