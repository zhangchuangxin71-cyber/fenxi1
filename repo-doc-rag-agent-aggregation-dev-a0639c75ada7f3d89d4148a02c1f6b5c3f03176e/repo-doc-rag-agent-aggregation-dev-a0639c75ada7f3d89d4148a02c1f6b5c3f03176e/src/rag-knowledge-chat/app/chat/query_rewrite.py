from __future__ import annotations

import re

from app.chat.models import RouteDecision

_INCREMENTAL_LISTING_PATTERN = re.compile(
    r"^(?:你能看到)?(?:我|用户)?(?:本次|这次|刚刚|刚)?"
    r"(?:新增|新加|上传|传)(?:了|的)?(?:哪些|什么)?(?:文档|文件|材料)"
    r"(?:吗|呢)?[?？。 ]*$"
)
_INCREMENTAL_REFERENCE_PATTERN = re.compile(
    r"(?:我|用户)?(?:本次|这次|刚刚|刚)?"
    r"(?:新增|新加|上传|传)(?:了|的)?(?:文档|文件|材料)"
)

_UNRESOLVED_DOCUMENT_REFERENCE_PATTERN = re.compile(
    r"(?:这|那|该)(?:篇|份|个|部)?(?:文档|文件|报告|制度|材料|论文)"
    r"|(?:上|下)(?:一)?(?:篇|份|个|部)(?:文档|文件|报告|制度|材料|论文)"
    r"|(?:新增|新上传|刚上传|刚传|新加)(?:的)?(?:文档|文件|材料)"
)
_CURRENT_SINGULAR_DOCUMENT_REFERENCE_PATTERN = re.compile(r"这(?:篇|份|个)(?:文档|文件|报告|制度|材料|论文)")


def _document_targets(document_names: list[str]) -> str:
    return "、".join(f"《{name}》" for name in document_names)


def _resolve_one(question: str, document_names: list[str]) -> str:
    if not document_names:
        return question
    targets = _document_targets(document_names)
    if _INCREMENTAL_LISTING_PATTERN.fullmatch(question.strip()):
        return f"查看以下文档的名称与元信息：{targets}"
    return _INCREMENTAL_REFERENCE_PATTERN.sub(targets, question)


def resolve_incremental_document_references(
    query: str | list[str], document_names: list[str]
) -> str | list[str]:
    if isinstance(query, str):
        return _resolve_one(query, document_names)
    rewritten = [_resolve_one(question, document_names) for question in query]
    return list(dict.fromkeys(question.strip() for question in rewritten if question.strip()))


def downgrade_unresolved_route_queries(
    decision: RouteDecision,
    *,
    unique_incremental_document_name: str | None = None,
) -> list[str]:
    """Apply narrow reference guards before retrieval."""

    downgraded: list[str] = []
    for item in decision.queries:
        if unique_incremental_document_name:
            rewritten, replacements = _CURRENT_SINGULAR_DOCUMENT_REFERENCE_PATTERN.subn(
                lambda _: f"《{unique_incremental_document_name}》",
                item.rewrite_query,
            )
            if replacements:
                item.rewrite_query = rewritten
                if not _UNRESOLVED_DOCUMENT_REFERENCE_PATTERN.search(rewritten):
                    item.status = "resolved"
                    item.reason = "本轮只有一篇增量文档，当前单数文档指代已确定。"
        if item.status != "resolved" or not _UNRESOLVED_DOCUMENT_REFERENCE_PATTERN.search(item.rewrite_query):
            continue
        item.status = "ambiguous"
        item.reason = "改写结果仍包含无法唯一确定的文档指代。"
        downgraded.append(item.rewrite_query)
    decision.query = decision.query_items
    return downgraded
