from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.rendering.skill_driven.component_registry import ComponentRegistry, SkillTheme
from app.rendering.skill_driven.contracts import (
    MarkdownLayoutDocument,
    MarkdownNode,
    SkillLayoutPlan,
    SkillRenderCandidate,
)
from app.rendering.wechat_layout.components.common import safe_http_url, set_style, wrap_text_nodes_with_leaf

FONT_STACK = "-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif"


class SkillHtmlAssembler:
    """Assemble inline WeChat HTML exclusively from registered GZH components."""

    version = "skill-component-assembler-v1"

    def __init__(self, registry: ComponentRegistry) -> None:
        self.registry = registry

    def assemble(
        self,
        *,
        document: MarkdownLayoutDocument,
        images: Sequence[Mapping[str, Any]],
        plan: SkillLayoutPlan,
    ) -> SkillRenderCandidate:
        theme = self.registry.theme(plan.theme_id)
        soup = BeautifulSoup("", "html.parser")
        root = soup.new_tag("section")
        root["data-layout-theme"] = theme.theme_id
        root["data-renderer-version"] = self.version
        root["data-skill-source"] = "gzh-design+xiaowan"
        root["data-skill-article-type"] = plan.article_type
        set_style(
            root,
            "max-width:677px;margin:0 auto;overflow-x:hidden;box-sizing:border-box",
            f"background:{theme.colors['paper']};color:{theme.colors['text']}",
            f"font-family:{FONT_STACK};font-size:15px;line-height:1.85;letter-spacing:0",
            "padding:0 16px 24px",
        )
        soup.append(root)

        root.append(self._hero(soup, theme, plan.hero_component_id, document.title))
        image_lookup = self._image_lookup(images)
        inserted: list[dict[str, str]] = []
        consumed_urls: set[str] = set()
        for index, node in enumerate(document.preamble):
            rendered = self._render_node(soup, theme, node, plan, emphasized=index == 0)
            root.append(rendered)
            key = (tuple(node.heading_path), node.content_ordinal)
            for item in image_lookup.get(key, ()):
                figure, metadata = self._image(soup, theme, plan.image_component_id, item)
                root.append(figure)
                inserted.append(metadata)
                consumed_urls.add(metadata["url"])

        if document.sections:
            root.append(self._toc(soup, theme, plan.toc_component_id, document))

        section_plan = {item.section_id: item for item in plan.sections}
        for section_number, section in enumerate(document.sections, 1):
            planned = section_plan[section.section_id]
            wrapper = soup.new_tag("section")
            wrapper["data-skill-section"] = section.section_id
            set_style(wrapper, "margin:48px 0 0")
            wrapper.append(
                self._heading(
                    soup,
                    theme,
                    planned.heading_component_id,
                    section.heading,
                    section_number,
                )
            )
            accent_ids = set(planned.accent_node_ids)
            for node in section.nodes:
                rendered = self._render_node(soup, theme, node, plan, emphasized=node.node_id in accent_ids)
                wrapper.append(rendered)
                key = (tuple(node.heading_path), node.content_ordinal)
                for item in image_lookup.get(key, ()):
                    figure, metadata = self._image(soup, theme, plan.image_component_id, item)
                    wrapper.append(figure)
                    inserted.append(metadata)
                    consumed_urls.add(metadata["url"])
            root.append(wrapper)

        for item in images:
            url = safe_http_url(item.get("url"))
            if not url or url in consumed_urls:
                continue
            figure, metadata = self._image(soup, theme, plan.image_component_id, item)
            root.append(figure)
            metadata["matched"] = "document_end"
            inserted.append(metadata)

        root.append(self._closing(soup, theme, plan.closing_component_id))
        self._style_inline_content(root, theme)
        wrap_text_nodes_with_leaf(root, soup)
        fragment = str(root)
        final_html = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{escape(theme.label)}</title></head>"
            '<body style="margin:0;padding:16px 0;background:#FFFFFF;">'
            f"{fragment}</body></html>"
        )
        return SkillRenderCandidate(
            final_html=final_html,
            theme_id=theme.theme_id,
            source_visible_text=document.source_visible_text,
            inserted_images=inserted,
            plan=plan,
            source_metadata={
                "gzh_theme_reference": theme.source_reference,
                "gzh_theme_sha256": theme.source_sha256,
                "xiaowan_policy": "content-freeze+image-evidence+mobile-layout",
            },
        )

    def _hero(
        self,
        soup: BeautifulSoup,
        theme: SkillTheme,
        component_id: str,
        title: MarkdownNode,
    ) -> Tag:
        box = self._component(soup, component_id, "hero")
        styles = {
            "moyu-green": (
                "margin:0 -16px 34px;padding:38px 24px 32px;border-radius:0 0 22px 22px;"
                f"background:linear-gradient(145deg,{theme.colors['deep']}, {theme.colors['primary']});"
                "box-shadow:0 14px 34px rgba(5,150,105,.22);color:#FFFFFF"
            ),
            "red-white": (
                "margin:8px 0 38px;padding:34px 24px 30px;background:#FFFFFF;"
                f"border-top:6px solid {theme.colors['primary']};"
                f"border-bottom:1px solid {theme.colors['line']};"
                "box-shadow:0 12px 36px rgba(220,38,38,.10);text-align:center"
            ),
            "graphite-minimal": (
                f"margin:16px 0 48px;padding:38px 18px;border-top:1px solid {theme.colors['line']};"
                f"border-bottom:1px solid {theme.colors['line']};background:#FFFFFF;text-align:center"
            ),
            "zen-whitespace": (
                f"margin:26px 0 58px;padding:42px 20px;border-top:1px solid {theme.colors['line']};"
                f"border-bottom:1px solid {theme.colors['line']};"
                f"background:{theme.colors['paper']};text-align:center"
            ),
            "moyu-ticket": (
                f"margin:10px 0 40px;padding:30px 22px;background:{theme.colors['paper']};"
                f"border:2px solid {theme.colors['line']};border-radius:8px;"
                "box-shadow:8px 8px 0 #111827"
            ),
            "olive-journal": (
                f"margin:0 -16px 42px;padding:0 0 26px;background:{theme.colors['paper']};"
                f"border-bottom:5px solid {theme.colors['accent']}"
            ),
        }
        set_style(box, styles[theme.theme_id])
        if component_id != theme.components["hero"].component_id:
            set_style(
                box,
                f"border-left:10px solid {theme.colors['accent']};border-radius:0 18px 18px 0",
            )
        eyebrow = soup.new_tag("p")
        eyebrow["data-layout-added"] = "true"
        labels = {
            "moyu-green": "WECHAT · FEATURE",
            "red-white": "FEATURE / 深度阅读",
            "graphite-minimal": "EDITORIAL NOTE",
            "zen-whitespace": "READ · REFLECT",
            "moyu-ticket": "ADMIT ONE · FEATURE",
            "olive-journal": "THE EDITORIAL JOURNAL",
        }
        set_style(
            eyebrow,
            "margin:0 0 16px;font-size:11px;line-height:1.4;font-weight:700;letter-spacing:1px",
            f"color:{'#FFFFFF' if theme.theme_id == 'moyu-green' else theme.colors['accent']}",
        )
        eyebrow.string = labels[theme.theme_id]
        box.append(eyebrow)
        if component_id != theme.components["hero"].component_id:
            variant = soup.new_tag("span")
            variant["data-layout-added"] = "true"
            set_style(
                variant,
                f"display:inline-block;margin:0 0 14px;padding:3px 10px;"
                f"border:1px solid {theme.colors['accent']};border-radius:999px;"
                f"color:{theme.colors['accent']};font-size:10px;font-weight:800",
            )
            variant.string = "CURATED LAYOUT"
            box.append(variant)
        h1 = soup.new_tag("h1")
        h1["data-source-node"] = title.node_id
        set_style(
            h1,
            "margin:0;font-size:24px;line-height:1.5;font-weight:800;letter-spacing:0",
            f"color:{'#FFFFFF' if theme.theme_id == 'moyu-green' else theme.colors['deep']}",
        )
        h1.string = title.text
        box.append(h1)
        line = soup.new_tag("section")
        line["data-layout-added"] = "true"
        set_style(
            line,
            "width:52px;height:4px;margin:22px 0 0;font-size:0;line-height:0",
            f"background:{'#FBBF24' if theme.theme_id == 'moyu-green' else theme.colors['accent']}",
        )
        line.string = "."
        box.append(line)
        return box

    def _toc(
        self,
        soup: BeautifulSoup,
        theme: SkillTheme,
        component_id: str,
        document: MarkdownLayoutDocument,
    ) -> Tag:
        box = self._component(soup, component_id, "toc")
        box["data-layout-added"] = "true"
        border = "2px solid" if theme.theme_id == "moyu-ticket" else "1px solid"
        shadow = "6px 6px 0 #111827" if theme.theme_id == "moyu-ticket" else "0 10px 30px rgba(0,0,0,.05)"
        set_style(
            box,
            f"margin:36px 0 44px;padding:22px 18px;background:{theme.colors['soft']};"
            f"border:{border} {theme.colors['line']};border-radius:10px;box-shadow:{shadow}",
        )
        if component_id != theme.components["toc"].component_id:
            set_style(
                box,
                f"background:{theme.colors['paper']};border-radius:0;box-shadow:none;"
                f"border-left:6px solid {theme.colors['accent']}",
            )
        heading = soup.new_tag("p")
        set_style(
            heading,
            f"margin:0 0 14px;color:{theme.colors['primary']};font-size:11px;"
            "font-weight:800;letter-spacing:1px",
        )
        heading.string = "CONTENTS · 本文导读"
        box.append(heading)
        for index, section in enumerate(document.sections, 1):
            row = soup.new_tag("section")
            set_style(
                row,
                "display:flex;align-items:flex-start;padding:10px 0;"
                f"border-top:1px solid {theme.colors['line']}",
            )
            number = soup.new_tag("span")
            set_style(
                number,
                f"display:inline-block;min-width:34px;color:{theme.colors['accent']};"
                "font-weight:800;font-size:12px",
            )
            number.string = f"{index:02d}"
            label = soup.new_tag("span")
            set_style(
                label,
                f"color:{theme.colors['deep']};font-size:14px;line-height:1.55;font-weight:650",
            )
            label.string = section.heading.text
            row.extend([number, label])
            box.append(row)
        return box

    def _heading(
        self,
        soup: BeautifulSoup,
        theme: SkillTheme,
        component_id: str,
        node: MarkdownNode,
        number: int,
    ) -> Tag:
        box = self._component(soup, component_id, "heading")
        base = "margin:0 0 24px;padding:0"
        if theme.theme_id == "moyu-green":
            base += (
                f";border-left:6px solid {theme.colors['accent']};padding:12px 16px;"
                f"background:{theme.colors['soft']};border-radius:0 10px 10px 0"
            )
        elif theme.theme_id == "red-white":
            base += (
                f";display:flex;align-items:center;border-bottom:2px solid {theme.colors['primary']};"
                "padding-bottom:12px"
            )
        elif theme.theme_id == "graphite-minimal":
            base += f";border-top:1px solid {theme.colors['line']};padding-top:20px"
        elif theme.theme_id == "zen-whitespace":
            base += f";padding:18px 0 12px;border-bottom:1px solid {theme.colors['line']}"
        elif theme.theme_id == "moyu-ticket":
            base += (
                f";border:2px solid {theme.colors['line']};padding:12px 14px;"
                f"background:{theme.colors['soft']};box-shadow:5px 5px 0 #111827"
            )
        else:
            base += (
                f";background:{theme.colors['deep']};padding:14px 16px;"
                f"border-left:8px solid {theme.colors['accent']}"
            )
        if component_id != theme.components["heading"].component_id:
            base += (
                f";border-radius:14px;border-right:1px solid {theme.colors['line']};"
                f"box-shadow:0 8px 22px {theme.colors['pale']}"
            )
        set_style(box, base)
        marker = soup.new_tag("span")
        marker["data-layout-added"] = "true"
        set_style(
            marker,
            "display:inline-block;margin-right:10px;font-size:12px;line-height:1;font-weight:900;vertical-align:middle",
            f"color:{'#FFFFFF' if theme.theme_id == 'olive-journal' else theme.colors['accent']}",
        )
        marker.string = f"{number:02d}"
        h2 = soup.new_tag("h2")
        h2["data-source-node"] = node.node_id
        set_style(
            h2,
            "display:inline;margin:0;font-size:20px;line-height:1.55;font-weight:800;vertical-align:middle",
            f"color:{'#FFFFFF' if theme.theme_id == 'olive-journal' else theme.colors['deep']}",
        )
        h2.string = node.text
        box.extend([marker, h2])
        return box

    def _render_node(
        self,
        soup: BeautifulSoup,
        theme: SkillTheme,
        node: MarkdownNode,
        plan: SkillLayoutPlan,
        *,
        emphasized: bool,
    ) -> Tag:
        parsed = BeautifulSoup(node.html, "html.parser")
        tag = next((item for item in parsed.contents if isinstance(item, Tag)), None)
        if tag is None:
            tag = soup.new_tag("p")
            tag.string = node.text
        tag.extract()
        tag["data-source-node"] = node.node_id

        if node.kind == "heading":
            component_id = f"{theme.theme_id}.{theme.components['heading'].component_id.split('.', 1)[1]}"
            wrapper = self._component(soup, component_id, "subheading")
            set_style(
                wrapper,
                f"margin:30px 0 16px;padding-left:12px;border-left:4px solid {theme.colors['accent']}",
            )
            set_style(
                tag, f"margin:0;color:{theme.colors['deep']};font-size:17px;line-height:1.55;font-weight:800"
            )
            wrapper.append(tag)
            return wrapper
        if node.kind == "blockquote":
            wrapper = self._component(soup, plan.quote_component_id, "quote")
            set_style(
                wrapper,
                f"margin:24px 0;padding:18px 20px;background:{theme.colors['soft']};"
                f"border-left:5px solid {theme.colors['primary']};border-radius:0 10px 10px 0",
            )
            set_style(
                tag, f"margin:0;color:{theme.colors['deep']};font-size:16px;line-height:1.8;font-weight:650"
            )
            wrapper.append(tag)
            return wrapper
        if node.kind == "list":
            wrapper = self._component(soup, plan.list_component_id, "list")
            shadow = "5px 5px 0 #111827" if theme.theme_id == "moyu-ticket" else "0 8px 24px rgba(0,0,0,.05)"
            set_style(
                wrapper,
                f"margin:22px 0;padding:14px 18px;background:{theme.colors['soft']};"
                f"border:1px solid {theme.colors['line']};border-radius:9px;box-shadow:{shadow}",
            )
            wrapper.append(tag)
            return wrapper
        if node.kind == "code":
            wrapper = self._component(soup, "common.code-dark", "code")
            set_style(
                wrapper,
                "margin:22px 0;padding:14px 16px;background:#172033;border-radius:8px;overflow:hidden",
            )
            set_style(
                tag,
                "margin:0;color:#E5EEF9;font-size:13px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere",
            )
            wrapper.append(tag)
            return wrapper
        if emphasized:
            wrapper = self._component(soup, plan.emphasis_component_id, "emphasis")
            border = "2px solid" if theme.theme_id == "moyu-ticket" else "1px solid"
            shadow = "6px 6px 0 #111827" if theme.theme_id == "moyu-ticket" else "0 12px 30px rgba(0,0,0,.07)"
            set_style(
                wrapper,
                f"margin:24px 0;padding:18px 20px;background:{theme.colors['soft']};"
                f"border:{border} {theme.colors['line']};border-top:4px solid {theme.colors['accent']};"
                f"border-radius:9px;box-shadow:{shadow}",
            )
            if plan.emphasis_component_id != theme.components["emphasis"].component_id:
                set_style(
                    wrapper,
                    f"border-left:8px solid {theme.colors['accent']};"
                    f"border-bottom:3px solid {theme.colors['primary']};"
                    "border-radius:2px 14px 14px 2px",
                )
            set_style(
                tag, f"margin:0;color:{theme.colors['deep']};font-size:15px;line-height:1.85;font-weight:600"
            )
            wrapper.append(tag)
            return wrapper
        tag["data-skill-component"] = plan.body_component_id
        tag["data-skill-semantic"] = "body"
        set_style(
            tag,
            f"margin:0 0 19px;color:{theme.colors['text']};font-size:15px;"
            "line-height:1.85;text-align:justify;overflow-wrap:anywhere",
        )
        return tag

    def _image(
        self,
        soup: BeautifulSoup,
        theme: SkillTheme,
        component_id: str,
        item: Mapping[str, Any],
    ) -> tuple[Tag, dict[str, str]]:
        url = safe_http_url(item.get("url"))
        if not url:
            raise ValueError("Image URL is not allowed")
        caption = str(item.get("caption") or "").strip()
        figure = soup.new_tag("figure")
        figure["data-skill-component"] = component_id
        figure["data-skill-semantic"] = "image"
        figure["data-layout-added"] = "image"
        figure["data-image-url"] = url
        shadow = "7px 7px 0 #111827" if theme.theme_id == "moyu-ticket" else "0 14px 34px rgba(0,0,0,.10)"
        set_style(
            figure,
            f"margin:28px 0 30px;padding:7px;background:{theme.colors['paper']};"
            f"border:1px solid {theme.colors['line']};border-radius:12px;"
            f"box-shadow:{shadow};text-align:center",
        )
        image = soup.new_tag("img", src=url, alt=caption)
        set_style(image, "display:block;max-width:100%;height:auto;margin:0 auto;border-radius:8px")
        figure.append(image)
        if caption:
            figcaption = soup.new_tag("figcaption")
            set_style(
                figcaption,
                f"padding:10px 8px 5px;color:{theme.colors['muted']};font-size:12px;line-height:1.6",
            )
            figcaption.string = caption
            figure.append(figcaption)
        return figure, {"url": url, "caption": caption, "matched": "exact"}

    def _closing(self, soup: BeautifulSoup, theme: SkillTheme, component_id: str) -> Tag:
        box = self._component(soup, component_id, "closing")
        box["data-layout-added"] = "true"
        set_style(
            box,
            "margin:56px 0 10px;padding:28px 0 8px;"
            f"border-top:1px solid {theme.colors['line']};text-align:center",
        )
        mark = soup.new_tag("p")
        set_style(
            mark, f"margin:0;color:{theme.colors['accent']};font-size:12px;font-weight:800;letter-spacing:1px"
        )
        mark.string = "— END —"
        box.append(mark)
        return box

    @staticmethod
    def _component(soup: BeautifulSoup, component_id: str, semantic: str) -> Tag:
        tag = soup.new_tag("section")
        tag["data-skill-component"] = component_id
        tag["data-skill-semantic"] = semantic
        return tag

    @staticmethod
    def _image_lookup(
        images: Sequence[Mapping[str, Any]],
    ) -> dict[tuple[tuple[str, ...], int | None], list[Mapping[str, Any]]]:
        result: dict[tuple[tuple[str, ...], int | None], list[Mapping[str, Any]]] = {}
        for item in images:
            position = item.get("insertion_position")
            position = position if isinstance(position, Mapping) else {}
            key = (
                tuple(
                    str(value).strip() for value in position.get("heading_path") or [] if str(value).strip()
                ),
                _integer(position.get("paragraph_ordinal")),
            )
            result.setdefault(key, []).append(item)
        return result

    @staticmethod
    def _style_inline_content(root: Tag, theme: SkillTheme) -> None:
        for tag in root.find_all(True):
            if tag.name == "strong":
                set_style(tag, f"color:{theme.colors['primary']};font-weight:750")
            elif tag.name == "em":
                set_style(
                    tag,
                    f"color:{theme.colors['deep']};font-style:normal;"
                    f"border-bottom:2px solid {theme.colors['line']}",
                )
            elif tag.name == "a":
                set_style(tag, "color:#576B95;text-decoration:underline;overflow-wrap:anywhere")
            elif tag.name == "li":
                set_style(tag, "margin:8px 0;line-height:1.8")
            elif tag.name == "code" and tag.parent and tag.parent.name != "pre":
                set_style(
                    tag,
                    f"padding:2px 5px;background:{theme.colors['pale']};"
                    f"color:{theme.colors['primary']};border-radius:4px;font-size:13px",
                )


def component_counts(html: str) -> Counter[str]:
    soup = BeautifulSoup(html, "html.parser")
    return Counter(str(tag.get("data-skill-semantic")) for tag in soup.select("[data-skill-semantic]"))


def _integer(value: Any) -> int | None:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None
