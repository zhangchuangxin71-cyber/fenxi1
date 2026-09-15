from __future__ import annotations

import re

import bleach
from bleach.css_sanitizer import CSSSanitizer

ALLOWED_TAGS = {
    "section",
    "h1",
    "h2",
    "h3",
    "h4",
    "p",
    "span",
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
    "figure",
    "figcaption",
    "img",
}

ALLOWED_CSS_PROPERTIES = {
    "align-items",
    "background",
    "background-color",
    "border",
    "border-bottom",
    "border-color",
    "border-left",
    "border-radius",
    "border-style",
    "border-top",
    "border-width",
    "box-shadow",
    "box-sizing",
    "color",
    "display",
    "flex",
    "flex-direction",
    "flex-shrink",
    "font-family",
    "font-size",
    "font-style",
    "font-weight",
    "gap",
    "height",
    "justify-content",
    "letter-spacing",
    "line-height",
    "margin",
    "margin-bottom",
    "margin-left",
    "margin-right",
    "margin-top",
    "max-width",
    "min-width",
    "overflow",
    "overflow-wrap",
    "overflow-x",
    "padding",
    "padding-bottom",
    "padding-left",
    "padding-right",
    "padding-top",
    "text-align",
    "text-decoration",
    "vertical-align",
    "white-space",
    "width",
    "word-break",
    "word-wrap",
}

CSS_SANITIZER = CSSSanitizer(allowed_css_properties=ALLOWED_CSS_PROPERTIES)
_DATA_ATTRIBUTE = re.compile(r"^data-[a-z0-9-]+$")


def _allowed_attribute(tag: str, name: str, value: str) -> bool:
    del value
    if name == "style" or _DATA_ATTRIBUTE.match(name):
        return True
    if tag == "span" and name == "leaf":
        return True
    if tag == "img" and name in {"src", "alt"}:
        return True
    return tag == "a" and name in {"href", "title"}


def sanitize_fragment(html: str) -> str:
    return bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=_allowed_attribute,
        protocols={"http", "https", "mailto"},
        css_sanitizer=CSS_SANITIZER,
        strip=True,
        strip_comments=True,
    )
