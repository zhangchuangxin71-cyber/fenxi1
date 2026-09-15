from __future__ import annotations

import hashlib
from typing import Literal

from bs4 import Tag

from app.rendering.skill_driven.contracts import (
    MarkdownLayoutDocument,
    MarkdownNode,
    MarkdownSection,
)
from app.rendering.wechat_layout.parser import MarkdownDocumentParser

_CONTENT_TAGS = {"p", "blockquote", "ul", "ol", "pre", "hr"}


def parse_markdown_layout(markdown: str) -> MarkdownLayoutDocument:
    parsed = MarkdownDocumentParser().parse(markdown)
    title: MarkdownNode | None = None
    preamble: list[MarkdownNode] = []
    sections: list[MarkdownSection] = []
    current_section: MarkdownSection | None = None
    heading_path: list[str] = []
    ordinals: dict[tuple[str, ...], int] = {}
    node_number = 0

    for tag in parsed.fragment.find_all(recursive=False):
        if not isinstance(tag, Tag):
            continue
        level = _heading_level(tag)
        if level is None and tag.name not in _CONTENT_TAGS:
            continue
        node_number += 1
        text = tag.get_text(" ", strip=True)
        if level is not None:
            heading_path = heading_path[: max(0, level - 1)] + ([text] if text else [])
            node = MarkdownNode(
                node_id=f"heading_{node_number:03d}",
                kind="heading",
                level=level,
                text=text,
                html=str(tag),
                heading_path=list(heading_path),
                content_ordinal=None,
            )
            if level == 1 and title is None:
                title = node
            elif level == 2:
                current_section = MarkdownSection(
                    section_id=f"section_{len(sections) + 1:02d}", heading=node, nodes=[]
                )
                sections.append(current_section)
            elif current_section is not None:
                current_section.nodes.append(node)
            else:
                preamble.append(node)
            continue

        path_key = tuple(heading_path)
        ordinals[path_key] = ordinals.get(path_key, 0) + 1
        node = MarkdownNode(
            node_id=f"node_{node_number:03d}",
            kind=_node_kind(tag),
            level=None,
            text=text,
            html=str(tag),
            heading_path=list(heading_path),
            content_ordinal=ordinals[path_key],
        )
        if current_section is None:
            preamble.append(node)
        else:
            current_section.nodes.append(node)

    if title is None:
        fallback = next((node for node in preamble if node.text), None)
        fallback_text = fallback.text if fallback is not None else "未命名文章"
        if fallback is not None:
            title = fallback.model_copy(
                update={
                    "kind": "heading",
                    "level": 1,
                    "html": f"<h1>{fallback_text}</h1>",
                    "heading_path": [fallback_text],
                    "content_ordinal": None,
                }
            )
            preamble = [node for node in preamble if node.node_id != fallback.node_id]
        else:
            title = MarkdownNode(
                node_id="heading_000",
                kind="heading",
                level=1,
                text=fallback_text,
                html=f"<h1>{fallback_text}</h1>",
                heading_path=[fallback_text],
                content_ordinal=None,
            )
    return MarkdownLayoutDocument(
        title=title,
        preamble=preamble,
        sections=sections,
        source_visible_text=parsed.source_visible_text,
        source_hash=hashlib.sha256(markdown.encode()).hexdigest(),
    )


def node_map(document: MarkdownLayoutDocument) -> dict[str, MarkdownNode]:
    nodes = [document.title, *document.preamble]
    for section in document.sections:
        nodes.extend([section.heading, *section.nodes])
    return {node.node_id: node for node in nodes}


def _heading_level(tag: Tag) -> int | None:
    if tag.name and len(tag.name) == 2 and tag.name.startswith("h") and tag.name[1].isdigit():
        return int(tag.name[1])
    return None


NodeKind = Literal["paragraph", "blockquote", "list", "code", "divider"]


def _node_kind(tag: Tag) -> NodeKind:
    if tag.name == "p":
        return "paragraph"
    if tag.name == "blockquote":
        return "blockquote"
    if tag.name in {"ul", "ol"}:
        return "list"
    if tag.name == "pre":
        return "code"
    return "divider"
