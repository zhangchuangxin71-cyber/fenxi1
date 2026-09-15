from __future__ import annotations

import json
from typing import Literal

from app.api.schemas import RetrievalWarning
from app.core.models import CandidateChunk, ToolOutput
from app.db.repositories import DocumentProfile

SCOPE_ENUMERATION_LIMIT = 10
_ACCESS_HINT = (
    "当前请求范围内的所有文档均可访问；此处仅列举其中若干篇作为示例。"
    "如需查看其他文档，请询问用户希望具体访问哪一篇文档。"
)


def _truncation_warning(group_ref: str, total: int, shown: int) -> list[RetrievalWarning]:
    if total <= shown:
        return []
    return [
        RetrievalWarning(
            code="SCOPE_ENUMERATION_TRUNCATED",
            message=f"当前范围有 {total} 篇文档，仅列举前 {shown} 篇。",
            affected_group_refs=[group_ref],
        )
    ]


def build_scope_metainfo(
    *,
    profiles: list[DocumentProfile],
    group_ref: str,
    questions: list[str],
    fields: list[Literal["doc_name", "page_count", "chapter_count"]],
    enumeration_limit: int = SCOPE_ENUMERATION_LIMIT,
) -> ToolOutput:
    requested = max(1, int(enumeration_limit))
    shown = profiles[: min(requested, len(profiles))]
    entries: list[dict[str, object]] = []
    for profile in shown:
        entry: dict[str, object] = {}
        if "doc_name" in fields:
            entry["doc_name"] = profile.doc_name
        if "page_count" in fields:
            entry["page_count"] = profile.page_count
        if "chapter_count" in fields:
            entry["chapter_count"] = profile.node_count
        entries.append(entry)
    if requested > len(profiles):
        access_hint = f"当前用户只有 {len(profiles)} 篇文档，已经列举所有文档。"
    elif len(shown) < len(profiles):
        access_hint = _ACCESS_HINT
    else:
        access_hint = "当前请求范围内的所有文档均可访问；已列举全部文档。"
    content = (
        f"当前请求范围共包含 {len(profiles)} 篇可访问文档；此处列举 {len(shown)} 篇。\n"
        f"{json.dumps(entries, ensure_ascii=False)}\n{access_hint}"
    )
    chunk = CandidateChunk(
        chunk_id=f"scope:{group_ref}:metainfo",
        document_id=shown[0].doc_id if shown else None,
        document_ids=[profile.doc_id for profile in shown],
        document_name="system",
        path="request:scope",
        content=content,
        hint=f"本 chunk 用于回答：{'；'.join(questions)}。{access_hint}",
        source_type="scope_metadata",
        category="scope_direct",
        group_matches={group_ref: "accept"},
        questions_by_group={group_ref: list(questions)},
        chunk_meta={
            "total_documents": len(profiles),
            "requested_documents": requested,
            "shown_documents": len(shown),
        },
    )
    warnings = _truncation_warning(group_ref, len(profiles), len(shown))
    return ToolOutput(chunks=[chunk], warnings=warnings, coverage_complete=not warnings)


def build_scope_descriptions(
    *, profiles: list[DocumentProfile], group_ref: str, questions: list[str]
) -> ToolOutput:
    shown = profiles[:SCOPE_ENUMERATION_LIMIT]
    truncated = len(profiles) > len(shown)
    suffix = f" {_ACCESS_HINT}" if truncated else ""
    chunks = [
        CandidateChunk(
            chunk_id=f"scope:{group_ref}:description:{index}",
            document_id=profile.doc_id,
            document_ids=[profile.doc_id],
            document_name=profile.doc_name,
            path="request:scope",
            content=(
                f"这是《{profile.doc_name}》（文件类型：{profile.doc_type or '未知'}）"
                f"的简要概览：{profile.doc_description}"
            ),
            hint=f"本 chunk 用于回答：{'；'.join(questions)}。{suffix}".strip(),
            source_type="document_overview",
            category="scope_direct",
            group_matches={group_ref: "accept"},
            questions_by_group={group_ref: list(questions)},
            chunk_meta={
                "scope_total_documents": len(profiles),
                "scope_shown_documents": len(shown),
            },
        )
        for index, profile in enumerate(shown, start=1)
    ]
    warnings = _truncation_warning(group_ref, len(profiles), len(shown))
    return ToolOutput(chunks=chunks, warnings=warnings, coverage_complete=not warnings)
