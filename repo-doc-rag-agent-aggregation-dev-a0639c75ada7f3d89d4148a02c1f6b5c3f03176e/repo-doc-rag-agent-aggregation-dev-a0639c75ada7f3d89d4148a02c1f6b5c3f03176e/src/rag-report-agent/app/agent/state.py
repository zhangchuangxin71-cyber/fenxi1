from typing import Any, Literal, TypedDict

from app.rag.types import ProvidedChunkInput

IntentType = Literal[
    "general_qa",
    "knowledge_qa",
    "report_outline",
    "outline_modify",
    "report_write",
    "report_edit",
]


class AgentState(TypedDict, total=False):
    session_id: str
    user_id: str
    conversation_id: str
    kb_id: str
    query: str
    history: list[dict]
    doc_ids: list[str]
    temp_doc_ids: list[str]
    retrieval_top_k: int
    retrieval_search_mode: str
    gen_temperature: float
    gen_max_tokens: int
    report_image_enabled: bool | None
    report_image_api_key: str | None
    report_image_tenant_code: str | None
    current_outline: dict | None
    outline_confirmed: bool
    current_report: str | None

    intent: IntentType
    intent_confidence: float
    intents: list[dict[str, Any]]
    effective_query: str
    identity_intro_requested: bool
    rewritten_queries: list[dict[str, Any]]
    selected_doc_ids: list[str]
    selected_temp_doc_ids: list[str]
    retrieved_chunks: list[ProvidedChunkInput]
    retrieved_query_groups: list[dict[str, Any]]
    used_chunks: list[ProvidedChunkInput]
    rag_fallback: bool

    answer_text: str
    outline_result: dict | None
    report_result: str | None
    report_images: list[dict[str, Any]]
    usage: dict[str, Any]
