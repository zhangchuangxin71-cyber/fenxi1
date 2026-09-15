from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    chunks: list[dict[str, Any]]
    prompt_text: str


def build_evidence(chunks: list[dict[str, Any]]) -> EvidenceBundle:
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    blocks: list[str] = []
    for chunk in chunks:
        chunk_id = str(chunk.get("chunk_id", "")).strip()
        if not chunk_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        item = {
            "citation_index": len(normalized) + 1,
            "chunk_id": chunk_id,
            "document_id": str(chunk.get("document_id") or ""),
            "document_name": str(chunk.get("document_name") or ""),
            "page_number": chunk.get("page_number"),
            "path": str(chunk.get("path", "")),
            "content": str(chunk.get("content", "")),
            "hint": str(chunk.get("hint") or ""),
            "score": float(chunk.get("score", 0) or 0),
            "source_type": str(chunk.get("source_type", "")),
            "document_meta": chunk.get("document_meta"),
            "chunk_meta": chunk.get("chunk_meta") or {},
        }
        normalized.append(item)
        blocks.append(
            f"[{item['citation_index']}] 文档：{item['document_name']}；位置：{item['path']}\n"
            f"{item['content']}"
        )
    return EvidenceBundle(chunks=normalized, prompt_text="\n\n".join(blocks))


def build_references(answer: str, chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    by_index = {int(chunk["citation_index"]): chunk for chunk in chunks}
    markers = [int(value) for value in re.findall(r"\[(\d+)]", answer)]
    references: list[dict[str, Any]] = []
    invalid: list[int] = []
    used: set[int] = set()
    for marker in markers:
        if marker not in by_index:
            if marker not in invalid:
                invalid.append(marker)
            continue
        if marker in used:
            continue
        used.add(marker)
        chunk = by_index[marker]
        doc_id = chunk["document_id"] or None
        # Scope aggregate chunks carry one representative document ID for clients,
        # while the system name marks that the source is request-level metadata.
        is_system_reference = doc_id is None or chunk["document_name"] == "system"
        references.append(
            {
                "citation_index": marker,
                "doc_id": doc_id,
                "doc_name": "system" if is_system_reference else chunk["document_name"] or None,
                "page_number": 1 if is_system_reference else chunk["page_number"],
            }
        )
    return references, invalid
