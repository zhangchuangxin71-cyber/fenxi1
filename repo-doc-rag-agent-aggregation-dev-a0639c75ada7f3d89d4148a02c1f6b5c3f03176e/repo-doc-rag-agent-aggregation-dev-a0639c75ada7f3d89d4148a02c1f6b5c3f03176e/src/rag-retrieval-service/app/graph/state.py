from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from app.api.schemas import RetrievalWarning, RetrieveRequest
from app.core.models import CandidateChunk
from app.db.repositories import DocumentProfile
from app.workflows.classification.models import QueryGroup
from app.workflows.document_routing.models import DocumentRoute, PrefilterResult
from app.workflows.merge.service import MergeResult


class RetrievalGraphState(TypedDict, total=False):
    request: RetrieveRequest
    request_id: str
    soft_deadline_at: float
    trace_collector: Any
    scope_documents: list[DocumentProfile]
    groups: list[QueryGroup]
    classification_trace: list[dict[str, Any]]
    prefilter_results: dict[str, PrefilterResult]
    keyword_prefilter_overflow: bool
    routes: list[DocumentRoute]
    candidate_chunks: Annotated[list[CandidateChunk], operator.add]
    warnings: Annotated[list[RetrievalWarning], operator.add]
    degraded_group_refs: Annotated[list[str], operator.add]
    remaining_return_tokens: int
    llm_request_count_at_checkpoint: int
    llm_request_count_after_extensions: int
    merge_result: MergeResult
    stats: dict[str, Any]
