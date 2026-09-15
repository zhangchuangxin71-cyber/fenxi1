from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, create_model

from app.agent_engine.context import stable_json
from app.agent_engine.contracts import RunContext
from app.agent_engine.errors import AgentEngineError
from app.agent_engine.skill_loader import SkillLoader
from app.agent_engine.tools import ToolRegistry, ToolSpec
from app.rendering.skill_driven.assembler import SkillHtmlAssembler
from app.rendering.skill_driven.asset_catalog import RenderingAssetCatalog
from app.rendering.skill_driven.contracts import (
    CandidateValidationOutput,
    ComponentCatalogOutput,
    MarkdownAnalysisOutput,
    PlanValidationOutput,
    RenderCandidateOutput,
    SkillAssetOutput,
    SkillLayoutPlan,
    ThemeCatalogOutput,
)
from app.rendering.skill_driven.gzh_validator import validate_candidate
from app.rendering.skill_driven.xiaowan_policy import validate_plan


class StrictToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def build_skill_tools(
    *,
    loader: SkillLoader,
    catalog: RenderingAssetCatalog,
    document: Any,
) -> tuple[ToolRegistry, type[BaseModel]]:
    registry = ToolRegistry()
    plan_model = _plan_model(catalog, document)

    async def load_asset(raw: BaseModel, context: RunContext) -> SkillAssetOutput:
        values = raw.model_dump()
        skill_id = str(values["skill_id"])
        result = loader.read(
            skill_id,
            node_name=context.node_name,
            level=str(values["level"]),
            reference=str(values.get("reference") or "") or None,
        )
        context.metadata.setdefault("loaded_skill_assets", set()).add(
            f"{skill_id}:{result.get('path') or result.get('level') or 'L0'}"
        )
        return SkillAssetOutput(
            skill_id=skill_id,
            level=str(result.get("level") or values["level"]),
            path=str(result.get("path") or ""),
            sha256=str(result.get("sha256") or ""),
            content=str(result.get("content") or result),
        )

    async def analyze(_: BaseModel, context: RunContext) -> MarkdownAnalysisOutput:
        document = context.metadata["document"]
        return MarkdownAnalysisOutput(
            source_hash=document.source_hash,
            title=document.title.model_dump(),
            preamble_node_ids=[node.node_id for node in document.preamble],
            sections=[
                {
                    "section_id": section.section_id,
                    "heading": section.heading.model_dump(),
                    "node_ids": [node.node_id for node in section.nodes],
                    "node_kinds": [node.kind for node in section.nodes],
                }
                for section in document.sections
            ],
            image_positions=list(context.metadata.get("image_summaries") or []),
        )

    async def get_themes(_: BaseModel, __: RunContext) -> ThemeCatalogOutput:
        return ThemeCatalogOutput(themes=catalog.themes())

    async def get_components(raw: BaseModel, _: RunContext) -> ComponentCatalogOutput:
        theme_id = str(raw.model_dump()["theme_id"])
        theme = catalog.theme(theme_id)
        return ComponentCatalogOutput(
            theme_id=theme_id,
            components=catalog.components(theme_id),
            required_semantics=["hero", "toc", "heading", "body", "emphasis", "image", "closing"],
            source_sha256=theme.source_sha256,
        )

    async def validate_layout_plan(raw: BaseModel, context: RunContext) -> PlanValidationOutput:
        plan = SkillLayoutPlan.model_validate(raw.model_dump(exclude={"user_facing_message"}))
        errors = validate_plan(
            plan,
            document=context.metadata["document"],
            images=context.metadata.get("images") or [],
            registry=catalog.registry,
        )
        if not errors:
            context.metadata.setdefault("validated_skill_plans", set()).add(stable_json(plan.model_dump()))
        return PlanValidationOutput(
            valid=not errors,
            errors=errors,
            normalized_plan=plan.model_dump(),
        )

    async def assemble(raw: BaseModel, context: RunContext) -> RenderCandidateOutput:
        plan = SkillLayoutPlan.model_validate(raw.model_dump(exclude={"user_facing_message"}))
        plan_json = stable_json(plan.model_dump())
        if plan_json not in set(context.metadata.get("validated_skill_plans") or ()):
            raise AgentEngineError(
                "SKILL_LAYOUT_PLAN_NOT_VALIDATED",
                "Call validate_skill_layout_plan successfully before assembly.",
            )
        loaded = set(context.metadata.get("loaded_skill_assets") or ())
        required = {
            "gzh_design:references/theme-index.md",
            "gzh_design:references/common-components.md",
            f"gzh_design:references/theme-{plan.theme_id}.md",
            "xiaowan_layout:references/layout-standard.md",
            "xiaowan_layout:references/release-checklist.md",
        }
        missing = sorted(required - loaded)
        if missing:
            raise AgentEngineError(
                "SKILL_LAYOUT_ASSETS_NOT_LOADED",
                "Read every required fixed asset before assembly.",
                {"missing": missing},
            )
        candidate = SkillHtmlAssembler(catalog.registry).assemble(
            document=context.metadata["document"],
            images=context.metadata.get("images") or [],
            plan=plan,
        )
        stored = context.objects.put(candidate, prefix="skill_layout_candidate")
        context.metadata.setdefault("skill_candidates", {})[stored.object_id] = {
            "hash": stored.content_hash,
            "theme_id": candidate.theme_id,
        }
        return RenderCandidateOutput(
            candidate_id=stored.object_id,
            candidate_hash=stored.content_hash,
            theme_id=candidate.theme_id,
            renderer_version=candidate.renderer_version,
            component_signature=_component_signature(candidate.final_html, candidate.theme_id),
        )

    async def validate_html(raw: BaseModel, context: RunContext) -> CandidateValidationOutput:
        values = raw.model_dump()
        candidate_id = str(values["candidate_id"])
        candidate_hash = str(values["candidate_hash"])
        try:
            stored = context.objects.get(candidate_id, candidate_hash)
        except (KeyError, ValueError) as exc:
            return CandidateValidationOutput(
                valid=False,
                candidate_id=candidate_id,
                candidate_hash=candidate_hash,
                errors=[str(exc)],
                warnings=[],
                metrics={},
                component_signature={},
            )
        candidate = stored.value
        report = validate_candidate(
            candidate,
            document=context.metadata["document"],
            images=context.metadata.get("images") or [],
            registry=catalog.registry,
        )
        if report.valid:
            context.metadata.setdefault("validated_skill_candidates", set()).add(candidate_id)
            context.metadata.setdefault("skill_candidate_reports", {})[candidate_id] = report
        return CandidateValidationOutput(
            valid=report.valid,
            candidate_id=candidate_id,
            candidate_hash=candidate_hash,
            errors=[f"{item.code}: {item.message}" for item in report.errors],
            warnings=[f"{item.code}: {item.message}" for item in report.warnings],
            metrics=dict(report.metrics),
            component_signature=report.signature.model_dump() if report.signature else {},
        )

    skill_input = create_model(
        "LoadRenderingSkillAssetInput",
        __config__=ConfigDict(extra="forbid"),
        skill_id=(Literal["gzh_design", "xiaowan_layout"], Field(description="固定排版 Skill ID。")),
        level=(Literal["L0", "L1", "L2"], Field(description="渐进披露层级。")),
        reference=(str, Field("", description="L2 时填写 allowlist 中的相对路径。")),
        user_facing_message=(str, Field("", description="面向用户的简短专业进度说明。")),
    )
    theme_input = create_model(
        "GetSkillComponentCatalogInput",
        __config__=ConfigDict(extra="forbid"),
        theme_id=(Literal.__getitem__(catalog.registry.ids()), Field(description="需要读取组件目录的主题。")),
        user_facing_message=(str, Field("", description="面向用户的简短专业进度说明。")),
    )
    empty_input = create_model(
        "RenderingEmptyInput",
        __config__=ConfigDict(extra="forbid"),
        user_facing_message=(str, Field("", description="面向用户的简短专业进度说明。")),
    )
    candidate_input = create_model(
        "SkillCandidateInput",
        __config__=ConfigDict(extra="forbid"),
        candidate_id=(str, Field(description="assemble_skill_html 返回的内存候选 ID。")),
        candidate_hash=(str, Field(description="assemble_skill_html 返回的候选 hash。")),
        user_facing_message=(str, Field("", description="面向用户的简短专业进度说明。")),
    )

    _register(
        registry,
        ToolSpec(
            name="load_rendering_skill_asset",
            description="读取固定 GZH/Xiaowan 排版 Skill 资产。",
            input_model=skill_input,
            output_model=SkillAssetOutput,
            allowed_nodes=frozenset({"render_html"}),
            side_effect="read_only",
            max_calls=5,
            semantic_argument_fields=("skill_id", "level", "reference"),
            handler=load_asset,
        ),
    )
    _register(
        registry,
        ToolSpec(
            name="analyze_markdown_layout",
            description="分析已批准 Markdown 的章节、节点和图片位置。",
            input_model=empty_input,
            output_model=MarkdownAnalysisOutput,
            allowed_nodes=frozenset({"render_html"}),
            side_effect="read_only",
            max_calls=2,
            semantic_argument_fields=(),
            handler=analyze,
        ),
    )
    _register(
        registry,
        ToolSpec(
            name="get_skill_theme_catalog",
            description="返回 GZH 六套主题与对应组件来源。",
            input_model=empty_input,
            output_model=ThemeCatalogOutput,
            allowed_nodes=frozenset({"render_html"}),
            side_effect="read_only",
            max_calls=2,
            semantic_argument_fields=(),
            handler=get_themes,
        ),
    )
    _register(
        registry,
        ToolSpec(
            name="get_skill_component_catalog",
            description="返回所选主题允许的固定组件和语义。",
            input_model=theme_input,
            output_model=ComponentCatalogOutput,
            allowed_nodes=frozenset({"render_html"}),
            side_effect="read_only",
            max_calls=2,
            semantic_argument_fields=("theme_id",),
            handler=get_components,
        ),
    )
    _register(
        registry,
        ToolSpec(
            name="validate_skill_layout_plan",
            description="校验富布局计划是否覆盖正文并符合 Xiaowan 装饰预算。",
            input_model=plan_model,
            output_model=PlanValidationOutput,
            allowed_nodes=frozenset({"render_html"}),
            side_effect="read_only",
            max_calls=3,
            semantic_argument_fields=("theme_id", "sections"),
            handler=validate_layout_plan,
        ),
    )
    _register(
        registry,
        ToolSpec(
            name="assemble_skill_html",
            description="只用固定组件资产在内存中组装公众号 HTML。",
            input_model=plan_model,
            output_model=RenderCandidateOutput,
            allowed_nodes=frozenset({"render_html"}),
            side_effect="memory_only",
            max_calls=3,
            semantic_argument_fields=("theme_id", "sections"),
            handler=assemble,
        ),
    )
    _register(
        registry,
        ToolSpec(
            name="validate_skill_html",
            description="执行 GZH 安全、正文冻结、图片证据和组件效果校验。",
            input_model=candidate_input,
            output_model=CandidateValidationOutput,
            allowed_nodes=frozenset({"render_html"}),
            side_effect="read_only",
            max_calls=3,
            semantic_argument_fields=("candidate_id", "candidate_hash"),
            handler=validate_html,
        ),
    )
    return registry, plan_model


def _component_signature(final_html: str, theme_id: str) -> dict[str, Any]:
    soup = BeautifulSoup(final_html, "html.parser")
    semantic_counts = Counter(
        str(tag.get("data-skill-semantic"))
        for tag in soup.select("[data-skill-semantic]")
        if tag.get("data-skill-semantic")
    )
    component_ids = list(
        dict.fromkeys(
            str(tag.get("data-skill-component"))
            for tag in soup.select("[data-skill-component]")
            if tag.get("data-skill-component")
        )
    )
    theme_specific = {
        str(tag.get("data-skill-semantic"))
        for tag in soup.select(f'[data-skill-component^="{theme_id}."]')
        if tag.get("data-skill-semantic")
    }
    return {
        "theme_id": theme_id,
        "component_ids": component_ids,
        "semantic_counts": dict(semantic_counts),
        "distinct_semantics": len(semantic_counts),
        "theme_specific_semantics": len(theme_specific),
    }


def _register(registry: ToolRegistry, spec: ToolSpec) -> None:
    registry.register(spec)


def _plan_model(catalog: RenderingAssetCatalog, document: Any) -> type[BaseModel]:
    theme_ids = catalog.registry.ids()
    component_ids = catalog.registry.component_ids() + ("common.code-dark",)
    section_ids = tuple(section.section_id for section in document.sections) or ("section_00",)
    node_ids = tuple(
        node.node_id
        for section in document.sections
        for node in section.nodes
        if node.kind in {"paragraph", "blockquote", "list"}
    ) or ("node_000",)
    theme_type: Any = Literal.__getitem__(theme_ids)
    component_type: Any = Literal.__getitem__(component_ids)
    section_type: Any = Literal.__getitem__(section_ids)
    node_type: Any = Literal.__getitem__(node_ids)
    section_model: Any = create_model(
        "SkillSectionPlan",
        __config__=ConfigDict(extra="forbid"),
        section_id=(section_type, Field(description="必须覆盖输入中的章节 ID。")),
        heading_component_id=(component_type, Field(description="当前主题章节标题组件。")),
        accent_node_ids=(list[node_type], Field(default_factory=list, description="已有正文节点强调列表。")),
    )
    return create_model(
        "SkillLayoutPlanInput",
        __config__=ConfigDict(extra="forbid"),
        theme_id=(theme_type, Field(description="GZH 主题 ID，必须来自主题目录。")),
        article_type=(
            Literal["tutorial", "list", "analysis", "policy", "story", "review", "general"],
            Field(description="文章结构类型。"),
        ),
        hero_component_id=(component_type, Field(description="首屏组件。")),
        toc_component_id=(component_type, Field(description="导读组件。")),
        body_component_id=(component_type, Field(description="正文组件。")),
        quote_component_id=(component_type, Field(description="引用组件。")),
        list_component_id=(component_type, Field(description="列表组件。")),
        emphasis_component_id=(component_type, Field(description="重点强调组件。")),
        image_component_id=(component_type, Field(description="图片证据容器组件。")),
        closing_component_id=(component_type, Field(description="结尾组件。")),
        sections=(list[section_model], Field(description="按原顺序覆盖全部章节。")),
        user_facing_message=(str, Field("", description="面向用户的专业简短进度说明。")),
    )
