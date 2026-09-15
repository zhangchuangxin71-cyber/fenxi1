from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence

from bs4 import BeautifulSoup, Tag

from app.rendering.skill_driven.component_registry import ComponentRegistry
from app.rendering.skill_driven.contracts import (
    ComponentSignature,
    MarkdownLayoutDocument,
    SkillRenderCandidate,
    SkillValidationReport,
)
from app.rendering.wechat_layout.components.common import safe_http_url
from app.rendering.wechat_layout.contracts import ValidationIssue
from app.rendering.wechat_layout.text import normalize_visible_text

_FORBIDDEN_CSS = {
    "POSITION": re.compile(r"position\s*:\s*(?:fixed|absolute|sticky)", re.I),
    "FLOAT": re.compile(r"(?:^|;)\s*float\s*:", re.I),
    "GRID": re.compile(r"display\s*:\s*grid", re.I),
    "CSS_VARIABLE": re.compile(r"var\s*\(\s*--", re.I),
    "ANIMATION": re.compile(r"(?:animation|transition)\s*:", re.I),
    "FIXED_WIDTH": re.compile(r"(?:^|;)\s*width\s*:\s*(?:[4-9]\d\d|\d{4,})px", re.I),
}


def validate_candidate(
    candidate: SkillRenderCandidate,
    *,
    document: MarkdownLayoutDocument,
    images: Sequence[Mapping[str, object]],
    registry: ComponentRegistry,
) -> SkillValidationReport:
    soup = BeautifulSoup(candidate.final_html, "html.parser")
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    for tag_name in ("script", "style", "link", "div"):
        if soup.find(tag_name) is not None:
            errors.append(_issue(f"GZH_FORBIDDEN_{tag_name.upper()}", f"禁止使用 <{tag_name}>。"))
    for tag in soup.find_all(True):
        if tag.has_attr("class") or tag.has_attr("id"):
            errors.append(_issue("GZH_CLASS_OR_ID", "公众号组件不能依赖 class 或 id。", tag=tag.name))
        if any(str(name).lower().startswith("on") for name in tag.attrs):
            errors.append(_issue("GZH_EVENT_ATTRIBUTE", "禁止事件处理属性。", tag=tag.name))
        style = str(tag.get("style") or "")
        for code, pattern in _FORBIDDEN_CSS.items():
            if pattern.search(style):
                errors.append(_issue(f"GZH_{code}", "包含公众号不兼容或移动端高风险 CSS。", tag=tag.name))
        for attr in ("src", "href"):
            value = tag.get(attr)
            if value and not str(value).startswith(("http://", "https://", "mailto:")):
                errors.append(_issue("GZH_UNSAFE_URL", "URL 不在允许协议中。", value=str(value)))

    source_nodes = soup.select("[data-source-node]")
    actual_text = normalize_visible_text(" ".join(tag.get_text(" ", strip=True) for tag in source_nodes))
    if actual_text != document.source_visible_text:
        errors.append(
            _issue(
                "XIAOWAN_CONTENT_FREEZE",
                "排版后的来源节点文本与已批准 Markdown 不一致。",
                expected_chars=len(document.source_visible_text),
                actual_chars=len(actual_text),
            )
        )
    expected_node_ids = _expected_node_ids(document)
    actual_node_ids = [str(tag.get("data-source-node")) for tag in source_nodes]
    if actual_node_ids != expected_node_ids:
        errors.append(
            _issue(
                "XIAOWAN_NODE_ORDER",
                "来源节点存在遗漏、重复或顺序变化。",
                expected=expected_node_ids,
                actual=actual_node_ids,
            )
        )

    expected_urls = [safe_http_url(item.get("url")) for item in images]
    expected_urls = [url for url in expected_urls if url]
    actual_urls = [str(tag.get("src")) for tag in soup.find_all("img") if tag.get("src")]
    if actual_urls != expected_urls:
        errors.append(_issue("XIAOWAN_IMAGE_EVIDENCE", "图片 URL、数量或顺序与 artifact 不一致。"))
    expected_captions = [
        str(item.get("caption") or "").strip()
        for item in images
        if safe_http_url(item.get("url")) and str(item.get("caption") or "").strip()
    ]
    actual_captions = [tag.get_text(" ", strip=True) for tag in soup.find_all("figcaption")]
    if actual_captions != expected_captions:
        errors.append(_issue("XIAOWAN_IMAGE_CAPTION", "图片标注与 artifact 不一致。"))

    unwrapped = [
        text
        for text in soup.find_all(string=re.compile(r"[\u3400-\u9fff]"))
        if not any(
            isinstance(parent, Tag) and parent.name == "span" and parent.has_attr("leaf")
            for parent in text.parents
        )
        and not any(isinstance(parent, Tag) and parent.name in {"head", "title"} for parent in text.parents)
    ]
    if unwrapped:
        errors.append(_issue("GZH_UNWRAPPED_TEXT", "中文文本必须由 span[leaf] 包裹。", count=len(unwrapped)))

    semantic_counts = Counter(
        str(tag.get("data-skill-semantic")) for tag in soup.select("[data-skill-semantic]")
    )
    component_ids = list(
        dict.fromkeys(str(tag.get("data-skill-component")) for tag in soup.select("[data-skill-component]"))
    )
    theme_specific = {
        str(tag.get("data-skill-semantic"))
        for tag in soup.select(f'[data-skill-component^="{candidate.theme_id}."]')
    }
    signature = ComponentSignature(
        theme_id=candidate.theme_id,
        component_ids=component_ids,
        semantic_counts=dict(semantic_counts),
        distinct_semantics=len(semantic_counts),
        theme_specific_semantics=len(theme_specific),
    )
    if len(document.sections) >= 3 and signature.distinct_semantics < 5:
        errors.append(_issue("SKILL_LAYOUT_NO_EFFECT", "组件语义不足，排版未形成有效结构变化。"))
    if len(document.sections) >= 3 and signature.theme_specific_semantics < 3:
        errors.append(_issue("SKILL_LAYOUT_NO_EFFECT", "主题专属组件不足。"))
    required = {"hero", "heading", "body", "closing"}
    if document.sections:
        required.add("toc")
    missing = sorted(required - set(semantic_counts))
    if missing:
        errors.append(_issue("SKILL_LAYOUT_COMPONENT_MISSING", "缺少必须组件语义。", missing=missing))

    root = soup.find("section", attrs={"data-layout-theme": candidate.theme_id})
    if not isinstance(root, Tag):
        errors.append(_issue("GZH_ROOT_MISSING", "缺少 Skill 文章根节点。"))
    if soup.select_one('[data-layout-theme="professional-clean"]') is not None:
        errors.append(_issue("SKILL_LAYOUT_OLD_RENDERER", "Skill 结果混入旧代码主题。"))
    if len(soup.select("[style*='box-shadow']")) > max(12, len(document.sections) * 3):
        warnings.append(_issue("XIAOWAN_DECORATION_DENSE", "阴影组件密度偏高，建议人工观察移动端节奏。"))

    return SkillValidationReport(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        metrics={
            "source_chars": len(document.source_visible_text),
            "rendered_source_chars": len(actual_text),
            "images": len(actual_urls),
            "leaf_spans": len(soup.select("span[leaf]")),
            "distinct_semantics": signature.distinct_semantics,
            "theme_specific_semantics": signature.theme_specific_semantics,
            "html_chars": len(candidate.final_html),
            "mobile_max_width": 677,
        },
        signature=signature,
    )


def _expected_node_ids(document: MarkdownLayoutDocument) -> list[str]:
    result = [document.title.node_id]
    result.extend(node.node_id for node in document.preamble)
    for section in document.sections:
        result.append(section.heading.node_id)
        result.extend(node.node_id for node in section.nodes)
    return result


def _issue(code: str, message: str, **details: object) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, details=dict(details))
