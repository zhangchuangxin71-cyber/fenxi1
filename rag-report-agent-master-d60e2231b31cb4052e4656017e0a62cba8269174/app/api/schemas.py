from typing import Any, Literal

from pydantic import BaseModel, Field


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class RetrievalConfig(BaseModel):
    top_k: int = Field(default=5, ge=1, le=20)
    search_mode: Literal["hybrid", "keyword", "semantic"] = "hybrid"


class GenerationConfig(BaseModel):
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    max_tokens: int = Field(default=4096, ge=1, le=32768)
    enable_images: bool | None = None
    image_api_key: str | None = None
    image_tenant_code: str | None = None


class OutlineSubsection(BaseModel):
    index: str
    title: str


class OutlineSection(BaseModel):
    index: int
    title: str
    subsections: list[OutlineSubsection] = Field(default_factory=list)


class Outline(BaseModel):
    title: str
    sections: list[OutlineSection] = Field(default_factory=list)


class ReportContext(BaseModel):
    current_outline: dict[str, Any] | None = None
    outline_confirmed: bool = False
    current_report: str | None = None


class ChatRequest(BaseModel):
    session_id: str
    user_id: str
    kb_id: str
    query: str
    history: list[HistoryItem] = Field(default_factory=list)
    stream: bool = True
    doc_ids: list[str] = Field(default_factory=list)
    temp_doc_ids: list[str] = Field(default_factory=list)
    retrieval_config: RetrievalConfig = Field(default_factory=RetrievalConfig)
    generation_config: GenerationConfig = Field(default_factory=GenerationConfig)
    report_context: ReportContext = Field(default_factory=ReportContext)


def normalize_outline(data: dict[str, Any] | None) -> Outline | None:
    if not data:
        return None
    raw = data.get("outline") if isinstance(data.get("outline"), dict) else data
    try:
        return Outline.model_validate(raw)
    except Exception:
        return None
