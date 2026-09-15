from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.rendering.wechat_layout.components.common import safe_http_url
from app.rendering.wechat_layout.contracts import ValidationIssue, ValidationReport
from app.rendering.wechat_layout.text import normalize_visible_text

BANNED_CSS = {
    "position_fixed": re.compile(r"position\s*:\s*(?:fixed|absolute|sticky)", re.I),
    "float": re.compile(r"(?:^|;)\s*float\s*:", re.I),
    "grid": re.compile(r"display\s*:\s*grid", re.I),
    "css_variable": re.compile(r"var\s*\(\s*--", re.I),
    "animation": re.compile(r"(?:animation|transition)\s*:", re.I),
    "external_css_url": re.compile(r"url\s*\(\s*['\"]?https?://", re.I),
}


class HtmlValidator:
    def validate(
        self,
        html: str,
        *,
        source_visible_text: str,
        expected_images: Sequence[Mapping[str, Any]],
    ) -> ValidationReport:
        soup = BeautifulSoup(html, "html.parser")
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []

        if soup.find("script") is not None:
            errors.append(self._issue("SCRIPT_TAG", "HTML contains a script tag."))
        for tag in soup.find_all(True):
            event_attributes = [name for name in tag.attrs if str(name).lower().startswith("on")]
            if event_attributes:
                errors.append(
                    self._issue(
                        "EVENT_ATTRIBUTE",
                        "HTML contains an event handler attribute.",
                        tag=tag.name,
                        attributes=event_attributes,
                    )
                )
            for name in ("href", "src"):
                value = tag.get(name)
                if value and not str(value).startswith(("http://", "https://", "mailto:")):
                    errors.append(
                        self._issue(
                            "UNSAFE_URL", "HTML contains a URL outside the allowlist.", value=str(value)
                        )
                    )

        root = soup.find("section", attrs={"data-layout-theme": True})
        legacy_root = soup.find("article", class_="wechat-article")
        content_root = root or legacy_root
        if not isinstance(content_root, Tag):
            errors.append(
                self._issue("MISSING_CONTENT_ROOT", "HTML does not contain a rendered article root.")
            )
            actual_visible_text = ""
        else:
            comparable = BeautifulSoup(str(content_root), "html.parser")
            for added in comparable.select('[data-layout-added="true"], figcaption'):
                added.decompose()
            actual_visible_text = normalize_visible_text(comparable.get_text(" ", strip=True))
            if actual_visible_text != source_visible_text:
                errors.append(
                    self._issue(
                        "CONTENT_MISMATCH",
                        "Rendered visible article text differs from approved Markdown.",
                        expected_chars=len(source_visible_text),
                        actual_chars=len(actual_visible_text),
                    )
                )

        safe_expected = [safe_http_url(item.get("url")) for item in expected_images]
        expected_urls = [value for value in safe_expected if value]
        actual_urls = [str(tag.get("src")) for tag in soup.find_all("img") if tag.get("src")]
        if actual_urls != expected_urls:
            errors.append(
                self._issue(
                    "IMAGE_MISMATCH",
                    "Rendered image URLs or order differ from the image artifact.",
                    expected=expected_urls,
                    actual=actual_urls,
                )
            )
        actual_captions = [tag.get_text(" ", strip=True) for tag in soup.find_all("figcaption")]
        expected_captions = [
            str(item.get("caption") or "").strip()
            for item in expected_images
            if safe_http_url(item.get("url")) and str(item.get("caption") or "").strip()
        ]
        if actual_captions != expected_captions:
            errors.append(
                self._issue(
                    "IMAGE_CAPTION_MISMATCH",
                    "Rendered image captions differ from the image artifact.",
                    expected=expected_captions,
                    actual=actual_captions,
                )
            )

        if soup.find("style") is not None:
            warnings.append(self._issue("STYLE_TAG", "A style tag may be filtered by the WeChat editor."))
        if soup.find("link") is not None:
            errors.append(self._issue("EXTERNAL_STYLESHEET", "External stylesheets are not allowed."))
        compatibility_counts: Counter[str] = Counter()
        for tag in soup.find_all(True):
            if tag.name == "div":
                compatibility_counts["div"] += 1
            if tag.has_attr("class"):
                compatibility_counts["class"] += 1
            if tag.has_attr("id"):
                compatibility_counts["id"] += 1
            style = str(tag.get("style") or "")
            for code, pattern in BANNED_CSS.items():
                if pattern.search(style):
                    compatibility_counts[code] += 1
        for code, count in sorted(compatibility_counts.items()):
            warnings.append(
                self._issue(
                    f"WECHAT_{code.upper()}",
                    "The HTML uses markup or CSS that may not survive WeChat paste.",
                    count=count,
                )
            )

        cjk_text_nodes = [
            value
            for value in (content_root.find_all(string=True) if isinstance(content_root, Tag) else [])
            if re.search(r"[\u3400-\u9fff]", str(value))
        ]
        unwrapped = [
            value
            for value in cjk_text_nodes
            if not any(
                isinstance(parent, Tag) and parent.name == "span" and parent.has_attr("leaf")
                for parent in value.parents
            )
        ]
        if unwrapped:
            warnings.append(
                self._issue(
                    "WECHAT_UNWRAPPED_TEXT",
                    "Some Chinese text nodes are not wrapped by span[leaf].",
                    count=len(unwrapped),
                )
            )

        return ValidationReport(
            valid=not errors,
            errors=errors,
            warnings=warnings,
            metrics={
                "source_chars": len(source_visible_text),
                "rendered_chars": len(actual_visible_text),
                "images": len(actual_urls),
                "leaf_spans": len(soup.select("span[leaf]")),
                "warnings": len(warnings),
            },
        )

    @staticmethod
    def _issue(code: str, message: str, **details: Any) -> ValidationIssue:
        return ValidationIssue(code=code, message=message, details=details)
