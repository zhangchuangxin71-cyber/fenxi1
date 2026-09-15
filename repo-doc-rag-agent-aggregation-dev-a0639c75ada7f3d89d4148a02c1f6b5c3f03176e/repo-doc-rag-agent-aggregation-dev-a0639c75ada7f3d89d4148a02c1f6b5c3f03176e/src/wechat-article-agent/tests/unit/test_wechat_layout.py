from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from bs4 import BeautifulSoup

from app.rendering.wechat_layout import LayoutEngine
from app.rendering.wechat_layout.contracts import LayoutEvent, ThemeDefinition
from app.rendering.wechat_layout.themes.registry import default_registry

THEME_IDS = {
    "bold-navy",
    "graphite-minimal",
    "minimal",
    "moyu-green",
    "olive-journal",
    "professional-clean",
    "red-white",
    "warm-editorial",
}

MARKDOWN = """# 公众号排版烟测

这是一段用于验证**正文保真**的导语。

## 一、核心制度

第一段说明政策背景，包含数字 123 和链接 [示例](https://example.com)。

第二段用于验证图片插入位置。

> 重要事实必须保持原文，不应由排版器改写。

### 1. 办理步骤

1. 准备材料
2. 提交申请

`inline_code` 与代码块也需要安全展示。

```text
status = approved
```
"""

IMAGES = [
    {
        "url": "https://example.com/policy.png",
        "caption": "核心制度示意图",
        "insertion_position": {
            "heading_path": ["公众号排版烟测", "一、核心制度"],
            "paragraph_ordinal": 1,
        },
    }
]


def test_registry_contains_the_eight_reviewed_themes() -> None:
    assert set(default_registry().ids()) == THEME_IDS


@pytest.mark.parametrize("theme_id", sorted(THEME_IDS))
def test_every_theme_renders_valid_deterministic_html(theme_id: str) -> None:
    engine = LayoutEngine()

    first = engine.render(
        markdown=MARKDOWN,
        images=IMAGES,
        task_spec={"topic": "政策解读"},
        requested_theme_id=theme_id,
    )
    second = engine.render(
        markdown=MARKDOWN,
        images=IMAGES,
        task_spec={"topic": "政策解读"},
        requested_theme_id=theme_id,
    )

    assert first.final_html == second.final_html
    assert first.theme_id == theme_id
    assert first.validation_report.valid is True
    assert first.fallback_used is False
    assert "<style" not in first.final_html
    assert "<script" not in first.final_html
    assert "data-layout-theme" in first.final_html
    assert "style=" in first.final_html
    assert 'leaf=""' in first.final_html
    assert "<h1" in first.final_html
    if theme_id in {"bold-navy", "minimal", "professional-clean", "warm-editorial"}:
        assert "<h2" in first.final_html
        assert "<h3" in first.final_html
    else:
        assert 'data-layout-heading="h2"' in first.final_html
        assert 'data-layout-heading="h3"' in first.final_html


def test_image_is_inserted_after_the_requested_paragraph() -> None:
    result = LayoutEngine().render(
        markdown=MARKDOWN,
        images=IMAGES,
        task_spec={},
        requested_theme_id="professional-clean",
    )

    first_paragraph = result.final_html.index("第一段说明政策背景")
    image = result.final_html.index("https://example.com/policy.png")
    second_paragraph = result.final_html.index("第二段用于验证图片插入位置")
    assert first_paragraph < image < second_paragraph


def test_untrusted_markdown_and_image_urls_are_sanitized() -> None:
    result = LayoutEngine().render(
        markdown="# 安全检查\n\n<script>alert(1)</script>正文。",
        images=[
            {
                "url": "javascript:alert(1)",
                "caption": "不安全图片",
                "insertion_position": {},
            }
        ],
        task_spec={},
    )

    assert result.validation_report.valid is True
    assert "<script" not in result.final_html
    assert "javascript:" not in result.final_html
    assert "<img" not in result.final_html


def test_theme_selector_is_deterministic_and_supports_explicit_theme() -> None:
    engine = LayoutEngine()
    explicit = engine.render(
        markdown="# 标题\n\n正文。",
        images=[],
        task_spec={"topic": "企业政策解读"},
        requested_theme_id="warm-editorial",
    )
    selected = engine.render(
        markdown="# 标题\n\n正文。",
        images=[],
        task_spec={"topic": "金融行业市场报告"},
    )

    assert explicit.theme_id == "warm-editorial"
    assert explicit.selection.reason == "explicit"
    assert selected.theme_id == "bold-navy"
    assert selected.selection.reason == "task_spec_rule"


def test_engine_uses_legacy_renderer_after_both_deterministic_attempts_fail() -> None:
    events: list[LayoutEvent] = []
    result = LayoutEngine(renderer=_AlwaysFailRenderer()).render(
        markdown="# 标题\n\n正文。",
        images=[],
        task_spec={"topic": "金融行业市场报告"},
        event_callback=events.append,
    )

    assert result.theme_id == "legacy-safe"
    assert result.renderer_version == "legacy-safe-v1"
    assert result.fallback_used is True
    assert result.validation_report.valid is True
    assert [event.status for event in events if event.stage == "fallback"] == [
        "degraded",
        "running",
        "completed",
    ]


def test_wechat_specific_heading_component_preserves_heading_text() -> None:
    result = LayoutEngine().render(
        markdown="# 标题\n\n## 关键章节\n\n正文。",
        images=[],
        task_spec={},
        requested_theme_id="moyu-green",
    )
    soup = BeautifulSoup(result.final_html, "html.parser")
    heading = soup.find("section", attrs={"data-layout-heading": "h2"})

    assert heading is not None
    assert "01" in heading.get_text(" ", strip=True)
    assert "关键章节" in heading.get_text(" ", strip=True)


class _AlwaysFailRenderer:
    version = "always-fail"

    def render(
        self,
        markdown: str,
        images: Sequence[Mapping[str, Any]],
        *,
        theme: ThemeDefinition,
    ) -> tuple[str, str, list[dict[str, str]]]:
        del markdown, images, theme
        raise RuntimeError("synthetic renderer failure")
