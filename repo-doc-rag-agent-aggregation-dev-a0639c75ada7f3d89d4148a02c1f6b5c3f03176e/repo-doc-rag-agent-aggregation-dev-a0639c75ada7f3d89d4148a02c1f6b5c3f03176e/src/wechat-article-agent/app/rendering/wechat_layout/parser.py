from __future__ import annotations

from dataclasses import dataclass

import bleach
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from app.rendering.wechat_layout.text import normalize_visible_text

ALLOWED_MARKDOWN_TAGS = [
    "h1",
    "h2",
    "h3",
    "h4",
    "p",
    "strong",
    "em",
    "blockquote",
    "ul",
    "ol",
    "li",
    "a",
    "code",
    "pre",
    "hr",
    "br",
]


@dataclass(slots=True)
class ParsedDocument:
    fragment: BeautifulSoup
    source_visible_text: str


class MarkdownDocumentParser:
    """Parses approved Markdown while rejecting embedded raw HTML."""

    def __init__(self) -> None:
        self._markdown = MarkdownIt("commonmark", {"html": False, "linkify": True})

    def parse(self, markdown: str) -> ParsedDocument:
        rendered = self._markdown.render(markdown)
        cleaned = bleach.clean(
            rendered,
            tags=ALLOWED_MARKDOWN_TAGS,
            attributes={"a": ["href", "title"]},
            protocols=["http", "https", "mailto"],
            strip=True,
        )
        fragment = BeautifulSoup(cleaned, "html.parser")
        return ParsedDocument(
            fragment=fragment,
            source_visible_text=normalize_visible_text(fragment.get_text(" ", strip=True)),
        )
