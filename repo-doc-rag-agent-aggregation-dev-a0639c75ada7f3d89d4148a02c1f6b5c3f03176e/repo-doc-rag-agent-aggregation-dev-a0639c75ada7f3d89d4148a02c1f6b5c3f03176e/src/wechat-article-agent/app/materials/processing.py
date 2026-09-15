from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from app.core.ids import prefixed_id
from app.materials.models import Material

_WEB_KINDS = {"factual", "resource", "creative_reference", "popular_culture"}
_CONSUMABLE_KINDS = {"user_document", "factual", "creative_reference", "popular_culture"}
_WEB_KIND_PRIORITY = ("factual", "creative_reference", "resource", "popular_culture")
_DROP_PRIORITY = ("popular_culture", "resource", "creative_reference", "factual", "user_document")


def normalize_material_library(
    items: list[dict[str, Any]] | None,
    *,
    documents: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Normalize old and new material snapshots without mutating stored values."""

    document_names = {
        str(item.get("doc_id")): str(item.get("doc_name") or item.get("file_name") or item.get("doc_id"))
        for item in documents or []
        if item.get("doc_id")
    }
    seen_material_ids: set[str] = set()
    seen_chunk_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw_item in items or []:
        if not isinstance(raw_item, dict):
            continue
        source = str(raw_item.get("source") or raw_item.get("source_doc_id") or "")
        kind = str(raw_item.get("material_kind") or "user_document")
        if kind not in _WEB_KINDS | {"user_document"}:
            kind = "user_document"
        metadata_value = raw_item.get("metadata")
        metadata: dict[str, Any] = dict(metadata_value) if isinstance(metadata_value, dict) else {}
        extraction_mode = str(metadata.get("extraction_mode") or "retrieve")
        if extraction_mode == "retrieve_fallback":
            extraction_mode = "retrieve"
        if extraction_mode not in {"raw", "retrieve", "web_search"}:
            extraction_mode = "web_search" if kind in _WEB_KINDS else "retrieve"
        material_id = _unique_id(str(raw_item.get("material_id") or ""), "mat", seen_material_ids)
        chunks: list[dict[str, Any]] = []
        for raw_chunk in raw_item.get("orig_chunks") or []:
            if not isinstance(raw_chunk, dict):
                continue
            chunk_meta_value = raw_chunk.get("chunk_meta")
            chunk_meta: dict[str, Any] = dict(chunk_meta_value) if isinstance(chunk_meta_value, dict) else {}
            page = raw_chunk.get("page_number", chunk_meta.get("page_number"))
            title = str(
                raw_chunk.get("title")
                or chunk_meta.get("doc_name")
                or document_names.get(source)
                or (raw_chunk.get("path") if kind in _WEB_KINDS else "")
                or source
            )
            ref = str(raw_chunk.get("ref") or (source if kind == "user_document" else ""))
            path = _display_path(
                raw_chunk.get("path"),
                title=title,
                page_number=page,
                material_kind=kind,
                ref=ref,
            )
            chunk_id = _unique_id(
                str(raw_chunk.get("chunk_id") or chunk_meta.get("chunk_id") or ""),
                "chunk",
                seen_chunk_ids,
            )
            chunk_type = str(raw_chunk.get("type") or ("web_search" if kind in _WEB_KINDS else "page"))
            if chunk_type not in {"full_text", "page", "web_search"}:
                chunk_type = "web_search" if kind in _WEB_KINDS else "page"
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "content": str(
                        raw_chunk.get("content") or raw_chunk.get("text") or chunk_meta.get("content") or ""
                    ),
                    "type": chunk_type,
                    "path": path,
                    "page_number": page if isinstance(page, (int, str)) else None,
                    "ref": ref,
                    "title": title,
                }
            )
        candidate = Material.model_validate(
            {
                "material_id": material_id,
                "material_kind": kind,
                "source": source or ("web_search" if kind in _WEB_KINDS else "unknown"),
                "summary": str(raw_item.get("summary") or ""),
                "active": bool(raw_item.get("active", True)),
                "metadata": {
                    "extraction_mode": extraction_mode,
                    "queries": [str(value) for value in metadata.get("queries") or [] if value],
                    "coverage_complete": bool(metadata.get("coverage_complete", False)),
                    "warnings": _clean_warnings(metadata.get("warnings")),
                },
                "orig_chunks": chunks,
            }
        )
        normalized.append(candidate.model_dump(mode="json"))
    return _deduplicate_exact_chunks(normalized)


def consumable_materials(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        item
        for item in items or []
        if item.get("active", True)
        and item.get("material_kind", "user_document") in _CONSUMABLE_KINDS
        and (item.get("summary") or item.get("orig_chunks"))
    ]


def material_source_names(items: list[dict[str, Any]] | None) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()
    for material in items or []:
        if not material.get("active", True):
            continue
        chunks = [chunk for chunk in material.get("orig_chunks") or [] if isinstance(chunk, dict)]
        if material.get("material_kind", "user_document") == "user_document":
            values = [str(chunks[0].get("title") or "").strip()] if chunks else []
        else:
            values = [
                str(chunk.get("ref") or "").strip()
                for chunk in chunks
                if _is_http_url(str(chunk.get("ref") or ""))
            ]
        for value in values:
            if value and value not in seen:
                seen.add(value)
                sources.append(value)
    return sources


def material_character_count(items: Iterable[dict[str, Any]]) -> int:
    return sum(
        len(str(chunk.get("content") or ""))
        for item in items
        for chunk in item.get("orig_chunks") or []
        if isinstance(chunk, dict)
    )


def web_search_materials(
    output: Any,
    *,
    annotations: list[dict[str, Any]],
    queries: list[str],
) -> list[dict[str, Any]]:
    """Build category materials using only URLs returned by this Ark call."""

    values = output.model_dump() if hasattr(output, "model_dump") else dict(output or {})
    annotation_by_url = {
        str(item.get("url") or "").strip(): item
        for item in annotations
        if isinstance(item, dict)
        and _is_http_url(str(item.get("url") or "").strip())
        and str(item.get("summary") or "").strip()
    }
    used_urls: set[str] = set()
    result: list[dict[str, Any]] = []
    for kind in _WEB_KIND_PRIORITY:
        category_value = values.get(kind)
        category: dict[str, Any] = dict(category_value) if isinstance(category_value, dict) else {}
        category_urls = list(
            dict.fromkeys(
                str(value).strip()
                for value in category.get("urls") or []
                if str(value).strip() in annotation_by_url
            )
        )
        urls = list(dict.fromkeys(value for value in category_urls if value not in used_urls))
        chunks: list[dict[str, Any]] = []
        for url in urls:
            used_urls.add(url)
            annotation = annotation_by_url[url]
            title = str(annotation.get("title") or url).strip()
            site_name = str(annotation.get("site_name") or "").strip()
            path = site_name or urlparse(url).netloc.removeprefix("www.") or title
            chunks.append(
                {
                    "chunk_id": prefixed_id("chunk"),
                    "content": str(annotation.get("summary") or "").strip(),
                    "type": "web_search",
                    "path": path,
                    "page_number": None,
                    "ref": url,
                    "title": title,
                }
            )
        summary = str(category.get("summary") or "").strip()
        # A model summary without a URL returned by this Ark call is not traceable
        # evidence and must not enter the material library.
        if kind in {"creative_reference", "popular_culture"}:
            if not summary or not category_urls:
                continue
            if not chunks:
                chunks.append(_summary_chunk(kind, summary))
        elif not chunks:
            continue
        result.append(
            {
                "material_id": prefixed_id("mat"),
                "material_kind": kind,
                "source": "web_search",
                "summary": summary,
                "active": True,
                "metadata": {
                    "extraction_mode": "web_search",
                    "queries": list(dict.fromkeys(query for query in queries if query)),
                    "coverage_complete": bool(chunks),
                    "warnings": [],
                },
                "orig_chunks": chunks,
            }
        )
    return result


def replace_reference_chunks_with_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Use one category summary as content while retaining every source reference."""

    if item.get("material_kind") not in {"creative_reference", "popular_culture"}:
        return item
    summary = str(item.get("summary") or "").strip()
    chunks = [dict(chunk) for chunk in item.get("orig_chunks") or [] if isinstance(chunk, dict)]
    if not summary or not chunks:
        return item
    for index, chunk in enumerate(chunks):
        chunk["content"] = summary if index == 0 else ""
    return {**item, "orig_chunks": chunks}


def estimated_material_tokens(items: list[dict[str, Any]]) -> int:
    serialized = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return max(math.ceil(len(serialized) / 4), math.ceil(len(serialized.encode("utf-8")) / 3))


def safe_material_token_budget(context_window: int) -> int:
    # Reserve output, system instructions and JSON/schema/tool overhead. The reserve
    # grows with the model window but never falls below 32K tokens.
    reserve = max(32_768, math.ceil(context_window * 0.30))
    return max(8_192, context_window - reserve)


def trim_materials_to_budget(
    items: list[dict[str, Any]], *, context_window: int
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    retained = list(items)
    warnings: list[dict[str, str]] = []
    budget = safe_material_token_budget(context_window)
    for kind in _DROP_PRIORITY:
        if estimated_material_tokens(retained) <= budget:
            break
        candidates = [item for item in retained if item.get("material_kind") == kind]
        for candidate in reversed(candidates):
            if estimated_material_tokens(retained) <= budget:
                break
            retained.remove(candidate)
            warnings.append(
                {
                    "code": "MATERIAL_DROPPED_FOR_CONTEXT_BUDGET",
                    "message": f"素材 {candidate.get('material_id')} 因上下文预算不足被裁剪。",
                }
            )
    return retained, warnings


def conflict_chunks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": str(chunk.get("chunk_id") or ""),
            "material_kind": str(item.get("material_kind") or "user_document"),
            "path": str(chunk.get("path") or ""),
            "ref": str(chunk.get("ref") or ""),
            "title": str(chunk.get("title") or ""),
            "content": str(chunk.get("content") or ""),
        }
        for item in items
        if item.get("active", True) and item.get("material_kind") in {"user_document", "factual"}
        for chunk in item.get("orig_chunks") or []
        if isinstance(chunk, dict) and chunk.get("content")
    ]


def apply_conflict_notes(items: list[dict[str, Any]], notes: dict[str, str]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in items:
        chunks: list[dict[str, Any]] = []
        for chunk in item.get("orig_chunks") or []:
            value = dict(chunk)
            note = notes.get(str(value.get("chunk_id") or ""), "").strip()
            if note:
                value["content"] = f"[冲突提示] {note}\n\n{value.get('content') or ''}"
            chunks.append(value)
        output.append({**item, "orig_chunks": chunks})
    return output


def _clean_warnings(value: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            output.append(
                {
                    "code": str(item.get("code") or "WARNING")[:100],
                    "message": str(item.get("message") or item.get("detail") or "")[:500],
                }
            )
        elif item:
            output.append({"code": "WARNING", "message": str(item)[:500]})
    return output


def _display_path(
    value: Any,
    *,
    title: str,
    page_number: Any,
    material_kind: str,
    ref: str,
) -> str:
    if material_kind in _WEB_KINDS:
        existing = " > ".join(str(part) for part in value) if isinstance(value, list) else str(value or "")
        if existing and not existing.startswith("document:"):
            return existing
        domain = urlparse(ref).netloc.removeprefix("www.") if _is_http_url(ref) else ""
        return title or domain or "网络来源"
    return f"{title}：第 {page_number} 页" if page_number not in {None, ""} else f"{title}：全文"


def _unique_id(value: str, prefix: str, seen: set[str]) -> str:
    candidate = value.strip()
    if not candidate or candidate in seen:
        candidate = prefixed_id(prefix)
    seen.add(candidate)
    return candidate


def _deduplicate_exact_chunks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    output: list[dict[str, Any]] = []
    for item in items:
        chunks: list[dict[str, Any]] = []
        for chunk in item.get("orig_chunks") or []:
            ref = str(chunk.get("ref") or "").strip()
            normalized_content = " ".join(str(chunk.get("content") or "").split())
            digest = hashlib.sha256(normalized_content.encode()).hexdigest() if normalized_content else ""
            key = (
                ("ref", ref) if ref and item.get("material_kind") != "user_document" else ("content", digest)
            )
            if key[1] and key in seen:
                continue
            if key[1]:
                seen.add(key)
            chunks.append(chunk)
        if (
            not chunks
            and item.get("material_kind") in {"creative_reference", "popular_culture"}
            and str(item.get("summary") or "").strip()
        ):
            chunks.append(_summary_chunk(str(item["material_kind"]), str(item["summary"])))
        if chunks or item.get("summary"):
            output.append({**item, "orig_chunks": chunks})
    return output


def _summary_chunk(kind: str, summary: str) -> dict[str, Any]:
    label = "网络创作范例总结" if kind == "creative_reference" else "网络流行文化总结"
    return {
        "chunk_id": prefixed_id("chunk"),
        "content": summary.strip(),
        "type": "web_search",
        "path": label,
        "page_number": None,
        "ref": "",
        "title": label,
    }


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
