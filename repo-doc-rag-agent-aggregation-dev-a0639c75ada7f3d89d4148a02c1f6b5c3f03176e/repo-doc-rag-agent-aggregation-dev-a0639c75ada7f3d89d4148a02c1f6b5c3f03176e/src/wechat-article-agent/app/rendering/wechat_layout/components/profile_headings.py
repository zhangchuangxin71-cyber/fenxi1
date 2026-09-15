from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from app.rendering.wechat_layout.components.common import decoration, move_children, set_style
from app.rendering.wechat_layout.contracts import ThemeDefinition

RICH_HEADING_PROFILES = {"moyu_green", "graphite_minimal", "olive_journal", "red_white"}


def replace_h2(source: Tag, *, number: int, theme: ThemeDefinition, soup: BeautifulSoup) -> Tag:
    profile = theme.variants.profile
    if profile not in RICH_HEADING_PROFILES:
        return source
    wrapper = soup.new_tag("section")
    wrapper["data-layout-heading"] = "h2"
    wrapper["data-source-heading"] = "true"
    title = soup.new_tag("p")
    move_children(source, title)
    badge = decoration(soup.new_tag("span"))
    badge.string = f"{number:02d}"

    if profile == "moyu_green":
        set_style(wrapper, f"display:flex;align-items:center;gap:14px;margin:{theme.spacing.section_margin}")
        set_style(
            badge,
            f"display:inline-block;color:{theme.colors.primary};font-size:28px;font-weight:900;line-height:1",
        )
        set_style(
            title,
            f"margin:0;font-size:{theme.typography.h2_size};font-weight:900;color:{theme.colors.heading};line-height:1.5",
        )
    elif profile == "graphite_minimal":
        set_style(
            wrapper,
            f"border-top:1px solid {theme.colors.border}",
            "padding-top:18px",
            f"margin:{theme.spacing.section_margin}",
        )
        set_style(badge, f"color:{theme.colors.border};font-size:26px;font-weight:700;margin-right:12px")
        set_style(
            title,
            f"display:inline;margin:0;font-size:{theme.typography.h2_size};font-weight:700;color:{theme.colors.heading};line-height:1.5",
        )
    elif profile == "olive_journal":
        set_style(
            wrapper,
            f"border-left:4px solid {theme.colors.primary}",
            "padding:10px 0 10px 16px",
            f"margin:{theme.spacing.section_margin}",
        )
        set_style(
            badge,
            "display:inline-block",
            f"background:{theme.colors.primary};color:#FFFFFF",
            "padding:2px 8px;margin:0 8px 6px 0",
            "font-size:10px;font-weight:700",
        )
        set_style(
            title,
            f"margin:0;font-size:{theme.typography.h2_size};font-weight:800;color:{theme.colors.heading};line-height:1.5",
        )
    else:
        set_style(
            wrapper,
            f"background:{theme.colors.surface}",
            f"border-left:5px solid {theme.colors.primary}",
            "padding:14px 16px",
            f"margin:{theme.spacing.section_margin}",
        )
        set_style(badge, f"color:{theme.colors.primary};font-size:12px;font-weight:800;margin-right:10px")
        set_style(
            title,
            f"display:inline;margin:0;font-size:{theme.typography.h2_size};font-weight:800;color:{theme.colors.heading};line-height:1.5",
        )
    wrapper.extend([badge, title])
    source.replace_with(wrapper)
    return wrapper


def replace_h3(source: Tag, *, theme: ThemeDefinition, soup: BeautifulSoup) -> Tag:
    if theme.variants.profile not in RICH_HEADING_PROFILES:
        return source
    wrapper = soup.new_tag("section")
    wrapper["data-layout-heading"] = "h3"
    wrapper["data-source-heading"] = "true"
    title = soup.new_tag("p")
    move_children(source, title)
    set_style(wrapper, "margin:28px 0 16px")
    set_style(
        title,
        f"margin:0;font-size:{theme.typography.h3_size};line-height:1.55",
        f"font-weight:700;color:{theme.colors.heading}",
        f"border-bottom:1px solid {theme.colors.border};padding-bottom:7px",
    )
    wrapper.append(title)
    source.replace_with(wrapper)
    return wrapper
