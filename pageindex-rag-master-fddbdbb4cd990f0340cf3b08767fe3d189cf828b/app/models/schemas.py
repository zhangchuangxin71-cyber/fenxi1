# 模块说明：系统使用的 Pydantic 数据模型定义。
from pydantic import BaseModel, Field


class DocumentCatalogItem(BaseModel):
    """文档目录项模型（用于展示与候选召回）。"""
    doc_id: str
    doc_name: str = ""
    doc_description: str = ""
    type: str = ""
    path: str = ""
    index_mode: str = "standard"
    page_count: int | None = None
    line_count: int | None = None


class QuestionItem(BaseModel):
    """批量问答中的题目模型。"""
    id: int
    question: str
    raw_text: str


class EvidenceItem(BaseModel):
    """单文档证据模型（页码范围 + 页面内容）。"""
    doc_id: str
    doc_name: str = ""
    doc_description: str = ""
    selected_page_ranges: list[str] = Field(default_factory=list)
    fetched_pages: str = ""
    page_content: list[dict] = Field(default_factory=list)


class AnswerRow(BaseModel):
    """批量问答输出行模型。"""
    id: int
    question: str
    answer: str
    selected_documents: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)


class ChunkInfo(BaseModel):
    """文档结构分块节点模型。"""
    title: str
    node_id: str
    start_index: int
    end_index: int
    summary: str = ""
    nodes: list[dict] = Field(default_factory=list)


class DocumentInfo(BaseModel):
    """索引后文档元信息模型。"""
    id: str
    type: str
    path: str
    doc_name: str
    doc_description: str = ""
    page_count: int | None = None
    line_count: int | None = None
    index_mode: str = "standard"
    content_hash: str = ""


class Answer(BaseModel):
    """统一问答结果模型。"""
    answer: str
    selected_documents: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)


def build_chunk_node(title: str, node_id: str, start_index: int, end_index: int, summary: str) -> dict:
    """构造标准化分块节点字典。"""
    return ChunkInfo(
        title=title,
        node_id=node_id,
        start_index=int(start_index),
        end_index=int(end_index),
        summary=summary or "",
        nodes=[],
    ).model_dump()


