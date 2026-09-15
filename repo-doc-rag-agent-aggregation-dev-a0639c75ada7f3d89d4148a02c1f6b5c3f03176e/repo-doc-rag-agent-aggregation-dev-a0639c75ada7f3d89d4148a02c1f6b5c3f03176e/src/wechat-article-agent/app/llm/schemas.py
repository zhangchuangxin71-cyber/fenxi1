from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntentTopicOption(StrictOutput):
    id: Literal["A", "B", "C"] = Field(description="选项标识；三个选项必须分别使用 A、B、C。")
    topic: str = Field(
        min_length=1,
        description="建议用户用于生成微信公众号文章的明确主题；必须原样保留用户输入中的核心实体词。",
    )


class IntentOutput(StrictOutput):
    is_wechat_article_intent: bool = Field(
        description="当前请求是否仍属于微信公众号文章生成、继续生成或修改文章的意图。"
    )
    user_facing_message: str = Field(
        description="需要澄清时展示在选项卡前的专业、简洁说明；意图明确时返回空字符串。"
    )
    options: list[IntentTopicOption] = Field(
        max_length=3, description="需要澄清时提供的三个公众号文章主题建议；意图明确时返回空数组。"
    )

    @model_validator(mode="after")
    def require_options_only_for_clarification(self) -> IntentOutput:
        if self.is_wechat_article_intent:
            if self.user_facing_message or self.options:
                raise ValueError("confirmed article intent must not include clarification content")
            return self
        if not self.user_facing_message:
            raise ValueError("ambiguous intent requires a user-facing message")
        if len(self.options) != 3 or {item.id for item in self.options} != {"A", "B", "C"}:
            raise ValueError("ambiguous intent must include A, B and C topic options")
        return self


class SessionMemoryOutput(StrictOutput):
    docs_research: str = Field("", description="仍然有效的文档研读范围、关注重点和素材取舍偏好。")
    task_spec: str = Field("", description="仍然有效的文章主题、受众、目标、语气和篇幅偏好。")
    outline: str = Field("", description="仍然有效的标题层级、章节组织和内容结构偏好。")
    article: str = Field("", description="仍然有效的正文措辞、段落节奏、详略和表达风格偏好。")
    ai_image: Literal[""] = Field("", description="图片生成偏好预留字段；第一版必须返回空字符串。")
    html_layout: Literal[""] = Field("", description="HTML 排版偏好预留字段；第一版必须返回空字符串。")


class OrchestratorOutput(StrictOutput):
    target: Literal["docs_research", "task_spec", "outline", "article"] = Field(
        description="本轮最早需要重新执行的阶段；选择后工作流会按固定顺序继续向下运行。"
    )
    reason: str = Field(min_length=1, description="面向用户的简短路由说明，解释将从哪个阶段开始及原因。")


class ResearchDirectionOption(StrictOutput):
    id: Literal["A", "B", "C"] = Field(description="选项标识；三个选项必须分别使用 A、B、C。")
    about: str = Field(min_length=1, description="该选项纳入研读的文档语义范围，即主要研究什么。")
    target: str = Field(min_length=1, description="该选项重点查找和提取的内容，即研究时重点关注什么。")


class ResearchDirectionPlanOutput(StrictOutput):
    coverage_mode: Literal["best_effort", "all_required"] = Field(
        description="best_effort 表示使用相关文档即可；all_required 表示任务明确要求覆盖每一篇指定文档。"
    )
    user_facing_message: str = Field(
        min_length=1, description="在选项卡出现前展示给用户的专业、简洁说明，不包含内部推理。"
    )
    options: list[ResearchDirectionOption] = Field(
        min_length=3, max_length=3, description="供用户单选的三个不同研读方向，必须恰好包含 A、B、C。"
    )

    @model_validator(mode="after")
    def require_three_distinct_options(self) -> ResearchDirectionPlanOutput:
        if {item.id for item in self.options} != {"A", "B", "C"}:
            raise ValueError("research direction options must contain A, B and C exactly once")
        return self


class ResearchDirectionValidationOutput(StrictOutput):
    sufficient: bool = Field(description="用户自由输入的 about 和 target 是否已明确到可以直接开始文档研读。")
    user_facing_message: str = Field(
        min_length=1, description="面向用户说明本次校验结果或下一步选择的专业、简洁文本。"
    )
    normalized_about: str = Field(
        min_length=1, description="对用户输入的研读语义范围进行忠实、简洁的规范化表述。"
    )
    normalized_target: str = Field(
        min_length=1, description="对用户输入的重点提取目标进行忠实、简洁的规范化表述。"
    )
    options: list[ResearchDirectionOption] = Field(
        max_length=3, description="输入不明确时提供的三个替代方向；输入已明确时必须为空数组。"
    )

    @model_validator(mode="after")
    def require_options_when_ambiguous(self) -> ResearchDirectionValidationOutput:
        ids = {item.id for item in self.options}
        if not self.sufficient and (len(self.options) != 3 or ids != {"A", "B", "C"}):
            raise ValueError("ambiguous custom direction must return A, B and C options")
        if self.sufficient and self.options:
            raise ValueError("sufficient custom direction must not return options")
        return self


class QueryGroup(StrictOutput):
    queries: list[str] = Field(
        min_length=1, description="围绕同一信息方面、可分别交给检索服务执行的一组具体检索问题。"
    )


class QueryPlanOutput(StrictOutput):
    query_groups: list[QueryGroup] = Field(
        min_length=1, description="按信息方面分组的检索问题；组数不得超过输入中的 group_count。"
    )


class MaterialSufficiencyOutput(StrictOutput):
    user_facing_message: str = Field(
        min_length=1,
        description="面向用户简洁说明为什么需要或不需要补充联网素材，不包含内部推理。",
    )
    need_web_search: bool = Field(description="当前用户文档是否明显不足、值得补充一次联网素材搜索。")


class WebMaterialCategory(StrictOutput):
    summary: str = Field(description="该类别搜索结果中与当前素材搜集方向直接相关的内容总结；无结果时为空。")
    urls: list[str] = Field(description="总结实际引用且出现在本次搜索 annotations 中的原文 URL。")


class WebMaterialOutput(StrictOutput):
    factual: WebMaterialCategory = Field(description="事实数据、官方资料、时间、制度和统计等可核对素材。")
    resource: WebMaterialCategory = Field(description="资源类扩展槽；V1 不做专门图片搜索，通常返回空。")
    creative_reference: WebMaterialCategory = Field(
        description="他人文章中可借鉴的结构、叙事角度和表达方式，不作为事实证据。"
    )
    popular_culture: WebMaterialCategory = Field(
        description="近期热梗、流行表达、使用语境和受众语气参考，不作为事实证据。"
    )


class ConflictGroupOutput(StrictOutput):
    conflict_chunk_ids: list[str] = Field(
        min_length=2, description="同一事实冲突组中涉及的两个或更多输入 chunk_id。"
    )
    user_facing_message: str = Field(
        min_length=1, description="引用可读出处、向用户说明具体矛盾点的专业简短文本。"
    )


class ConflictCheckOutput(StrictOutput):
    conflicts: list[ConflictGroupOutput] = Field(
        description="对文章事实结论有影响的冲突组；没有可确认冲突时返回空数组。"
    )


def dynamic_boolean_output(name: str, ids: list[str]) -> type[BaseModel]:
    """Build a strict schema whose allowed keys exactly match server-owned IDs."""

    fields: dict[str, Any] = {
        value: (
            bool,
            Field(description=f"是否保留素材 {value}；true 表示保留，false 表示删除。"),
        )
        for value in ids
    }
    return cast(
        type[BaseModel],
        create_model(
            name,
            __config__=ConfigDict(extra="forbid"),
            **fields,
        ),
    )


def dynamic_string_output(name: str, ids: list[str], *, description: str) -> type[BaseModel]:
    """Build a strict string map for one-to-one chunk transformations."""

    fields: dict[str, Any] = {
        value: (str, Field(description=f"{description}；对应 chunk_id={value}。")) for value in ids
    }
    return cast(
        type[BaseModel],
        create_model(
            name,
            __config__=ConfigDict(extra="forbid"),
            **fields,
        ),
    )


def dynamic_theme_selection_output(name: str, ids: list[str]) -> type[BaseModel]:
    """Build a strict theme selector whose enum is derived from the registry."""

    if not ids:
        raise ValueError("theme selector requires at least one registered theme")
    theme_id_type: Any = Literal.__getitem__(tuple(ids))
    return create_model(
        name,
        __config__=ConfigDict(extra="forbid"),
        theme_id=(
            theme_id_type,
            Field(description="代码排版器主题标识；必须原样选择可用主题目录中的一个 theme_id。"),
        ),
        user_facing_message=(
            str,
            Field(
                min_length=1,
                description="面向用户说明本次选择排版主题的专业、简洁文本，不包含内部推理。",
            ),
        ),
    )


class TaskSpecArtifact(StrictOutput):
    topic: str = Field(description="文章最终要讨论的明确主题。")
    audience: str = Field(description="文章面向的核心读者群体。")
    goal: str = Field(description="文章希望读者理解、认同或采取的行动。")
    tone: str = Field(description="适合该主题和受众的语言风格与表达语气。")
    length: str = Field(description="可执行的篇幅要求，例如字数范围或长短定位。")
    other_requirements: list[str] = Field(description="标题、结构、禁忌、覆盖范围等无法归入前述字段的要求。")


class TaskSpecOutput(StrictOutput):
    user_facing_message: str = Field(
        min_length=1, description="在任务书审批卡前展示给用户的专业、简洁说明，不复述完整任务书。"
    )
    artifact: TaskSpecArtifact = Field(description="本轮生成的完整文章任务书，供用户审批和后续节点使用。")


class MarkdownOutput(StrictOutput):
    user_facing_message: str = Field(
        min_length=1, description="在当前 Markdown 产物审批卡前展示给用户的专业、简洁说明。"
    )
    markdown: str = Field(
        min_length=1, description="当前阶段要求的完整 Markdown 产物，不使用 Markdown 代码围栏包裹。"
    )


class ImagePlanItem(StrictOutput):
    position_id: str = Field(description="图片插入位置，必须原样引用输入 positions 中存在的 position_id。")
    caption: str = Field(description="图片在文章中的简短中文图注，应说明图片与相邻内容的关系。")
    prompt: str = Field(
        description=(
            "发送给图片模型的完整中文生成提示词；必须写明相邻正文的事实约束、视觉主体、具体场景、"
            "构图、风格和禁止编造的元素。"
        )
    )


class ImagePlanOutput(StrictOutput):
    user_facing_message: str = Field(
        min_length=1, description="面向用户说明配图计划的专业、简洁文本；无须逐项复述提示词。"
    )
    images: list[ImagePlanItem] = Field(
        description="建议生成的图片列表；数量不得超过输入 max_count，不需要配图时返回空数组。"
    )


class PreflightOutput(StrictOutput):
    action: Literal["replay", "revise_current", "regenerate_current", "approve_current", "supersede"] = Field(
        description="对用户自由文本的处理动作：重放、修改当前产物、重生成当前产物、接受当前产物或取代旧流程。"
    )
    reason: str = Field(description="面向用户的简短处理说明，不包含内部推理。")
    feedback: str = Field(description="仅保留执行当前产物修改所需的用户意见；其他 action 返回空字符串。")
    confidence: float = Field(
        ge=0, le=1, description="对 action 判断的置信度，0 表示完全不确定，1 表示完全确定。"
    )
