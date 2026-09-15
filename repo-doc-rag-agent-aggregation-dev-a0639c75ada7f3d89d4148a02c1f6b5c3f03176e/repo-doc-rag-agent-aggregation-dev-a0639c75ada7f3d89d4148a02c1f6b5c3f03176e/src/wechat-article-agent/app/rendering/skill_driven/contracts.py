from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.rendering.wechat_layout.contracts import ValidationIssue


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarkdownNode(StrictModel):
    node_id: str = Field(description="本轮已批准 Markdown 中稳定的节点 ID。")
    kind: Literal["heading", "paragraph", "blockquote", "list", "code", "divider"]
    level: int | None = Field(None, description="标题层级；非标题节点为空。")
    text: str = Field(description="节点的只读可见文本。")
    html: str = Field(description="经过 Markdown parser 清洗的节点 HTML，仅供装配器内部使用。")
    heading_path: list[str] = Field(description="节点所在的完整标题路径。")
    content_ordinal: int | None = Field(None, description="同一标题路径下的内容节点序号。")


class MarkdownSection(StrictModel):
    section_id: str
    heading: MarkdownNode
    nodes: list[MarkdownNode]


class MarkdownLayoutDocument(StrictModel):
    title: MarkdownNode
    preamble: list[MarkdownNode]
    sections: list[MarkdownSection]
    source_visible_text: str
    source_hash: str


class SectionLayoutPlan(StrictModel):
    section_id: str = Field(description="必须按输入顺序覆盖的章节 ID。")
    heading_component_id: str = Field(description="当前主题的章节标题组件 ID。")
    accent_node_ids: list[str] = Field(
        default_factory=list,
        description="需要以主题强调卡展示的正文节点 ID；不得引用标题或不存在的节点。",
    )


class SkillLayoutPlan(StrictModel):
    theme_id: str
    article_type: Literal["tutorial", "list", "analysis", "policy", "story", "review", "general"]
    hero_component_id: str
    toc_component_id: str
    body_component_id: str
    quote_component_id: str
    list_component_id: str
    emphasis_component_id: str
    image_component_id: str
    closing_component_id: str
    sections: list[SectionLayoutPlan]


class ComponentSignature(StrictModel):
    theme_id: str
    component_ids: list[str]
    semantic_counts: dict[str, int]
    distinct_semantics: int
    theme_specific_semantics: int


class SkillValidationReport(StrictModel):
    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    metrics: dict[str, int | float | str | bool] = Field(default_factory=dict)
    signature: ComponentSignature | None = None


class SkillRenderCandidate(StrictModel):
    final_html: str
    theme_id: str
    renderer_version: str = "skill-component-assembler-v1"
    source_visible_text: str
    inserted_images: list[dict[str, str]] = Field(default_factory=list)
    plan: SkillLayoutPlan
    source_metadata: dict[str, str] = Field(default_factory=dict)


class SkillAssetOutput(StrictModel):
    skill_id: str
    level: str
    path: str
    sha256: str
    content: str


class ThemeCatalogOutput(StrictModel):
    themes: list[dict[str, Any]]


class ComponentCatalogOutput(StrictModel):
    theme_id: str
    components: list[dict[str, Any]]
    required_semantics: list[str]
    source_sha256: str


class MarkdownAnalysisOutput(StrictModel):
    source_hash: str
    title: dict[str, Any]
    preamble_node_ids: list[str]
    sections: list[dict[str, Any]]
    image_positions: list[dict[str, Any]]


class PlanValidationOutput(StrictModel):
    valid: bool
    errors: list[str]
    normalized_plan: dict[str, Any]


class RenderCandidateOutput(StrictModel):
    candidate_id: str
    candidate_hash: str
    theme_id: str
    renderer_version: str
    component_signature: dict[str, Any]


class CandidateValidationOutput(StrictModel):
    valid: bool
    candidate_id: str
    candidate_hash: str
    errors: list[str]
    warnings: list[str]
    metrics: dict[str, Any]
    component_signature: dict[str, Any]
