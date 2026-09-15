from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bs4 import BeautifulSoup

from app.rendering.html import HtmlRenderer
from app.rendering.wechat_layout.parser import MarkdownDocumentParser


class LegacyHtmlRendererAdapter:
    version = "legacy-safe-v1"

    def __init__(self) -> None:
        self._renderer = HtmlRenderer()
        self._parser = MarkdownDocumentParser()

    def render(
        self,
        markdown: str,
        images: Sequence[Mapping[str, Any]],
    ) -> tuple[str, str, list[dict[str, str]]]:
        html = self._renderer.render(markdown, [dict(item) for item in images], safe_theme=True)
        source_text = self._parser.parse(markdown).source_visible_text
        soup = BeautifulSoup(html, "html.parser")
        inserted = [
            {
                "url": str(tag.get("src") or ""),
                "caption": str(tag.get("alt") or ""),
                "matched": "legacy",
            }
            for tag in soup.find_all("img")
        ]
        return html, source_text, inserted
