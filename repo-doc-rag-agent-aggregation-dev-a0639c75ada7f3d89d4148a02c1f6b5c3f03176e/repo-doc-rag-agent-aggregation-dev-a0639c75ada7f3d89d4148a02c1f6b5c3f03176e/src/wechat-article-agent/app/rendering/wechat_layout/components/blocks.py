from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from app.rendering.wechat_layout.components.common import decoration, move_children, set_style
from app.rendering.wechat_layout.contracts import ThemeDefinition


def replace_list(source: Tag, *, theme: ThemeDefinition, soup: BeautifulSoup) -> Tag:
    """Replace unreliable native lists with the section-based pattern used by reviewed Skills."""

    ordered = source.name == "ol"
    wrapper = soup.new_tag("section")
    wrapper["data-source-list"] = source.name or "ul"
    set_style(wrapper, "margin:12px 0 24px")
    items = source.find_all("li", recursive=False)
    for number, source_item in enumerate(items, start=1):
        item = soup.new_tag("section")
        set_style(item, "display:flex;align-items:flex-start;gap:10px;margin-bottom:10px")
        marker = decoration(soup.new_tag("span"))
        marker.string = str(number) if ordered else "•"
        _style_list_marker(marker, ordered=ordered, theme=theme)
        content = soup.new_tag("span")
        set_style(
            content,
            f"flex:1;color:{theme.colors.text}",
            f"font-size:{theme.typography.body_size};line-height:{theme.typography.body_line_height}",
        )
        move_children(source_item, content)
        item.extend([marker, content])
        wrapper.append(item)
    source.replace_with(wrapper)
    return wrapper


def style_blockquote(tag: Tag, theme: ThemeDefinition) -> None:
    profile = theme.variants.profile
    if profile == "moyu_green":
        style = (
            f"background:{theme.colors.surface};border:1px dashed {theme.colors.border};"
            "border-radius:8px;padding:14px 16px"
        )
    elif profile == "graphite_minimal":
        style = (
            f"background:{theme.colors.background};border-left:3px solid {theme.colors.primary};"
            "padding:16px 0 16px 22px"
        )
    elif profile == "olive_journal":
        style = (
            f"background:{theme.colors.surface};border:1px solid {theme.colors.border};"
            "border-radius:6px;padding:18px 20px"
        )
    elif profile == "red_white":
        style = (
            f"background:{theme.colors.surface};border-left:4px solid {theme.colors.primary};"
            "border-radius:0 8px 8px 0;padding:16px 20px"
        )
    elif theme.variants.blockquote == "card":
        style = (
            f"background:{theme.colors.surface};border:1px solid {theme.colors.border};"
            "border-radius:5px;padding:16px 18px"
        )
    elif theme.variants.blockquote == "editorial":
        style = (
            f"background:{theme.colors.surface};border-top:1px solid {theme.colors.border};"
            f"border-bottom:1px solid {theme.colors.border};padding:18px 20px"
        )
    else:
        style = (
            f"background:{theme.colors.surface};border-left:3px solid {theme.colors.primary};"
            "padding:14px 17px"
        )
    set_style(tag, f"margin:24px 0;color:{theme.colors.muted};line-height:1.8", style)
    for paragraph in tag.find_all("p", recursive=False):
        set_style(paragraph, "margin:0;text-align:left")


def style_image_figure(figure: Tag, *, theme: ThemeDefinition) -> None:
    profile = theme.variants.profile
    if profile in {"moyu_green", "olive_journal", "red_white"}:
        set_style(
            figure,
            "margin:24px 0 26px;padding:6px;text-align:center",
            f"background:{theme.colors.background};border:1px solid {theme.colors.border}",
            "border-radius:10px;box-shadow:0 4px 12px rgba(0,0,0,0.06)",
        )
    elif profile == "graphite_minimal":
        set_style(
            figure,
            f"margin:28px 0;padding:6px 0 0;border-top:1px solid {theme.colors.border}",
            "text-align:center",
        )
    else:
        set_style(figure, "margin:24px 0 26px;text-align:center")

    image = figure.find("img", recursive=False)
    if isinstance(image, Tag):
        radius = "8px" if profile in {"moyu_green", "olive_journal", "red_white"} else "4px"
        set_style(image, f"border-radius:{radius}")
    caption = figure.find("figcaption", recursive=False)
    if isinstance(caption, Tag) and profile in {"moyu_green", "olive_journal", "red_white"}:
        set_style(caption, "margin-bottom:2px")


def style_divider(tag: Tag, theme: ThemeDefinition) -> None:
    profile = theme.variants.profile
    if profile == "red_white":
        set_style(tag, f"border:0;border-top:2px solid {theme.colors.border};margin:38px 10px")
    elif profile == "graphite_minimal":
        set_style(tag, f"border:0;border-top:1px solid {theme.colors.border};margin:42px 10px")
    elif profile in {"moyu_green", "olive_journal"}:
        set_style(tag, f"border:0;border-top:1px solid {theme.colors.primary};margin:38px 16px")
    else:
        set_style(tag, f"border:0;border-top:1px solid {theme.colors.border};margin:34px 0")


def _style_list_marker(marker: Tag, *, ordered: bool, theme: ThemeDefinition) -> None:
    profile = theme.variants.profile
    if ordered and profile in {"moyu_green", "red_white"}:
        set_style(
            marker,
            "display:inline-flex;align-items:center;justify-content:center",
            "width:22px;height:22px;border-radius:50%;flex-shrink:0;margin-top:2px",
            f"background:{theme.colors.primary};color:#FFFFFF;font-size:12px;font-weight:700",
        )
    elif ordered and profile == "olive_journal":
        set_style(
            marker,
            "display:inline-flex;align-items:center;justify-content:center",
            "min-width:22px;height:22px;border-radius:3px;flex-shrink:0;margin-top:2px",
            f"background:{theme.colors.primary};color:#FFFFFF;font-size:11px;font-weight:700",
        )
    elif ordered:
        set_style(
            marker,
            f"min-width:20px;color:{theme.colors.primary};font-weight:800",
            f"line-height:{theme.typography.body_line_height};flex-shrink:0",
        )
    else:
        size = "18px" if profile in {"moyu_green", "red_white"} else "16px"
        set_style(
            marker,
            f"min-width:12px;color:{theme.colors.primary};font-size:{size};font-weight:800",
            f"line-height:{theme.typography.body_line_height};flex-shrink:0",
        )
