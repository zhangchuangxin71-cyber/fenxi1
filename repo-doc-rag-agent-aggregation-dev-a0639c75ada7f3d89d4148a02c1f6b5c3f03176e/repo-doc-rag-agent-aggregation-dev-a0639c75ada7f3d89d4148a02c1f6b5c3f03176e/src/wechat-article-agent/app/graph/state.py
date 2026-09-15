from __future__ import annotations

from typing import Any, Literal, TypedDict


class WechatArticleState(TypedDict, total=False):
    session_id: str
    user_id: str
    kb_id: str
    run_id: str
    response_id: str
    revision: int
    intent: str
    has_prior_article_revision: bool
    intent_clarification_used: bool
    intent_topic_options: list[dict[str, str]]
    entry_stage: Literal["docs_research", "task_spec", "outline", "article"]
    route_reason: str
    previous_revision_status: str
    current_user_input: str
    session_memory: dict[str, str]
    doc_ids: list[str]
    temp_doc_ids: list[str]
    relevant_doc_ids: list[str]
    document_coverage_mode: Literal["best_effort", "all_required"]
    artifact_id: str
    current_stage: str
    generation_attempts: dict[str, int]
    clarification_round: int
    status: str
    review_action: str
    review_feedback: str
    pending_interrupt_id: str
    research_direction_options: list[dict[str, str]]
    research_direction: dict[str, str]
    confirmed_requirements: str
    document_meta: list[dict[str, Any]]
    retrieval_warnings: list[dict[str, Any]]
    need_web_search: bool
    web_info_overview: str
    material_conflicts: list[dict[str, Any]]
    conflict_resolution: str
    conflict_feedback: str
    document_material_status: str
    web_material_status: str


class RuntimeContext(TypedDict, total=False):
    conversation: list[dict[str, str]]
    compact_history: list[dict[str, str]]
    debug_enabled: bool
    response_id: str
