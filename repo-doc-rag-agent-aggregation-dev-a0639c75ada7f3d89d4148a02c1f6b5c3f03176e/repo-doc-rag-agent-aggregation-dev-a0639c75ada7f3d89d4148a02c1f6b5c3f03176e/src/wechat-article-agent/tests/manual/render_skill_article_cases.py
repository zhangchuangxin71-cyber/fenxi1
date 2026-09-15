"""Render three article-type fixtures with Skill and deterministic controls."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT = Path(__file__).parents[2]
sys.path.insert(0, str(PROJECT))

from app.rendering.skill_driven.assembler import SkillHtmlAssembler  # noqa: E402
from app.rendering.skill_driven.component_registry import REGISTRY  # noqa: E402
from app.rendering.skill_driven.contracts import SectionLayoutPlan, SkillLayoutPlan  # noqa: E402
from app.rendering.skill_driven.gzh_validator import validate_candidate  # noqa: E402
from app.rendering.skill_driven.markdown_model import parse_markdown_layout  # noqa: E402
from app.rendering.wechat_layout import LayoutEngine  # noqa: E402

INPUT = PROJECT / "tests" / "rendering_data" / "skill_driven" / "input"
OUTPUT = PROJECT / "docs" / "smoke-artifacts"
CASES = {
    "tutorial": ("moyu-green", "tutorial"),
    "policy": ("red-white", "policy"),
    "story": ("zen-whitespace", "story"),
}


def _plan(theme_id: str, article_type: str, markdown: str) -> SkillLayoutPlan:
    document = parse_markdown_layout(markdown)
    components = REGISTRY.theme(theme_id).components
    return SkillLayoutPlan(
        theme_id=theme_id,
        article_type=article_type,  # type: ignore[arg-type]
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
                    node.node_id
                    for node in section.nodes
                    if node.kind in {"paragraph", "blockquote", "list"}
                ][:1]
                if index == 0
                else [],
            )
            for index, section in enumerate(document.sections)
        ],
    )


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    summary: dict[str, object] = {"cases": {}}
    for case_id, (theme_id, article_type) in CASES.items():
        markdown = (INPUT / f"{case_id}.md").read_text(encoding="utf-8")
        document = parse_markdown_layout(markdown)
        candidate = SkillHtmlAssembler(REGISTRY).assemble(
            document=document,
            images=[],
            plan=_plan(theme_id, article_type, markdown),
        )
        report = validate_candidate(candidate, document=document, images=[], registry=REGISTRY)
        if not report.valid:
            raise RuntimeError(f"{case_id}: {[item.code for item in report.errors]}")
        skill_path = OUTPUT / f"skill-rendering-case-{case_id}.html"
        control_path = OUTPUT / f"skill-rendering-case-{case_id}-control.html"
        skill_path.write_text(candidate.final_html, encoding="utf-8")
        control = LayoutEngine(default_theme="professional-clean").render(
            markdown=markdown,
            images=[],
            task_spec={"topic": document.title.text, "audience": "公众号读者"},
            requested_theme_id="professional-clean",
        )
        control_path.write_text(control.final_html, encoding="utf-8")
        paths[case_id] = skill_path
        paths[f"{case_id}-control"] = control_path
        summary["cases"][case_id] = {
            "article_type": article_type,
            "theme_id": theme_id,
            "skill_html": str(skill_path.relative_to(PROJECT)),
            "control_html": str(control_path.relative_to(PROJECT)),
            "signature": report.signature.model_dump() if report.signature else None,
            "metrics": report.metrics,
        }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        for name, html_path in paths.items():
            for viewport_name, viewport in (
                ("desktop", {"width": 900, "height": 1000}),
                ("mobile", {"width": 390, "height": 844}),
            ):
                page = browser.new_page(viewport=viewport)
                page.goto(html_path.resolve().as_uri(), wait_until="domcontentloaded")
                page.wait_for_timeout(150)
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
                )
                if overflow:
                    raise RuntimeError(f"horizontal overflow: {name}/{viewport_name}")
                screenshot = OUTPUT / f"skill-rendering-case-{name}-{viewport_name}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                page.close()
        browser.close()

    report_path = OUTPUT / "skill-rendering-article-cases.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
