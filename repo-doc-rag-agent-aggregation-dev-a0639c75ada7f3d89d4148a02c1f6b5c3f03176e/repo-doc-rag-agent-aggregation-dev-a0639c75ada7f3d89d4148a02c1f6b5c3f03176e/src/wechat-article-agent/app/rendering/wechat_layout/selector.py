from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.rendering.wechat_layout.contracts import ThemeSelection
from app.rendering.wechat_layout.themes.registry import ThemeRegistry

DEFAULT_THEME = "professional-clean"

THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "professional-clean": ("企业", "政策", "政务", "正式", "权威", "专业", "通用"),
    "minimal": ("学术", "观点", "极简", "克制", "深度", "研究"),
    "bold-navy": ("商业", "金融", "行业", "报告", "财经", "市场"),
    "warm-editorial": ("文化", "人物", "生活", "温暖", "叙事", "故事"),
    "moyu-green": ("教程", "工具", "效率", "步骤", "操作", "清单"),
    "graphite-minimal": ("技术", "设计", "产品原理", "高信息密度", "评论"),
    "olive-journal": ("案例", "复盘", "观察", "系统说明", "手记"),
    "red-white": ("重点", "警示", "行动", "提醒", "重要", "解读"),
}


class ThemeSelector:
    def __init__(self, registry: ThemeRegistry, default_theme: str = DEFAULT_THEME) -> None:
        self._registry = registry
        self._default_theme = default_theme
        registry.get(default_theme)

    def select(
        self,
        *,
        requested_theme_id: str | None,
        task_spec: Mapping[str, Any] | None,
    ) -> ThemeSelection:
        if requested_theme_id:
            self._registry.get(requested_theme_id)
            return ThemeSelection(
                theme_id=requested_theme_id, reason="explicit", scores={requested_theme_id: 1}
            )

        source = " ".join(
            str((task_spec or {}).get(key) or "") for key in ("topic", "tone", "goal", "other_requirements")
        ).lower()
        scores = {
            theme_id: sum(2 if keyword in source else 0 for keyword in keywords)
            for theme_id, keywords in THEME_KEYWORDS.items()
            if theme_id in self._registry.ids()
        }
        highest = max(scores.values(), default=0)
        if highest <= 0:
            return ThemeSelection(theme_id=self._default_theme, reason="default", scores=scores)
        winners = sorted(theme_id for theme_id, score in scores.items() if score == highest)
        selected = self._default_theme if self._default_theme in winners else winners[0]
        return ThemeSelection(theme_id=selected, reason="task_spec_rule", scores=scores)
