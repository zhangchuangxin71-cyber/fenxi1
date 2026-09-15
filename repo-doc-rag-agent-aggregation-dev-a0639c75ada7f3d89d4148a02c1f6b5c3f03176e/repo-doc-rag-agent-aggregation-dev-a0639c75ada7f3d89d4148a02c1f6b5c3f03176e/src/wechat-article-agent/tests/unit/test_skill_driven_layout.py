from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agent_engine import AgentBudget, ModelTurn, ToolCall
from app.rendering.skill_driven.adapter import run_layout_agent
from app.rendering.skill_driven.assembler import SkillHtmlAssembler
from app.rendering.skill_driven.component_registry import REGISTRY
from app.rendering.skill_driven.contracts import SectionLayoutPlan, SkillLayoutPlan
from app.rendering.skill_driven.gzh_validator import validate_candidate
from app.rendering.skill_driven.markdown_model import parse_markdown_layout
from app.rendering.skill_driven.xiaowan_policy import validate_plan

INPUT = Path(__file__).parents[2] / "tests" / "rendering_data" / "input"


def _fixture() -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    return (
        (INPUT / "article.md").read_text(encoding="utf-8"),
        json.loads((INPUT / "images.json").read_text(encoding="utf-8")),
        json.loads((INPUT / "task_spec.json").read_text(encoding="utf-8")),
    )


def _plan(theme_id: str, markdown: str) -> SkillLayoutPlan:
    document = parse_markdown_layout(markdown)
    components = REGISTRY.theme(theme_id).components
    return SkillLayoutPlan(
        theme_id=theme_id,
        article_type="analysis",
        hero_component_id=components["hero"].component_id,
        toc_component_id=components["toc"].component_id,
        body_component_id=components["body"].component_id,
        quote_component_id=components["quote"].component_id,
        list_component_id=components["list"].component_id,
        emphasis_component_id=components["emphasis"].component_id,
        image_component_id=components["image"].component_id,
        closing_component_id=components["closing"].component_id,
        sections=[
            SectionLayoutPlan(
                section_id=section.section_id,
                heading_component_id=components["heading"].component_id,
                accent_node_ids=[
                    node.node_id for node in section.nodes if node.kind in {"paragraph", "blockquote", "list"}
                ][:1],
            )
            for section in document.sections
        ],
    )


@pytest.mark.parametrize("theme_id", REGISTRY.ids())
def test_all_gzh_themes_assemble_fancy_valid_html(theme_id: str) -> None:
    markdown, images, _ = _fixture()
    document = parse_markdown_layout(markdown)
    candidate = SkillHtmlAssembler(REGISTRY).assemble(
        document=document,
        images=images,
        plan=_plan(theme_id, markdown),
    )
    report = validate_candidate(
        candidate,
        document=document,
        images=images,
        registry=REGISTRY,
    )

    assert report.valid is True
    assert report.signature is not None
    assert report.signature.distinct_semantics >= 5
    assert report.signature.theme_specific_semantics >= 3
    assert candidate.final_html.count("data-source-node=") > 10
    assert candidate.final_html.count("data-skill-component=") > 8
    assert "professional-clean" not in candidate.final_html
    assert "skill-component-assembler-v1" in candidate.final_html


def test_cross_theme_component_plan_is_rejected_before_assembly() -> None:
    markdown, images, _ = _fixture()
    document = parse_markdown_layout(markdown)
    plan = _plan("moyu-green", markdown)
    plan.hero_component_id = REGISTRY.theme("red-white").components["hero"].component_id

    errors = validate_plan(plan, document=document, images=images, registry=REGISTRY)

    assert any("hero_component_id" in error for error in errors)


def test_markdown_without_h1_promotes_the_first_content_node_once() -> None:
    document = parse_markdown_layout("首段内容。\n\n## 第一节\n\n正文内容。")

    assert document.title.text == "首段内容。"
    assert document.title.node_id == "node_001"
    assert document.preamble == []
    assert [node.node_id for section in document.sections for node in section.nodes] == ["node_003"]
    assert document.source_visible_text == "首段内容。 第一节 正文内容。"


def test_agent_component_variant_changes_the_actual_html_structure() -> None:
    markdown, images, _ = _fixture()
    document = parse_markdown_layout(markdown)
    base_plan = _plan("olive-journal", markdown)
    variant_plan = base_plan.model_copy(deep=True)
    variants = REGISTRY.theme("olive-journal").component_variants
    variant_plan.hero_component_id = variants["hero"][1].component_id
    variant_plan.toc_component_id = variants["toc"][1].component_id
    variant_plan.emphasis_component_id = variants["emphasis"][1].component_id

    errors = validate_plan(
        variant_plan,
        document=document,
        images=images,
        registry=REGISTRY,
    )
    base = SkillHtmlAssembler(REGISTRY).assemble(document=document, images=images, plan=base_plan)
    variant = SkillHtmlAssembler(REGISTRY).assemble(document=document, images=images, plan=variant_plan)

    assert errors == []
    assert variant.final_html != base.final_html
    assert "olive-journal.masthead-hero" in variant.final_html
    assert "olive-journal.dark-summary-split" in variant.final_html
    assert "CURATED LAYOUT" in variant.final_html


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments=encoded,
        provider_item={
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": encoded,
        },
    )


class FakeSkillGateway:
    def __init__(self, plan: SkillLayoutPlan) -> None:
        self.turn = 0
        self.plan = plan
        self.tool_order: list[str] = []

    async def agent_turn(self, **kwargs: Any) -> ModelTurn:
        self.turn += 1
        outputs = [
            json.loads(str(item["output"]))
            for item in kwargs["input_items"]
            if item.get("type") == "function_call_output"
        ]
        calls: list[ToolCall] = []
        if self.turn == 1:
            assets = [
                ("gzh_design", "references/theme-index.md"),
                ("gzh_design", "references/common-components.md"),
                ("gzh_design", f"references/theme-{self.plan.theme_id}.md"),
                ("xiaowan_layout", "references/layout-standard.md"),
                ("xiaowan_layout", "references/release-checklist.md"),
            ]
            calls = [
                _call(
                    f"asset_{index}",
                    "load_rendering_skill_asset",
                    {"skill_id": skill, "level": "L2", "reference": reference},
                )
                for index, (skill, reference) in enumerate(assets, 1)
            ]
        elif self.turn == 2:
            calls = [
                _call("analyze", "analyze_markdown_layout", {}),
                _call("themes", "get_skill_theme_catalog", {}),
            ]
        elif self.turn == 3:
            calls = [
                _call(
                    "components",
                    "get_skill_component_catalog",
                    {"theme_id": self.plan.theme_id},
                )
            ]
        elif self.turn == 4:
            calls = [_call("plan", "validate_skill_layout_plan", self.plan.model_dump())]
        elif self.turn == 5:
            calls = [_call("assemble", "assemble_skill_html", self.plan.model_dump())]
        elif self.turn == 6:
            candidate = next(item for item in reversed(outputs) if "candidate_id" in item)
            calls = [
                _call(
                    "validate",
                    "validate_skill_html",
                    {
                        "candidate_id": candidate["candidate_id"],
                        "candidate_hash": candidate["candidate_hash"],
                    },
                )
            ]
        else:
            candidate = next(item for item in reversed(outputs) if "candidate_id" in item)
            return ModelTurn(
                text=json.dumps(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "candidate_hash": candidate["candidate_hash"],
                        "theme_id": self.plan.theme_id,
                        "user_facing_message": "已完成主题组件装配和公众号兼容性校验。",
                    },
                    ensure_ascii=False,
                )
            )
        self.tool_order.extend(call.name for call in calls)
        return ModelTurn(
            tool_calls=calls,
            response_items=[call.provider_item for call in calls],
        )


@pytest.mark.asyncio
async def test_agent_uses_real_skill_component_loop_without_old_layout_engine() -> None:
    markdown, images, task_spec = _fixture()
    plan = _plan("moyu-ticket", markdown)
    gateway = FakeSkillGateway(plan)
    events: list[tuple[str, dict[str, Any]]] = []

    result, rendered = await run_layout_agent(
        gateway=gateway,
        run_id="skill_layout_test",
        markdown=markdown,
        images=images,
        task_spec=task_spec,
        default_theme="professional-clean",
        budget=AgentBudget(max_steps=8, max_tool_calls=12, max_context_tokens=131_072),
        event_callback=lambda kind, payload: events.append((kind, payload)),
        debug=True,
    )

    assert result.status == "completed"
    assert rendered is not None
    assert rendered.theme_id == "moyu-ticket"
    assert rendered.renderer_version == "skill-component-assembler-v1"
    assert rendered.validation_report.valid is True
    assert "professional-clean" not in rendered.final_html
    assert "assemble_skill_html" in gateway.tool_order
    assert "validate_skill_html" in gateway.tool_order
    assert "render_layout_plan" not in gateway.tool_order
    assert any(kind == "tool_started" for kind, _ in events)


@pytest.mark.asyncio
async def test_forged_candidate_cannot_complete() -> None:
    markdown, images, task_spec = _fixture()

    class ForgedGateway:
        async def agent_turn(self, **_: Any) -> ModelTurn:
            return ModelTurn(
                text=json.dumps(
                    {
                        "candidate_id": "skill_layout_forged",
                        "candidate_hash": "0" * 64,
                        "theme_id": "moyu-green",
                        "user_facing_message": "不应完成。",
                    },
                    ensure_ascii=False,
                )
            )

    result, rendered = await run_layout_agent(
        gateway=ForgedGateway(),
        run_id="skill_layout_forged",
        markdown=markdown,
        images=images,
        task_spec=task_spec,
        default_theme="professional-clean",
        budget=AgentBudget(max_steps=2, max_tool_calls=1, max_context_tokens=131_072),
    )

    assert result.status == "degraded"
    assert rendered is None
