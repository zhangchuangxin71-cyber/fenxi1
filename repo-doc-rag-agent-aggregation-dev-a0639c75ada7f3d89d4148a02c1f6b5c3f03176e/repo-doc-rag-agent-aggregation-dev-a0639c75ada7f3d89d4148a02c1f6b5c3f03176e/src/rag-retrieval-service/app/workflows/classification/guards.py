from __future__ import annotations

import re

from app.api.schemas import RetrievalCategory

_SUMMARY_TERMS = (
    "总结",
    "解释",
    "讲了什么",
    "讲什么",
    "主要讲",
    "汇总",
    "概述",
    "概括",
    "概览",
    "主要内容",
    "研究方向",
)
_DOCUMENT_NOUNS = ("文档", "文件", "材料", "资料", "论文", "报告", "年报", "法律")

_SCOPE_VISIBILITY_PATTERNS = (
    "你能看到什么",
    "你能查到什么",
    "你能访问什么",
    "你能看到哪些信息",
    "你能查到哪些信息",
    "你能访问哪些信息",
    "你能看到哪些文档",
    "你能查到哪些文档",
    "你能访问哪些文档",
    "你能看到几篇文档",
    "你能查到几篇文档",
    "能看到几篇文档",
    "能查到几篇文档",
    "有几篇文档",
    "有几份文档",
    "当前文档有哪些",
    "列出文档",
    "列举文档",
    "文档列表",
)
_COLLECTION_MARKERS = (
    "这些文档",
    "这些文件",
    "这些材料",
    "这些资料",
    "这批文档",
    "这批文件",
    "这批材料",
    "这批论文",
    "上述文档",
    "上述文件",
    "上述材料",
    "上述所有",
    "所有文档",
    "所有文件",
    "全部文档",
    "全部文件",
    "你看到的文档",
    "你能看到的文档",
    "你查到的文档",
    "当前可见文档",
)
_COUNTED_COLLECTION_RE = re.compile(
    r"(?:这|当前这|当前的这|当前的|当前请求的|当前用户请求的|上述的?|全部|所有)\s*"
    r"[0-9零一二两三四五六七八九十百千]+\s*"
    r"(?:篇|份|个)\s*(?:文档|文件|材料|资料|论文|报告)"
)
_UNRESOLVED_ORDINAL_RE = re.compile(
    r"(?:当前的?|这(?:些)?|上述的?)?\s*第\s*"
    r"[0-9零一二两三四五六七八九十百千]+\s*篇\s*"
    r"(?:文档|文件|材料|资料|论文|报告)"
)

_DIRECT_PATTERNS = (
    "目录",
    "章节结构",
    "标题结构",
    "有几页",
    "多少页",
    "页数",
    "有几章",
    "多少章",
    "章节数",
)
_SPECIFIED_RESOURCE_RE = re.compile(r"第\s*[0-9零一二两三四五六七八九十百千]+\s*(?:页|章|章节)")

_EXPLICIT_BROAD_PATTERNS = (
    "详细总结",
    "详细地总结",
    "全面总结",
    "深入总结",
    "全文总结",
    "完整总结",
    "深入解释",
    "详细解释",
    "全文解释",
    "整体内容",
    "完整内容",
    "整体比较",
    "整体对比",
    "全文比较",
    "全文对比",
    "整体有什么区别",
    "整体有什么差别",
    "整体讲了什么",
    "整体讲什么",
)


def guard_category(query: str) -> RetrievalCategory | None:
    """Return only a high-confidence correction for robust classification."""

    text = _normalize(query)
    if _is_scope(text):
        return "scope_direct"
    if _is_direct(text):
        return "routed_direct"
    if _is_broad(text):
        return "routed_broad"
    return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.casefold())


def _contains_summary_term(text: str) -> bool:
    return any(term in text for term in _SUMMARY_TERMS)


def _is_scope(text: str) -> bool:
    if any(pattern in text for pattern in _SCOPE_VISIBILITY_PATTERNS):
        return True
    if _contains_summary_term(text) and any(marker in text for marker in _COLLECTION_MARKERS):
        return True
    if _contains_summary_term(text) and _COUNTED_COLLECTION_RE.search(text):
        return True
    # An ordinal such as “当前的第 1 篇文档” has no resolvable document identity in
    # the retrieval contract. Treating its summary as request-scope is the safe fallback.
    return _contains_summary_term(text) and bool(_UNRESOLVED_ORDINAL_RE.search(text))


def _is_direct(text: str) -> bool:
    return any(pattern in text for pattern in _DIRECT_PATTERNS) or bool(
        _SPECIFIED_RESOURCE_RE.search(text)
    )


def _is_broad(text: str) -> bool:
    if any(pattern in text for pattern in _EXPLICIT_BROAD_PATTERNS):
        return True
    return _contains_summary_term(text) and any(noun in text for noun in _DOCUMENT_NOUNS)
