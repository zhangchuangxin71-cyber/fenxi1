from __future__ import annotations

from app.rendering.wechat_layout.themes.registry import ThemeRegistry, default_registry


def describe_supported_themes(registry: ThemeRegistry | None = None) -> str:
    """Return a compact, model-facing description of the allowlisted themes.

    This is deliberately generated from the registry instead of duplicated in a
    prompt, so a newly bundled theme is automatically available to the selector.
    """

    active_registry = registry or default_registry()
    lines = [
        "当前代码排版器支持以下主题。theme_id 必须原样使用下面的标识：",
    ]
    for theme in active_registry.all():
        palette = "、".join(
            value for value in (theme.colors.primary, theme.colors.accent, theme.colors.background) if value
        )
        profile = theme.variants.profile
        details = [f"中文名称：{theme.label}"]
        if theme.suitable_for:
            details.append(f"适用场景：{'、'.join(theme.suitable_for)}")
        if profile:
            details.append(f"视觉方向：{profile}")
        if palette:
            details.append(f"色彩关键词：{palette}")
        lines.append(f"- theme_id={theme.id}; " + "；".join(details))
    return "\n".join(lines)
