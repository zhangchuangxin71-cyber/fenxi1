from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.rendering.wechat_layout.components.blocks import (
    replace_list,
    style_blockquote,
    style_divider,
)
from app.rendering.wechat_layout.components.common import set_style, wrap_text_nodes_with_leaf
from app.rendering.wechat_layout.components.profile_headings import replace_h2, replace_h3
from app.rendering.wechat_layout.contracts import ThemeDefinition
from app.rendering.wechat_layout.images import insert_images
from app.rendering.wechat_layout.parser import MarkdownDocumentParser
from app.rendering.wechat_layout.sanitizer import sanitize_fragment

FONT_STACK = "-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif"


class DeterministicHtmlRenderer:
    """Renders approved content without model calls or external side effects."""

    version = "deterministic-v1"

    def __init__(self, parser: MarkdownDocumentParser | None = None) -> None:
        self._parser = parser or MarkdownDocumentParser()

    def render(
        self,
        markdown: str,
        images: Sequence[Mapping[str, Any]],
        *,
        theme: ThemeDefinition,
    ) -> tuple[str, str, list[dict[str, str]]]:
        document = self._parser.parse(markdown)
        soup = BeautifulSoup("", "html.parser")
        root = soup.new_tag("section")
        root["data-layout-theme"] = theme.id
        root["data-renderer-version"] = self.version
        set_style(
            root,
            "max-width:677px;margin:0 auto;overflow-x:hidden;box-sizing:border-box",
            f"background:{theme.colors.background};color:{theme.colors.text}",
            f"font-family:{FONT_STACK};font-size:{theme.typography.body_size}",
            f"line-height:{theme.typography.body_line_height};letter-spacing:{theme.typography.letter_spacing}",
            f"padding:{theme.spacing.content_padding}",
        )
        for child in list(document.fragment.contents):
            root.append(child.extract())
        soup.append(root)

        inserted = insert_images(root, images, theme=theme, soup=soup)
        self._style_document(root, theme=theme, soup=soup)
        wrap_text_nodes_with_leaf(root, soup)
        sanitized = sanitize_fragment(str(root))
        final_html = self._wrap_document(sanitized, theme)
        return final_html, document.source_visible_text, inserted

    def _style_document(self, root: Tag, *, theme: ThemeDefinition, soup: BeautifulSoup) -> None:
        h2_number = 0
        for tag in list(root.find_all(True)):
            if tag.name == "h1":
                set_style(
                    tag,
                    f"margin:10px 0 34px;font-size:{theme.typography.h1_size}",
                    f"line-height:1.35;text-align:center;color:{theme.colors.heading}",
                    "font-weight:800",
                )
            elif tag.name == "h2":
                h2_number += 1
                replacement = replace_h2(tag, number=h2_number, theme=theme, soup=soup)
                if replacement is tag:
                    self._style_standard_h2(tag, theme)
            elif tag.name == "h3":
                replacement = replace_h3(tag, theme=theme, soup=soup)
                if replacement is tag:
                    set_style(
                        tag,
                        f"margin:30px 0 15px;font-size:{theme.typography.h3_size}",
                        f"line-height:1.55;color:{theme.colors.heading};font-weight:700",
                    )
            elif tag.name == "h4":
                set_style(
                    tag,
                    f"margin:24px 0 12px;font-size:{theme.typography.body_size}",
                    f"color:{theme.colors.heading};font-weight:700",
                )
            elif tag.name == "p":
                set_style(
                    tag,
                    f"margin:{theme.spacing.paragraph_margin};font-size:{theme.typography.body_size};line-height:{theme.typography.body_line_height};color:{theme.colors.text};text-align:justify;overflow-wrap:anywhere",
                )
            elif tag.name == "blockquote":
                style_blockquote(tag, theme)
            elif tag.name in {"ul", "ol"}:
                replace_list(tag, theme=theme, soup=soup)
            elif tag.name == "li":
                set_style(
                    tag,
                    f"margin:7px 0;line-height:{theme.typography.body_line_height};color:{theme.colors.text}",
                )
            elif tag.name == "strong":
                set_style(tag, f"color:{theme.colors.primary};font-weight:700")
            elif tag.name == "em":
                set_style(
                    tag,
                    f"color:{theme.colors.secondary};font-style:normal",
                    f"border-bottom:1px solid {theme.colors.border}",
                )
            elif tag.name == "a":
                set_style(tag, "color:#576B95;text-decoration:underline;overflow-wrap:anywhere")
            elif tag.name == "code" and tag.parent and tag.parent.name != "pre":
                set_style(
                    tag,
                    f"background:{theme.colors.code_background};color:{theme.colors.heading}",
                    "padding:2px 6px;border-radius:3px;font-size:13px",
                )
            elif tag.name == "pre":
                self._style_code(tag, theme)
            elif tag.name == "hr":
                style_divider(tag, theme)

    @staticmethod
    def _style_standard_h2(tag: Tag, theme: ThemeDefinition) -> None:
        profile = theme.variants.profile
        if profile == "navy":
            style = f"background:{theme.colors.primary};color:#FFFFFF;padding:10px 14px;border-radius:3px"
        elif profile == "editorial":
            style = (
                f"color:{theme.colors.heading};border-bottom:1px solid {theme.colors.border};"
                "padding-bottom:8px"
            )
        elif profile == "minimal":
            style = (
                f"color:{theme.colors.heading};border-top:1px solid {theme.colors.border};padding-top:14px"
            )
        else:
            style = (
                f"color:{theme.colors.heading};border-left:4px solid {theme.colors.primary};padding-left:12px"
            )
        set_style(
            tag,
            f"{theme.spacing.section_margin};font-size:{theme.typography.h2_size};line-height:1.5;font-weight:800",
            style,
        )

    @staticmethod
    def _style_code(tag: Tag, theme: ThemeDefinition) -> None:
        if theme.variants.code == "dark":
            background, color = theme.colors.code_background, "#E7EEF7"
        else:
            background, color = theme.colors.code_background, theme.colors.heading
        set_style(
            tag,
            f"margin:22px 0;padding:15px 16px;background:{background};color:{color}",
            f"border:1px solid {theme.colors.border};border-radius:4px",
            "white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere",
            "font-size:13px;line-height:1.65",
        )
        code = tag.find("code")
        if isinstance(code, Tag):
            set_style(code, "font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:inherit")

    @staticmethod
    def _wrap_document(fragment: str, theme: ThemeDefinition) -> str:
        title = theme.label
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{title}</title></head>"
            '<body style="margin:0;padding:16px 0;background:#FFFFFF;">'
            f"{fragment}</body></html>"
        )
