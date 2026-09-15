from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


BoundedId = Annotated[str, Field(min_length=1, max_length=256)]
QueryText = Annotated[StrictStr, Field(min_length=1, max_length=16000)]
SearchMode = Literal["hybrid", "keyword", "semantic"]
SourceType = Literal["page", "scope_metadata", "document_overview", "title_tree"]
RetrievalCategory = Literal["routed_focused", "routed_broad", "routed_direct", "scope_direct"]


class RetrieveOptions(StrictModel):
    include_document_meta: bool = True
    include_debug: bool = False
    ensure_document_coverage: bool = False


class RetrieveRequest(StrictModel):
    user_id: str = Field(min_length=1, max_length=256)
    kb_id: str = Field(min_length=1, max_length=256)
    query: QueryText | list[QueryText] = Field(
        description=(
            "字符串表示尚未拆分的原始检索问题，检索服务会完成必要的拆分与改写；"
            "字符串列表表示调用者已利用对话历史完成指代消解、拆分与改写，"
            "每项必须是可独立检索的问题。"
        )
    )
    session_id: str | None = Field(default=None, max_length=512)
    doc_ids: list[BoundedId] = Field(default_factory=list)
    temp_doc_ids: list[BoundedId] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1, le=100)
    max_return_tokens: int | None = Field(default=None, ge=128)
    search_mode: SearchMode = "hybrid"
    options: RetrieveOptions = Field(default_factory=RetrieveOptions)

    @field_validator("user_id", "kb_id")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str | list[str]) -> str | list[str]:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("must not be blank")
            return text
        if not value:
            raise ValueError("query list must not be empty")
        normalized: list[str] = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("query list items must not be blank")
            if text not in normalized:
                normalized.append(text)
        if sum(len(item) for item in normalized) > 16000:
            raise ValueError("query list exceeds the total character limit")
        return normalized

    @field_validator("doc_ids", "temp_doc_ids")
    @classmethod
    def normalize_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def deduplicate_scope_ids(self) -> RetrieveRequest:
        permanent_ids = set(self.doc_ids)
        self.temp_doc_ids = [doc_id for doc_id in self.temp_doc_ids if doc_id not in permanent_ids]
        return self

    @property
    def query_text(self) -> str:
        return self.query if isinstance(self.query, str) else "；".join(self.query)


class ChunkDocumentMeta(StrictModel):
    doc_type: str | None = None
    doc_description: str = ""
    page_count: int | None = None
    node_count: int | None = None
    is_temporary: bool = False


class ChunkHintData(StrictModel):
    group_refs: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    retrieval_category: RetrievalCategory
    match_grades: dict[str, Literal["accept", "possible"]] = Field(default_factory=dict)
    document_name: str | None = None
    page_numbers: list[int] = Field(default_factory=list)
    chapter_titles: list[str] = Field(default_factory=list)
    content_truncated: bool = False
    original_content_tokens: int | None = None
    returned_content_tokens: int | None = None


class RetrievedChunk(StrictModel):
    chunk_id: str
    document_id: str | None
    document_ids: list[str] = Field(default_factory=list)
    document_name: str | None
    page_number: int | None = None
    path: str
    content: str
    hint: str
    score: float | None
    source_type: SourceType
    document_meta: ChunkDocumentMeta | None
    chunk_meta: dict[str, Any] = Field(default_factory=dict)


class RetrievalWarning(StrictModel):
    code: str
    message: str
    affected_group_refs: list[str] = Field(default_factory=list)
    affected_document_ids: list[str] = Field(default_factory=list)
    retryable: bool = False


class CoverageSummary(StrictModel):
    complete: bool
    truncated: bool = False
    truncated_by: str | None = None
    covered_group_refs: list[str] = Field(default_factory=list)
    uncovered_group_refs: list[str] = Field(default_factory=list)
    degraded_group_refs: list[str] = Field(default_factory=list)


class RetrieveUsage(StrictModel):
    candidate_document_count: int = 0
    inspected_node_count: int = 0
    inspected_page_count: int = 0
    returned_count: int = 0
    returned_tokens: int = 0
    tokenizer: str
    latency_ms: int
    actual_mode: Literal["keyword", "semantic"] | None = None
    llm_request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class SafeLLMCallSummary(StrictModel):
    model: str
    phase: str
    group_index: int | None = None
    group_count: int | None = None
    queue_wait_ms: int = 0
    provider_duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider_request_id: str | None = None


class TraceEvent(StrictModel):
    sequence: int
    node: str
    phase: str
    status: Literal["started", "ok", "degraded", "failed", "skipped"]
    duration_ms: int | None = None
    input_counts: dict[str, int] = Field(default_factory=dict)
    output_counts: dict[str, int] = Field(default_factory=dict)
    group_refs: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    error_category: str | None = None
    llm: SafeLLMCallSummary | None = None


class RetrievalDebug(StrictModel):
    request_id: str
    graph_run_id: str
    requested_mode: SearchMode
    actual_mode: Literal["keyword", "semantic"] | None = None
    state_summary: dict[str, Any] = Field(default_factory=dict)
    node_durations_ms: dict[str, int] = Field(default_factory=dict)
    trace: list[TraceEvent] = Field(default_factory=list)
    input: dict[str, Any] = Field(default_factory=dict)
    classification_trace: list[dict[str, Any]] = Field(default_factory=list)
    groups: list[dict[str, Any]] = Field(default_factory=list)
    routes: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    llm_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_events: list[dict[str, Any]] = Field(default_factory=list)


class RetrieveResponse(StrictModel):
    chunks: list[RetrievedChunk]
    warnings: list[RetrievalWarning]
    coverage: CoverageSummary
    usage: RetrieveUsage
    debug: RetrievalDebug | None = None


class DocumentMetaRequest(StrictModel):
    user_id: str = Field(min_length=1, max_length=256)
    kb_id: str = Field(min_length=1, max_length=256)
    session_id: str | None = Field(default=None, max_length=512)
    doc_ids: list[BoundedId] = Field(default_factory=list)
    temp_doc_ids: list[BoundedId] = Field(default_factory=list)
    include_missing: bool = True

    @field_validator("user_id", "kb_id")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("doc_ids", "temp_doc_ids")
    @classmethod
    def normalize_document_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def validate_document_scope(self) -> DocumentMetaRequest:
        permanent_ids = set(self.doc_ids)
        self.temp_doc_ids = [doc_id for doc_id in self.temp_doc_ids if doc_id not in permanent_ids]
        if not self.doc_ids and not self.temp_doc_ids:
            raise ValueError("at least one document ID is required")
        return self


class DocumentMeta(StrictModel):
    doc_id: str
    doc_name: str
    doc_type: str | None = None
    doc_description: str = ""
    status: str
    page_count: int | None = None
    node_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    kb_id: str
    user_id: str
    is_temporary: bool = False
    bound_at: datetime | None = None


class DocumentMetaResponse(StrictModel):
    documents: list[DocumentMeta]
    missing_doc_ids: list[str] = Field(default_factory=list)


class DocumentRouteCriterion(StrictModel):
    queries: list[QueryText]
    target_docs_description: str = Field(min_length=1, max_length=16000)
    target_docs_keywords: list[BoundedId] = Field(default_factory=list)

    @field_validator("queries", "target_docs_keywords")
    @classmethod
    def normalize_route_text_list(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("target_docs_description")
    @classmethod
    def normalize_route_description(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @model_validator(mode="after")
    def validate_queries(self) -> DocumentRouteCriterion:
        if not self.queries:
            raise ValueError("queries must not be empty")
        return self


class DocumentRouteRequest(StrictModel):
    user_id: str = Field(min_length=1, max_length=256)
    kb_id: str = Field(min_length=1, max_length=256)
    session_id: str | None = Field(default=None, max_length=512)
    doc_ids: list[BoundedId] = Field(default_factory=list)
    temp_doc_ids: list[BoundedId] = Field(default_factory=list)
    criteria: list[DocumentRouteCriterion] = Field(min_length=1)
    keyword_prefilter: bool = False

    @field_validator("user_id", "kb_id")
    @classmethod
    def strip_route_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("doc_ids", "temp_doc_ids")
    @classmethod
    def normalize_route_document_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def validate_route_scope(self) -> DocumentRouteRequest:
        permanent_ids = set(self.doc_ids)
        self.temp_doc_ids = [doc_id for doc_id in self.temp_doc_ids if doc_id not in permanent_ids]
        if not self.doc_ids and not self.temp_doc_ids:
            raise ValueError("at least one document ID is required")
        return self


class DocumentRouteGroupResult(StrictModel):
    index: int = Field(ge=0)
    accept_doc_ids: list[str] = Field(default_factory=list)
    possible_doc_ids: list[str] = Field(default_factory=list)
    reject_doc_ids: list[str] = Field(default_factory=list)
    decision_source: Literal["llm", "mixed", "rule_fallback"]
    degraded: bool = False
    warnings: list[RetrievalWarning] = Field(default_factory=list)


class DocumentRouteUsage(StrictModel):
    candidate_document_count: int = 0
    llm_request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class DocumentRouteResponse(StrictModel):
    request_id: str
    groups: list[DocumentRouteGroupResult]
    usage: DocumentRouteUsage


class DocumentRawRequest(StrictModel):
    user_id: str = Field(min_length=1, max_length=256)
    kb_id: str = Field(min_length=1, max_length=256)
    doc_id: BoundedId

    @field_validator("user_id", "kb_id", "doc_id")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text


class DocumentRawStatusRequest(DocumentRawRequest):
    pass


class DocumentRawPage(StrictModel):
    page: int = Field(ge=1)
    content: str = ""


class DocumentRawNode(StrictModel):
    node_id: str
    title: str | None = None
    text: str | None = None
    summary: str | None = None
    paragraph_index: int | None = None
    start_index: int | None = None
    end_index: int | None = None
    level: int | None = None
    nodes: list[DocumentRawNode] = Field(default_factory=list)


class MinerURawResult(StrictModel):
    md_content: str | None = None
    content_list: list[dict[str, Any]] = Field(default_factory=list)
    middle_json: dict[str, Any] = Field(default_factory=dict)


class DocumentRawResponse(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    doc_id: str
    doc_name: str
    doc_type: str
    doc_description: str = ""
    status: str
    page_count: int | None = None
    line_count: int | None = None
    node_count: int | None = None
    is_temporary: bool = False
    pages: list[DocumentRawPage] = Field(default_factory=list)
    structure: list[DocumentRawNode] = Field(default_factory=list)
    raw_mineru: MinerURawResult


class DocumentRawRepairAccepted(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    doc_id: str
    repair_id: str | None = None
    task_id: str | None = None
    status: Literal["queued", "processing", "completed"]
    status_url: str
    retry_after: int = Field(default=2, ge=1)


class DocumentRawStatusResponse(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    doc_id: str
    status: Literal["not_started", "queued", "processing", "completed", "failed"]
    repair_id: str | None = None
    task_id: str | None = None
    retryable: bool | None = None
    error_code: str | None = None
    message: str | None = None
    updated_at: datetime | None = None
    next_retry_at: datetime | None = None


class HealthResponse(StrictModel):
    ok: bool
    service: str
    version: str
    env: str


class ReadyResponse(StrictModel):
    ok: bool
    service: str
    database: str


class ErrorEnvelope(StrictModel):
    error: dict[str, Any]
