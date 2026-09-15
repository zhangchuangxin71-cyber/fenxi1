"""Generate six GZH Skill themes, deterministic control, and browser screenshots."""

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

INPUT = PROJECT / "tests" / "rendering_data" / "input"
OUTPUT = PROJECT / "docs" / "smoke-artifacts"


def plan(theme_id: str, markdown: str) -> SkillLayoutPlan:
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
                    node.node_id
                    for node in section.nodes
                    if node.kind in {"paragraph", "blockquote", "list"}
                ][:1],
            )
            for section in document.sections
        ],
    )


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    markdown = (INPUT / "article.md").read_text(encoding="utf-8")
    images = json.loads((INPUT / "images.json").read_text(encoding="utf-8"))
    task_spec = json.loads((INPUT / "task_spec.json").read_text(encoding="utf-8"))
    document = parse_markdown_layout(markdown)
    summary: dict[str, object] = {"themes": {}, "fixture_source_hash": document.source_hash}

    control = LayoutEngine(default_theme="professional-clean").render(
        markdown=markdown,
        images=images,
        task_spec=task_spec,
        requested_theme_id="professional-clean",
    )
    control_path = OUTPUT / "skill-rendering-control-professional-clean.html"
    control_path.write_text(control.final_html, encoding="utf-8")

    html_paths: dict[str, Path] = {"control-professional-clean": control_path}
    for theme_id in REGISTRY.ids():
        candidate = SkillHtmlAssembler(REGISTRY).assemble(
            document=document,
            images=images,
            plan=plan(theme_id, markdown),
        )
        report = validate_candidate(
            candidate,
            document=document,
            images=images,
            registry=REGISTRY,
        )
        if not report.valid:
            raise RuntimeError(f"{theme_id}: {[item.code for item in report.errors]}")
        html_path = OUTPUT / f"skill-rendering-{theme_id}.html"
        html_path.write_text(candidate.final_html, encoding="utf-8")
        html_paths[theme_id] = html_path
        summary["themes"][theme_id] = {
            "html": str(html_path.relative_to(PROJECT)),
            "html_chars": len(candidate.final_html),
            "signature": report.signature.model_dump() if report.signature else None,
            "metrics": report.metrics,
        }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        for theme_id, html_path in html_paths.items():
            for viewport_name, viewport in (
                ("desktop", {"width": 900, "height": 1000}),
                ("mobile", {"width": 390, "height": 844}),
            ):
                page = browser.new_page(viewport=viewport)
                page.goto(html_path.resolve().as_uri(), wait_until="domcontentloaded")
                page.wait_for_timeout(250)
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
                )
                if overflow:
                    raise RuntimeError(f"horizontal overflow: {theme_id}/{viewport_name}")
                screenshot = OUTPUT / f"skill-rendering-{theme_id}-{viewport_name}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                if theme_id in summary["themes"]:
                    summary["themes"][theme_id][f"{viewport_name}_screenshot"] = str(
                        screenshot.relative_to(PROJECT)
                    )
                page.close()
        browser.close()

    report_path = OUTPUT / "skill-rendering-gallery.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
