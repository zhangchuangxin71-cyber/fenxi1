from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputText(StrictModel):
    type: Literal["input_text", "output_text"]
    text: str = Field(min_length=1, max_length=1_000_000)


class InputMessage(StrictModel):
    role: Literal["user", "assistant", "system", "developer"]
    content: str | list[InputText]

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "\n".join(part.text for part in self.content)


class ClarificationSelection(StrictModel):
    option_id: Literal["A", "B", "C", "custom"]
    about: str = Field("", max_length=10_000)
    target: str = Field("", max_length=10_000)
    topic: str = Field("", max_length=10_000)

    @model_validator(mode="after")
    def validate_custom_value(self) -> ClarificationSelection:
        self.about = self.about.strip()
        self.target = self.target.strip()
        self.topic = self.topic.strip()
        if self.option_id != "custom":
            if self.about or self.target or self.topic:
                raise ValueError("custom values are only valid when option_id=custom")
            return self
        has_direction = bool(self.about and self.target)
        has_partial_direction = bool(self.about or self.target)
        has_topic = bool(self.topic)
        if has_partial_direction and not has_direction:
            raise ValueError("about and target must be provided together")
        if has_direction == has_topic:
            raise ValueError("custom selection requires either topic or about and target")
        return self


class ConflictSelection(StrictModel):
    option_id: Literal["document_priority", "automatic_authority", "custom_feedback"]


class HitlInput(StrictModel):
    interrupt_id: str = Field(min_length=1, max_length=256)
    decision: Literal["approve", "revise", "regenerate"]
    feedback: str = Field("", max_length=100_000)
    selection: ClarificationSelection | ConflictSelection | None = None

    @model_validator(mode="after")
    def require_revise_feedback(self) -> HitlInput:
        self.feedback = self.feedback.strip()
        if self.selection is not None and self.decision != "revise":
            raise ValueError("selection is only valid when decision=revise")
        if (
            isinstance(self.selection, ConflictSelection)
            and self.selection.option_id == "custom_feedback"
            and not self.feedback
        ):
            raise ValueError("feedback is required for custom conflict resolution")
        if self.decision == "revise" and not self.feedback and self.selection is None:
            raise ValueError("feedback or selection is required when decision=revise")
        return self


class RequestContext(StrictModel):
    session_id: str = Field(min_length=1, max_length=512)
    user_id: str | None = Field(None, min_length=1, max_length=256)
    kb_id: str | None = Field(None, min_length=1, max_length=256)
    doc_ids: list[str] | None = Field(None, max_length=500)
    temp_doc_ids: list[str] | None = Field(None, max_length=500)
    debug: bool = False
    hitl: HitlInput | None = None

    @field_validator("doc_ids", "temp_doc_ids")
    @classmethod
    def normalize_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class ResponseRequest(StrictModel):
    model: Literal["wechat-article-agent"] = "wechat-article-agent"
    input: list[InputMessage] = Field(min_length=1, max_length=500)
    previous_response_id: str | None = Field(None, max_length=256)
    context: RequestContext
    stream: Literal[True] = True

    @model_validator(mode="after")
    def validate_mode(self) -> ResponseRequest:
        if self.context.hitl is not None and self.previous_response_id is None:
            raise ValueError("previous_response_id is required for a HITL decision")
        if self.context.hitl is None and self.previous_response_id is not None:
            raise ValueError("previous_response_id is only valid for a HITL decision")
        return self

    @property
    def latest_user_text(self) -> str:
        messages = [message for message in self.input if message.role == "user"]
        return messages[-1].text() if messages else ""

    @property
    def conversation(self) -> list[dict[str, str]]:
        return [{"role": item.role, "content": item.text()} for item in self.input]


class CancelContext(StrictModel):
    session_id: str = Field(min_length=1, max_length=512)


class CancelRequest(StrictModel):
    context: CancelContext


class ErrorBody(StrictModel):
    code: str
    message: str
    retryable: bool = False
    request_id: str
    stage: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(StrictModel):
    error: ErrorBody
