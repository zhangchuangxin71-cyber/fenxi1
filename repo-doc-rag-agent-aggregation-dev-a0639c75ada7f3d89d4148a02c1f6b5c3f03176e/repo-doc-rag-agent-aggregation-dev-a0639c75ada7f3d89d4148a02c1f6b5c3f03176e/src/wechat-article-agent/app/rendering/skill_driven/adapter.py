from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.agent_engine import AgentBudget, AgentRequest, AgentResult, AgentRunner, CancellationToken
from app.agent_engine.contracts import EngineEventCallback, RunContext
from app.agent_engine.errors import AgentEngineError
from app.agent_engine.model import GatewayAgentModel
from app.agent_engine.registry import NodeAgentProfile
from app.agent_engine.skill_loader import SkillLoader
from app.rendering.skill_driven.asset_catalog import RenderingAssetCatalog
from app.rendering.skill_driven.component_registry import REGISTRY
from app.rendering.skill_driven.contracts import SkillRenderCandidate
from app.rendering.skill_driven.markdown_model import parse_markdown_layout
from app.rendering.skill_driven.prompts import LAYOUT_AGENT_SYSTEM_PROMPT
from app.rendering.skill_driven.tools import build_skill_tools
from app.rendering.wechat_layout.contracts import (
    RenderResult,
    ThemeSelection,
    ValidationReport,
)

_SKILL_ROOT = Path(__file__).parents[2] / "skills" / "rendering"
_SKILL_LOADER = SkillLoader(_SKILL_ROOT)


def build_layout_profile(*, budget: AgentBudget) -> NodeAgentProfile:
    return NodeAgentProfile(
        node_name="render_html",
        completion_model=_completion_model(),
        skill_allowlist=("gzh_design", "xiaowan_layout"),
        tool_allowlist=(
            "load_rendering_skill_asset",
            "analyze_markdown_layout",
            "get_skill_theme_catalog",
            "get_skill_component_catalog",
            "validate_skill_layout_plan",
            "assemble_skill_html",
            "validate_skill_html",
        ),
        budget=budget,
        completion_validator=_validate_completion,
    )


async def run_layout_agent(
    *,
    gateway: Any,
    run_id: str,
    markdown: str,
    images: Sequence[Mapping[str, Any]],
    task_spec: Mapping[str, Any],
    default_theme: str,
    budget: AgentBudget,
    event_callback: EngineEventCallback | None = None,
    cancellation: CancellationToken | None = None,
    skill_loader: SkillLoader | None = None,
    debug: bool = False,
    **unused: Any,
) -> tuple[AgentResult, RenderResult | None]:
    del default_theme, unused
    document = parse_markdown_layout(markdown)
    loader = skill_loader or _SKILL_LOADER
    catalog = RenderingAssetCatalog(loader, REGISTRY)
    profile = build_layout_profile(budget=budget)
    tools, _plan_schema = build_skill_tools(loader=loader, catalog=catalog, document=document)
    del _plan_schema
    clean_task_spec = {
        key: task_spec.get(key)
        for key in ("topic", "audience", "goal", "tone", "length", "other_requirements", "generation_basis")
        if task_spec.get(key) is not None
    }
    image_summaries = [
        {
            "caption": str(item.get("caption") or ""),
            "position_id": str((item.get("insertion_position") or {}).get("position_id") or ""),
            "heading_path": list((item.get("insertion_position") or {}).get("heading_path") or []),
            "paragraph_ordinal": (item.get("insertion_position") or {}).get("paragraph_ordinal"),
        }
        for item in images
    ]
    request = AgentRequest(
        node_name=profile.node_name,
        run_id=run_id,
        task="使用迁入项目的 GZH 组件资产和 Xiaowan policy，为当前已批准文章完成富组件排版。",
        system_prompt=LAYOUT_AGENT_SYSTEM_PROMPT,
        input_context={
            "task_spec": clean_task_spec,
            "markdown_source_hash": document.source_hash,
            "section_count": len(document.sections),
            "image_count": len(images),
        },
        skill_allowlist=profile.skill_allowlist,
        tool_allowlist=profile.tool_allowlist,
        completion_model=profile.completion_model,
        budget=profile.budget,
        cancellation=cancellation or CancellationToken(),
        event_callback=event_callback,
        debug=debug,
        private_context={
            "document": document,
            "images": [dict(item) for item in images],
            "image_summaries": image_summaries,
        },
        completion_validator=profile.completion_validator,
        completion_resolver=_resolve_completion,
    )
    result = await AgentRunner(
        model=GatewayAgentModel(gateway),
        tools=tools,
        skill_loader=loader,
    ).run(request)
    rendered = None
    if result.status == "completed" and result.output is not None:
        value = result.output.get("render_result")
        if isinstance(value, RenderResult):
            rendered = value
    return result, rendered


def _completion_model() -> type[BaseModel]:
    theme_type: Any = Literal.__getitem__(REGISTRY.ids())
    return create_model(
        "SkillLayoutAgentCompletion",
        __config__=ConfigDict(extra="forbid"),
        candidate_id=(str, Field(description="已通过 validate_skill_html 的候选 ID。")),
        candidate_hash=(str, Field(description="已通过校验的候选 hash。")),
        theme_id=(theme_type, Field(description="候选实际使用的 GZH 主题 ID。")),
        user_facing_message=(
            str,
            Field(min_length=1, description="面向用户说明主题和校验完成的专业短文本。"),
        ),
    )


def _validate_completion(output: BaseModel, context: RunContext) -> None:
    candidate_id = str(getattr(output, "candidate_id", ""))
    candidate_hash = str(getattr(output, "candidate_hash", ""))
    if candidate_id not in set(context.metadata.get("validated_skill_candidates") or ()):
        raise AgentEngineError(
            "SKILL_LAYOUT_CANDIDATE_NOT_VALIDATED",
            "Call validate_skill_html successfully before completion.",
        )
    try:
        stored = context.objects.get(candidate_id, candidate_hash)
    except (KeyError, ValueError) as exc:
        raise AgentEngineError(
            "SKILL_LAYOUT_CANDIDATE_REFERENCE_INVALID",
            "The completed candidate reference is invalid.",
        ) from exc
    if not isinstance(stored.value, SkillRenderCandidate):
        raise AgentEngineError("SKILL_LAYOUT_CANDIDATE_TYPE_INVALID", "Unexpected candidate type.")
    if stored.value.theme_id != str(getattr(output, "theme_id", "")):
        raise AgentEngineError("SKILL_LAYOUT_THEME_MISMATCH", "Completion theme differs from candidate.")
    if candidate_id not in set(context.metadata.get("skill_candidate_reports") or {}):
        raise AgentEngineError("SKILL_LAYOUT_REPORT_MISSING", "Validated candidate report is missing.")


def _resolve_completion(output: BaseModel, context: RunContext) -> dict[str, Any]:
    completion = cast(Any, output)
    stored = context.objects.get(str(completion.candidate_id), str(completion.candidate_hash))
    candidate = stored.value
    reports = context.metadata.get("skill_candidate_reports") or {}
    report = reports.get(str(completion.candidate_id))
    if not isinstance(candidate, SkillRenderCandidate) or report is None:
        raise AgentEngineError("SKILL_LAYOUT_REPORT_MISSING", "Validated report is missing.")
    rendered = RenderResult(
        final_html=candidate.final_html,
        theme_id=candidate.theme_id,
        renderer_version=candidate.renderer_version,
        validation_report=ValidationReport(
            valid=report.valid,
            errors=report.errors,
            warnings=report.warnings,
            metrics=report.metrics,
        ),
        fallback_used=False,
        selection=ThemeSelection(
            theme_id=candidate.theme_id,
            reason="由受限 ReAct Agent 根据任务书选择 GZH 主题并完成组件级规划。",
        ),
        source_metadata=candidate.source_metadata,
    )
    return {
        "render_result": rendered,
        "candidate_id": stored.object_id,
        "candidate_hash": stored.content_hash,
        "theme_id": candidate.theme_id,
        "user_facing_message": str(completion.user_facing_message),
    }
