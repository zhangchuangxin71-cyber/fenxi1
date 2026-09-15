from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from typing import Any

import bleach
from markdown_it import MarkdownIt

PRIMARY_STYLE = """
<style>
.wechat-article{max-width:677px;margin:0 auto;color:#242424;font-family:-apple-system,BlinkMacSystemFont,
'Segoe UI','Microsoft YaHei',sans-serif;font-size:16px;line-height:1.8;letter-spacing:0;}
.wechat-article h1{font-size:28px;line-height:1.35;margin:1.2em 0 .7em;color:#111;}
.wechat-article h2{font-size:22px;line-height:1.45;margin:1.5em 0 .65em;padding-left:12px;
border-left:4px solid #16856b;}
.wechat-article h3{font-size:18px;line-height:1.5;margin:1.3em 0 .5em;color:#163f37;}
.wechat-article p{margin:.75em 0;text-align:justify;}
.wechat-article blockquote{margin:1em 0;padding:10px 14px;border-left:3px solid #d2a340;
background:#f7f7f5;color:#555;}
.wechat-article img{display:block;max-width:100%;height:auto;margin:18px auto 6px;}
.wechat-article figure{margin:18px 0;text-align:center;}.wechat-article figcaption{font-size:13px;color:#777;}
.wechat-article ul,.wechat-article ol{padding-left:1.5em;}.wechat-article a{color:#0b6e58;}
</style>
"""

SAFE_STYLE = """
<style>.wechat-article{max-width:677px;margin:auto;font-family:sans-serif;line-height:1.75;color:#222}
.wechat-article img{max-width:100%;height:auto}.wechat-article h1{font-size:26px}
.wechat-article h2{font-size:21px}
.wechat-article h3{font-size:18px}</style>
"""


class HtmlRenderer:
    def __init__(self) -> None:
        self._markdown = MarkdownIt("commonmark", {"html": False, "linkify": True})

    def render(self, markdown: str, images: list[dict[str, Any]] | None, *, safe_theme: bool = False) -> str:
        body = self._markdown.render(markdown)
        body = bleach.clean(
            body,
            tags=[
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
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
            ],
            attributes={"a": ["href", "title"]},
            protocols=["http", "https", "mailto"],
            strip=True,
        )
        if images:
            body = self._insert_images(body, images)
        style = SAFE_STYLE if safe_theme else PRIMARY_STYLE
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            f'{style}</head><body><article class="wechat-article">{body}'
            "</article></body></html>"
        )

    @staticmethod
    def _insert_images(body: str, images: list[dict[str, Any]]) -> str:
        positions: list[tuple[int, str]] = []
        parser = _BlockLocator(body)
        parser.feed(body)
        for item in images:
            url = str(item.get("url", ""))
            if not url.startswith(("https://", "http://")):
                continue
            position = item.get("insertion_position") or {}
            heading_path = [str(value) for value in position.get("heading_path") or []]
            paragraph = int(position.get("paragraph_ordinal") or 1)
            offset = parser.position(heading_path, paragraph)
            caption = str(item.get("caption", ""))
            figure = (
                f'<figure><img src="{escape(url, quote=True)}" '
                f'alt="{escape(caption, quote=True)}">'
                f"<figcaption>{escape(caption)}</figcaption></figure>"
            )
            positions.append((offset, figure))
        for offset, figure in sorted(positions, reverse=True):
            body = body[:offset] + figure + body[offset:]
        return body


class _BlockLocator(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self._line_offsets = [0]
        for index, character in enumerate(source):
            if character == "\n":
                self._line_offsets.append(index + 1)
        self.heading_path: list[str] = []
        self._heading_level: int | None = None
        self._heading_text: list[str] = []
        self.paragraphs: list[tuple[tuple[str, ...], int, int]] = []
        self._paragraph_counts: dict[tuple[str, ...], int] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_level = int(tag[1])
            self._heading_text = []

    def handle_data(self, data: str) -> None:
        if self._heading_level is not None:
            self._heading_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._heading_level is not None and tag == f"h{self._heading_level}":
            title = "".join(self._heading_text).strip()
            self.heading_path = self.heading_path[: self._heading_level - 1] + [title]
            self._heading_level = None
        elif tag == "p" and self.heading_path:
            path = tuple(self.heading_path)
            ordinal = self._paragraph_counts.get(path, 0) + 1
            self._paragraph_counts[path] = ordinal
            start = self._absolute_position()
            end = self.source.find(">", start)
            self.paragraphs.append((path, ordinal, len(self.source) if end < 0 else end + 1))

    def position(self, heading_path: list[str], paragraph_ordinal: int) -> int:
        target = tuple(heading_path)
        exact = [
            offset
            for path, ordinal, offset in self.paragraphs
            if path == target and ordinal == paragraph_ordinal
        ]
        if exact:
            return exact[0]
        same_section = [offset for path, _, offset in self.paragraphs if path == target]
        return same_section[-1] if same_section else len(self.source)

    def _absolute_position(self) -> int:
        line, column = self.getpos()
        line_index = max(0, min(line - 1, len(self._line_offsets) - 1))
        return self._line_offsets[line_index] + column
