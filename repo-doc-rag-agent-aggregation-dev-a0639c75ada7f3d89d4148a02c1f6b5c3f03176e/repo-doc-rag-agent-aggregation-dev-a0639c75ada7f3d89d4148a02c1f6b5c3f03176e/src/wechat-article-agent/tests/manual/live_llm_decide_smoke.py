"""Smoke-test the real Ark theme decision and deterministic layout path."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path(__file__).parents[2]
sys.path.insert(0, str(PROJECT))

from app.config import get_settings  # noqa: E402
from app.llm.gateway import LLMGateway  # noqa: E402
from app.rendering.wechat_layout import LayoutEngine  # noqa: E402
from app.rendering.wechat_layout.llm_selector import select_theme_with_llm  # noqa: E402
from app.rendering.wechat_layout.themes.registry import default_registry  # noqa: E402

INPUT = PROJECT / "tests" / "rendering_data" / "input"
OUTPUT = PROJECT / "docs" / "smoke-artifacts"


async def main() -> None:
    settings = get_settings()
    gateway = LLMGateway(settings)
    task_spec = json.loads((INPUT / "task_spec.json").read_text(encoding="utf-8"))
    markdown = (INPUT / "article.md").read_text(encoding="utf-8")
    images = json.loads((INPUT / "images.json").read_text(encoding="utf-8"))
    try:
        selected, catalog = await select_theme_with_llm(
            llm=gateway,
            run_id=f"llm_decide_smoke_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
            task_spec=task_spec,
        )
    finally:
        await gateway.close()

    registry = default_registry()
    theme_id = str(selected.theme_id)
    assert theme_id in registry.ids()
    rendered = LayoutEngine(default_theme=settings.html_layout_default_theme).render(
        markdown=markdown,
        images=images,
        task_spec=task_spec,
        requested_theme_id=theme_id,
    )
    assert rendered.validation_report.valid
    assert rendered.theme_id == theme_id
    assert rendered.renderer_version != "skill-component-assembler-v1"

    OUTPUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    html_path = OUTPUT / f"llm-decide-smoke-{stamp}.html"
    report_path = OUTPUT / f"llm-decide-smoke-{stamp}.json"
    html_path.write_text(rendered.final_html, encoding="utf-8")
    report = {
        "status": "completed",
        "theme_id": theme_id,
        "user_facing_message": str(selected.user_facing_message),
        "catalog_theme_count": catalog.count("- theme_id="),
        "renderer_version": rendered.renderer_version,
        "html_chars": len(rendered.final_html),
        "validation_valid": rendered.validation_report.valid,
        "html_path": str(html_path.relative_to(PROJECT)),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {**report, "report_path": str(report_path.relative_to(PROJECT))},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
