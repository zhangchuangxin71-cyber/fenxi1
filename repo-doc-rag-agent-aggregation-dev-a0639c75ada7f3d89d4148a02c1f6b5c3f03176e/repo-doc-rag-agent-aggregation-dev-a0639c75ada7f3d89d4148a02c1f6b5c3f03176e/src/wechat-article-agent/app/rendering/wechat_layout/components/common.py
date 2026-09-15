from __future__ import annotations

from collections.abc import Iterable

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag


def set_style(tag: Tag, *parts: str) -> None:
    declarations: dict[str, str] = {}
    current = str(tag.get("style") or "")
    for chunk in (current, *parts):
        for item in chunk.split(";"):
            if ":" not in item:
                continue
            name, value = item.split(":", 1)
            if name.strip() and value.strip():
                declarations[name.strip().lower()] = value.strip()
    tag["style"] = ";".join(f"{name}:{value}" for name, value in declarations.items())


def move_children(source: Tag, target: Tag) -> None:
    for child in list(source.contents):
        target.append(child.extract())


def decoration(tag: Tag) -> Tag:
    tag["data-layout-added"] = "true"
    return tag


def wrap_text_nodes_with_leaf(root: Tag, soup: BeautifulSoup) -> None:
    for value in list(root.find_all(string=True)):
        if not isinstance(value, NavigableString) or not value.strip():
            continue
        parent = value.parent
        if not isinstance(parent, Tag) or parent.name in {"style", "script"}:
            continue
        if parent.name == "span" and parent.has_attr("leaf"):
            continue
        if any(
            isinstance(ancestor, Tag) and ancestor.name == "span" and ancestor.has_attr("leaf")
            for ancestor in parent.parents
        ):
            continue
        leaf = soup.new_tag("span")
        leaf["leaf"] = ""
        value.replace_with(leaf)
        leaf.append(value)


def safe_http_url(value: object) -> str | None:
    url = str(value or "").strip()
    return url if url.startswith(("https://", "http://")) else None


def normalized_heading_path(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip())
