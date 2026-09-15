from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.rendering.wechat_layout.components.blocks import style_image_figure
from app.rendering.wechat_layout.components.common import (
    normalized_heading_path,
    safe_http_url,
    set_style,
)
from app.rendering.wechat_layout.contracts import ThemeDefinition

CONTENT_TAGS = {"p", "blockquote", "pre", "ul", "ol"}


def _heading_level(tag: Tag) -> int | None:
    if tag.name and len(tag.name) == 2 and tag.name[0] == "h" and tag.name[1].isdigit():
        return int(tag.name[1])
    return None


def _positions(root: Tag) -> dict[tuple[tuple[str, ...], int], Tag]:
    heading_path: list[str] = []
    counts: dict[tuple[str, ...], int] = {}
    result: dict[tuple[tuple[str, ...], int], Tag] = {}
    for tag in root.find_all(recursive=False):
        if not isinstance(tag, Tag):
            continue
        level = _heading_level(tag)
        if level is not None:
            title = tag.get_text(" ", strip=True)
            heading_path = heading_path[: max(0, level - 1)] + ([title] if title else [])
            continue
        if tag.name not in CONTENT_TAGS or not heading_path:
            continue
        path = tuple(heading_path)
        counts[path] = counts.get(path, 0) + 1
        result[(path, counts[path])] = tag
    return result


def insert_images(
    root: Tag,
    images: Sequence[Mapping[str, Any]],
    *,
    theme: ThemeDefinition,
    soup: BeautifulSoup,
) -> list[dict[str, str]]:
    positions = _positions(root)
    inserted: list[dict[str, str]] = []
    section_fallbacks: dict[tuple[str, ...], Tag] = {}
    for (path, _), tag in positions.items():
        section_fallbacks[path] = tag

    for item in images:
        url = safe_http_url(item.get("url"))
        if not url:
            continue
        raw_position = item.get("insertion_position")
        position = raw_position if isinstance(raw_position, Mapping) else {}
        path = normalized_heading_path(position.get("heading_path") or [])
        try:
            ordinal = max(1, int(position.get("paragraph_ordinal") or 1))
        except (TypeError, ValueError):
            ordinal = 1
        anchor = positions.get((path, ordinal)) or section_fallbacks.get(path)
        caption = str(item.get("caption") or "").strip()

        figure = soup.new_tag("figure")
        figure["data-layout-added"] = "image"
        figure["data-image-url"] = url
        set_style(figure, "margin:24px 0 26px;text-align:center")
        image = soup.new_tag("img", src=url, alt=caption)
        set_style(image, "display:block;max-width:100%;height:auto;margin:0 auto;border-radius:4px")
        figure.append(image)
        if caption:
            figcaption = soup.new_tag("figcaption")
            figcaption.string = caption
            set_style(
                figcaption,
                f"margin-top:8px;font-size:{theme.typography.caption_size};line-height:1.6;color:{theme.colors.muted};text-align:center",
            )
            figure.append(figcaption)
        style_image_figure(figure, theme=theme)
        if anchor is None:
            root.append(figure)
            matched = "document_end"
        else:
            anchor.insert_after(figure)
            matched = "exact" if (path, ordinal) in positions else "section_end"
        inserted.append({"url": url, "caption": caption, "matched": matched})
    return inserted
