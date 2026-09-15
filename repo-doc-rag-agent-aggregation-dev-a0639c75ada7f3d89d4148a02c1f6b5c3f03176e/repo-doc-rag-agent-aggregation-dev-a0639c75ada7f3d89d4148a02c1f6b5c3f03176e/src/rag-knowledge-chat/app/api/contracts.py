from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.platform.settings import CHAT_MODEL_ALIAS, MAX_MESSAGE_CHARS


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=32_000)


class StreamOptions(StrictModel):
    include_usage: bool = False


BoundedId = Annotated[str, Field(min_length=1, max_length=256)]
MIN_RAG_RETURN_TOKENS = 128


class RagOptions(StrictModel):
    user_id: str = Field(min_length=1, max_length=256)
    kb_id: str = Field(min_length=1, max_length=256)
    session_id: BoundedId | None = None
    doc_ids: list[BoundedId] = Field(default_factory=list, max_length=100)
    temp_doc_ids: list[BoundedId] = Field(default_factory=list, max_length=100)
    incremental_doc_ids: list[BoundedId] = Field(default_factory=list, max_length=100)
    top_k: int | None = Field(default=None, ge=4, le=20)
    max_return_tokens: int | None = Field(
        default=None,
        ge=MIN_RAG_RETURN_TOKENS,
    )
    include_debug: bool = False

    @model_validator(mode="after")
    def validate_temporary_document_scope(self) -> RagOptions:
        self.doc_ids = list(dict.fromkeys(self.doc_ids))
        permanent_ids = set(self.doc_ids)
        self.temp_doc_ids = [
            doc_id for doc_id in dict.fromkeys(self.temp_doc_ids) if doc_id not in permanent_ids
        ]
        if self.temp_doc_ids and not self.session_id:
            raise ValueError("rag.session_id is required when rag.temp_doc_ids is not empty")
        self.incremental_doc_ids = list(dict.fromkeys(self.incremental_doc_ids))
        scope_ids = permanent_ids | set(self.temp_doc_ids)
        unknown = [doc_id for doc_id in self.incremental_doc_ids if doc_id not in scope_ids]
        if unknown:
            raise ValueError("rag.incremental_doc_ids must be a subset of rag.doc_ids and rag.temp_doc_ids")
        return self


class ChatCompletionRequest(StrictModel):
    model: str = Field(default=CHAT_MODEL_ALIAS, min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1, max_length=80)
    stream: bool = True
    stream_options: StreamOptions = Field(default_factory=StreamOptions)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=1200, ge=1, le=8192)
    rag: RagOptions

    @model_validator(mode="after")
    def validate_supported_contract(self) -> ChatCompletionRequest:
        if not self.stream:
            raise ValueError("stream=true is required in v1")
        if self.stream_options.include_usage:
            raise ValueError("stream_options.include_usage is not supported in v1")
        if self.messages[-1].role != "user":
            raise ValueError("last message must have role=user")
        total_chars = sum(len(message.content) for message in self.messages)
        if total_chars > MAX_MESSAGE_CHARS:
            raise ValueError("messages exceed the input context preflight limit")
        return self

    @property
    def last_user_message(self) -> str:
        return self.messages[-1].content

    def llm_messages(self) -> list[dict[str, str]]:
        return [message.model_dump() for message in self.messages]


class ChatCompletionStreamDelta(StrictModel):
    content: str | None = Field(
        default=None,
        description="最终回答的文本增量；调用方按事件顺序拼接。",
    )
    reasoning_content: str | None = Field(
        default=None,
        description="仅最终回答模型产生的思考内容增量。",
    )


class ChatCompletionStreamChoice(StrictModel):
    index: int = 0
    delta: ChatCompletionStreamDelta
    finish_reason: Literal["stop"] | None = None


class ChatCompletionStatusEvent(StrictModel):
    type: Literal["status"] = "status"
    stage: Literal["route", "retrieval", "generation", "thinking"]
    message: str


class ChatCompletionReference(StrictModel):
    citation_index: int = Field(description="与回答正文中的 [N] 标号对应。")
    doc_id: str | None = None
    doc_name: str | None = None
    page_number: int | None = None


class ChatCompletionEvidenceChunk(StrictModel):
    citation_index: int
    chunk_id: str
    document_id: str
    document_name: str
    page_number: int | None = None
    path: str = ""
    content: str
    hint: str = ""
    score: float = 0
    source_type: str = ""
    document_meta: dict[str, Any] | None = None
    chunk_meta: dict[str, Any] = Field(default_factory=dict)


class ChatCompletionRetrievalMeta(StrictModel):
    status: Literal["success", "no_result", "error", "not_called"]
    request_id: str | None = None
    error: dict[str, Any] | None = None


class ChatCompletionStreamError(StrictModel):
    code: str
    message: str
    retryable: bool
    request_id: str


class ChatCompletionRagPayload(StrictModel):
    event: ChatCompletionStatusEvent | None = None
    answer_basis: (
        Literal[
            "knowledge_base",
            "general_no_retrieval",
            "general_no_result",
            "general_retrieval_error",
        ]
        | None
    ) = None
    retrieval: ChatCompletionRetrievalMeta | None = None
    references: list[ChatCompletionReference] | None = None
    chunks: list[ChatCompletionEvidenceChunk] | None = None
    debug: dict[str, Any] | None = None
    error: ChatCompletionStreamError | None = None


class ChatCompletionStreamChunk(StrictModel):
    """JSON payload carried by one SSE ``data:`` frame; ``[DONE]`` terminates the stream."""

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionStreamChoice]
    rag: ChatCompletionRagPayload | None = None
