"""Reusable multi-format document parser for the assistant service.

This module wraps the migrated ``conversion_core`` package and exposes a
single service-friendly entry point:

    parse_document_to_structure(...)

The returned payload matches the JSON shape consumed by PageIndex retrieval:
``id/type/path/doc_name/doc_description/page_count/structure/pages``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional

from pydantic import ValidationError


SERVICE_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _load_service_env(env_path: Path = SERVICE_ENV_PATH) -> None:
    """Load document-assistant-service/.env before conversion_core reads env vars."""
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=True)
        return
    except Exception:
        pass

    # Minimal fallback for environments without python-dotenv.
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


_load_service_env()

LOGGER = logging.getLogger(__name__)

from .conversion_core.config import (
    PageIndexConfig,
    RuntimeConfig,
    coerce_bool,
)
from .conversion_core.exceptions import ConfigError
from .conversion_core.formatter import (
    strip_internal_fields,
    to_legacy_output,
    validate_output_structure,
)
from .conversion_core.parser import DocumentParser
from .conversion_core.summarizer import build_doubao_tree_summarizer
from .mineru_adapter import SUPPORTED_MINERU_DOC_TYPES, parse_document_with_mineru
from .pageindex_compat.page_index_md import md_to_tree

SUPPORTED_DOCUMENT_SUFFIXES = {
    ".pdf",
    ".docx",
    ".md",
    ".markdown",
    ".xlsx",
    ".txt",
    ".pptx",
}


def _count_structure_nodes(nodes: Any) -> int:
    if not isinstance(nodes, list):
        return 0
    count = 0
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        count += 1
        children = node.get("nodes")
        if isinstance(children, list):
            stack.extend(children)
    return count


def is_supported_document(path: str | os.PathLike[str]) -> bool:
    """Return whether ``path`` can be parsed by the multi-format parser."""
    return Path(path).suffix.lower() in SUPPORTED_DOCUMENT_SUFFIXES


def stable_document_id(file_path: str | os.PathLike[str]) -> str:
    """Build a deterministic document id from the file content."""
    path = Path(file_path)
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sha256:{sha256.hexdigest()}"))


def _is_toc_like_title(title: str) -> bool:
    text = str(title or "").strip()
    if not text:
        return True
    if re.search(r"(?:[.\u2026·•。．\s]{3,}|\t+)\s*\d{1,4}\s*$", text):
        return True
    if re.search(r"^\s*[IVX]+\s*目录\s*$", text, flags=re.IGNORECASE):
        return True
    return text.lower() in {"目录", "contents"}


def _collect_titles(nodes: Any, out: List[str], limit: int = 12) -> None:
    if not isinstance(nodes, list) or len(out) >= limit:
        return
    for node in nodes:
        if len(out) >= limit:
            return
        if not isinstance(node, dict):
            continue
        title = str(node.get("title") or "").strip()
        if title and not _is_toc_like_title(title) and title not in out:
            out.append(title)
        _collect_titles(node.get("nodes"), out, limit=limit)


def _build_doc_description_fallback(payload: Mapping[str, Any]) -> str:
    """Build a lightweight description when LLM description is empty."""
    doc_name = str(payload.get("doc_name") or "document")
    doc_type = str(payload.get("type") or "").strip().lower() or "文档"
    page_count = payload.get("page_count")

    titles: List[str] = []
    _collect_titles(payload.get("structure"), titles, limit=12)
    if titles:
        major = "、".join(titles[:8])
        if isinstance(page_count, int) and page_count > 0:
            return f"《{doc_name}》为{doc_type}，共{page_count}页，主要内容包括：{major}。"
        return f"《{doc_name}》主要内容包括：{major}。"

    snippets: List[str] = []
    structure = payload.get("structure")
    if isinstance(structure, list):
        for node in structure:
            if not isinstance(node, dict):
                continue
            summary = str(node.get("summary") or "").strip()
            if summary:
                snippets.append(summary)
            if len(snippets) >= 3:
                break
    if snippets:
        return " ".join(snippets)[:480]

    if isinstance(page_count, int) and page_count > 0:
        return f"{doc_name}，共{page_count}页。"
    return f"{doc_name} 文档树索引。"


def _renumber_structure_nodes(nodes: Any, start: int = 0) -> int:
    """Renumber node ids in preorder to the PageIndex-friendly 0000 style."""
    if not isinstance(nodes, list):
        return start
    current = int(start)
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node["node_id"] = f"{current:04d}"
        current += 1
        current = _renumber_structure_nodes(node.get("nodes"), current)
    return current


def _iter_tree_nodes(nodes: Any):
    """Yield tree nodes in preorder."""
    if not isinstance(nodes, list):
        return
    stack = [node for node in reversed(nodes) if isinstance(node, dict)]
    while stack:
        node = stack.pop()
        yield node
        children = node.get("nodes")
        if isinstance(children, list):
            stack.extend(child for child in reversed(children) if isinstance(child, dict))


def _normalize_legacy_payload(payload: Dict[str, Any], file_path: str) -> Dict[str, Any]:
    """Normalize conversion output to the shape stored by the service."""
    path_obj = Path(file_path)
    payload = dict(payload)
    # The legacy formatter may use the file stem as id; force a stable UUID
    # so PostgreSQL primary keys and cached document references stay robust.
    payload["id"] = stable_document_id(path_obj)
    payload["type"] = str(payload.get("type") or path_obj.suffix.lower().lstrip("."))
    payload["path"] = str(payload.get("path") or path_obj.resolve())
    payload["doc_name"] = str(payload.get("doc_name") or path_obj.name)

    structure = payload.get("structure")
    if isinstance(structure, list):
        _renumber_structure_nodes(structure, start=0)
    else:
        structure = []

    pages = payload.get("pages")
    if not isinstance(pages, list):
        pages = []

    page_count = payload.get("page_count")
    if not isinstance(page_count, int):
        page_count = len(pages)

    normalized = {
        "id": payload["id"],
        "type": payload["type"],
        "path": payload["path"],
        "doc_name": payload["doc_name"],
        "doc_description": str(payload.get("doc_description") or "").strip(),
        "page_count": page_count,
        "structure": structure,
        "pages": pages,
    }
    if not normalized["doc_description"]:
        normalized["doc_description"] = _build_doc_description_fallback(normalized)
    return normalized


def _build_runtime_config(overrides: Optional[Mapping[str, Any]] = None) -> RuntimeConfig:
    data = dict(overrides or {})
    try:
        return RuntimeConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid runtime configuration: {exc}", stage="config_validate", cause=exc) from exc


def _build_pageindex_config(overrides: Optional[Mapping[str, Any]] = None) -> PageIndexConfig:
    data = dict(overrides or {})
    try:
        return PageIndexConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid pageindex configuration: {exc}", stage="config_validate", cause=exc) from exc


async def _build_markdown(
    *,
    file_path: str,
    parser: DocumentParser,
    runtime_cfg: RuntimeConfig,
    opt: Any,
) -> Dict[str, Any]:
    result = await md_to_tree(
        md_path=file_path,
        if_thinning=runtime_cfg.if_thinning,
        min_token_threshold=runtime_cfg.thinning_threshold,
        if_add_node_summary="no",
        summary_token_threshold=runtime_cfg.summary_token_threshold,
        model=opt.model,
        if_add_doc_description="no",
        if_add_node_text="yes",
        if_add_node_id=opt.if_add_node_id,
    )
    enriched = parser.enrich_with_metadata(result, file_path, "md")
    md_nodes = enriched.get("structure")
    if isinstance(md_nodes, list):
        # The markdown compatibility parser returns PageIndex-style nodes under
        # `structure`; the shared summarizer expects `nodes`. Keep both names
        # pointing at the same list so summaries are written back to the final
        # legacy output.
        enriched["nodes"] = md_nodes
    for node in _iter_tree_nodes(enriched.get("nodes")):
        text = str(node.get("text") or "").strip()
        if text and not node.get("_summary_source"):
            node["_summary_source"] = text[: max(runtime_cfg.summary_input_chars * 2, 8000)]
    return enriched


async def parse_document_to_structure_async(
    file_path: str | os.PathLike[str],
    *,
    runtime_overrides: Optional[Mapping[str, Any]] = None,
    pageindex_overrides: Optional[Mapping[str, Any]] = None,
    output_schema: str = "legacy",
    generate_summary: bool = True,
    generate_doc_description: bool = True,
) -> Dict[str, Any]:
    """Parse one document and return normalized PageIndex-style JSON.

    Args:
        file_path: Source document path.
        runtime_overrides: Optional ``RuntimeConfig`` overrides.
        pageindex_overrides: Optional ``PageIndexConfig`` overrides.
        output_schema: Use ``legacy`` for the service JSON shape.
        generate_summary: Whether to generate node summaries.
        generate_doc_description: Whether to generate document description.
    """
    source_path = Path(file_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"File not found: {source_path}")
    if output_schema not in {"legacy", "modern"}:
        raise ValueError("output_schema must be one of: legacy, modern")
    runtime_cfg = _build_runtime_config(runtime_overrides)
    if runtime_cfg.parser_backend == "mineru":
        if source_path.suffix.lower() not in SUPPORTED_MINERU_DOC_TYPES:
            raise ValueError(f"Unsupported file format for MinerU backend: {source_path.suffix}")
    elif not is_supported_document(source_path):
        raise ValueError(f"Unsupported file format: {source_path.suffix}")
    pageindex_cfg = _build_pageindex_config(pageindex_overrides)
    pageindex_payload = {
        "model": os.getenv("ARK_MODEL") or os.getenv("OPENAI_MODEL") or None,
        "toc_check_page_num": 3,
        "max_page_num_each_node": 10,
        "max_token_num_each_node": 512,
        "if_add_node_id": "yes",
        "if_add_node_summary": "yes",
        "if_add_doc_description": "yes",
        "if_add_node_text": "yes",
        **pageindex_cfg.to_config_loader_payload(),
    }
    opt = SimpleNamespace(**pageindex_payload)

    add_node_id = coerce_bool(getattr(opt, "if_add_node_id", None), default=False)
    add_node_text = coerce_bool(getattr(opt, "if_add_node_text", None), default=False)
    add_doc_description = coerce_bool(getattr(opt, "if_add_doc_description", None), default=False)

    parser = DocumentParser(
        opt=opt,
        add_node_id=add_node_id,
        add_node_text=add_node_text,
        add_doc_description=add_doc_description,
        max_file_size_mb=runtime_cfg.max_file_size_mb,
        txt_lines_per_node=runtime_cfg.txt_lines_per_node,
        txt_chars_per_node=runtime_cfg.txt_chars_per_node,
        max_xlsx_rows_per_sheet=(
            runtime_cfg.max_xlsx_rows_per_sheet if runtime_cfg.max_xlsx_rows_per_sheet > 0 else None
        ),
        xlsx_batch_size=runtime_cfg.xlsx_batch_size,
        summary_input_chars=runtime_cfg.summary_input_chars,
        pdf_max_depth=runtime_cfg.pdf_max_depth,
        pdf_parser=runtime_cfg.pdf_parser,
        pdf_vision_model=runtime_cfg.pdf_vision_model,
        pdf_vision_concurrency=runtime_cfg.pdf_vision_concurrency,
        pdf_table_mode=runtime_cfg.pdf_table_mode,
    )

    ext = source_path.suffix.lower()
    processed: Dict[str, Any] | None = None
    raw_mineru_repair: dict[str, Any] | None = None
    if runtime_cfg.parser_backend == "mineru":
        LOGGER.info(
            "MinerU parser start: source=%s ext=%s backend=%s parse_method=%s",
            source_path,
            ext,
            runtime_cfg.mineru_backend,
            runtime_cfg.mineru_parse_method,
        )
        try:
            processed = await asyncio.to_thread(
                parse_document_with_mineru,
                source_path,
                api_url=runtime_cfg.mineru_api_url,
                timeout_seconds=runtime_cfg.mineru_timeout_seconds,
                retry_times=runtime_cfg.mineru_retry_times,
                retry_backoff_base=runtime_cfg.mineru_retry_backoff_base,
                poll_interval_seconds=runtime_cfg.mineru_poll_interval_seconds,
                backend=runtime_cfg.mineru_backend,
                parse_method=runtime_cfg.mineru_parse_method,
                lang=runtime_cfg.mineru_lang,
                use_async_tasks=runtime_cfg.mineru_use_async_tasks,
                client_concurrency=runtime_cfg.mineru_client_concurrency,
                weak_heading_split_enabled=runtime_cfg.weak_heading_split_enabled,
                weak_heading_split_min_chars=runtime_cfg.weak_heading_split_min_chars,
                weak_heading_split_max_chars=runtime_cfg.weak_heading_split_max_chars,
            )
            LOGGER.info(
                "MinerU parser succeeded: source=%s ext=%s doc_type=%s page_count=%s node_count=%s",
                source_path,
                ext,
                processed.get("doc_type") or processed.get("type"),
                processed.get("page_count") or len(processed.get("pages") or []),
                processed.get("node_count") or _count_structure_nodes(processed.get("structure")),
            )
        except Exception as exc:
            if runtime_cfg.mineru_fallback_to_native and is_supported_document(source_path):
                now = datetime.now(timezone.utc)
                raw_mineru_repair = {
                    "status": "failed",
                    "attempt_count": 1,
                    "retryable": True,
                    "error_code": "RAW_MINERU_INITIAL_PARSE_FAILED",
                    "error_message": str(exc),
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "lease_expires_at": None,
                    "next_retry_at": (now + timedelta(seconds=60)).isoformat(),
                }
                LOGGER.warning(
                    "MinerU parser failed, falling back to native parser: source=%s ext=%s "
                    "error_type=%s stage=%s error=%s",
                    source_path,
                    ext,
                    type(exc).__name__,
                    getattr(exc, "stage", None),
                    exc,
                    exc_info=True,
                )
                processed = None
            else:
                LOGGER.exception(
                    "MinerU parser failed without fallback: source=%s ext=%s error_type=%s stage=%s",
                    source_path,
                    ext,
                    type(exc).__name__,
                    getattr(exc, "stage", None),
                )
                raise

    if processed is None:
        if ext in {".md", ".markdown"}:
            processed = await _build_markdown(
                file_path=str(source_path),
                parser=parser,
                runtime_cfg=runtime_cfg,
                opt=opt,
            )
        else:
            processed = await parser.process(str(source_path))
    else:
        processed.setdefault("doc_type", processed.get("type") or ext.lstrip("."))
        processed.setdefault("file_path", processed.get("path") or str(source_path))
        if isinstance(processed.get("structure"), list) and not isinstance(processed.get("nodes"), list):
            processed["nodes"] = processed["structure"]

    nodes = processed.get("nodes")
    if generate_summary and isinstance(nodes, list):
        summarizer = build_doubao_tree_summarizer(
            summary_input_chars=runtime_cfg.summary_input_chars,
            summary_max_chars=runtime_cfg.summary_max_chars,
            summary_concurrency=runtime_cfg.summary_concurrency,
            min_summary_text_chars=runtime_cfg.min_summary_text_chars,
            summary_rate_limit_per_sec=runtime_cfg.summary_rate_limit_per_sec,
            summary_timeout_seconds=runtime_cfg.summary_timeout_seconds,
            summary_retry_times=runtime_cfg.summary_retry_times,
            summary_retry_backoff_base=runtime_cfg.summary_retry_backoff_base,
            summary_prompt_template=runtime_cfg.summary_prompt_template,
            summary_model=runtime_cfg.summary_model,
        )
        await summarizer.generate_summaries_async(nodes)
        if generate_doc_description and hasattr(summarizer, "generate_document_description_async"):
            try:
                doc_description = await summarizer.generate_document_description_async(processed)
                if doc_description:
                    processed["doc_description"] = doc_description
            except Exception as exc:
                logging.warning("Document description generation failed, ignored: %s", exc)
        summarizer.ensure_summary_for_nodes(nodes)
    raw_mineru = processed.get("raw_mineru")
    raw_mineru = dict(raw_mineru) if isinstance(raw_mineru, Mapping) else None

    processed = strip_internal_fields(processed)
    processed = validate_output_structure(processed)
    if output_schema == "legacy":
        processed = to_legacy_output(processed, str(source_path))

    normalized = _normalize_legacy_payload(processed, str(source_path))
    if raw_mineru is not None:
        normalized["raw_mineru"] = raw_mineru
    if raw_mineru_repair is not None:
        normalized["raw_mineru_repair"] = raw_mineru_repair
    return normalized


def parse_document_to_structure(
    file_path: str | os.PathLike[str],
    *,
    runtime_overrides: Optional[Mapping[str, Any]] = None,
    pageindex_overrides: Optional[Mapping[str, Any]] = None,
    output_schema: str = "legacy",
    generate_summary: bool = True,
    generate_doc_description: bool = True,
) -> Dict[str, Any]:
    """Synchronous wrapper around ``parse_document_to_structure_async``."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            parse_document_to_structure_async(
                file_path,
                runtime_overrides=runtime_overrides,
                pageindex_overrides=pageindex_overrides,
                output_schema=output_schema,
                generate_summary=generate_summary,
                generate_doc_description=generate_doc_description,
            )
        )

    raise RuntimeError(
        "parse_document_to_structure() cannot be called inside a running event loop; "
        "use parse_document_to_structure_async() instead."
    )


async def index_document_into_client_async(
    client: Any,
    file_path: str | os.PathLike[str],
    *,
    runtime_overrides: Optional[Mapping[str, Any]] = None,
    pageindex_overrides: Optional[Mapping[str, Any]] = None,
    generate_summary: bool = True,
    generate_doc_description: bool = True,
) -> str:
    """Parse one document, place it in ``client.documents``, and persist it."""
    payload = await parse_document_to_structure_async(
        file_path,
        runtime_overrides=runtime_overrides,
        pageindex_overrides=pageindex_overrides,
        output_schema="legacy",
        generate_summary=generate_summary,
        generate_doc_description=generate_doc_description,
    )
    doc_id = str(payload["id"])
    client.documents[doc_id] = payload
    save_doc = getattr(client, "_save_doc", None)
    if callable(save_doc):
        save_doc(doc_id)
    else:
        logging.warning("PageIndex client has no _save_doc(doc_id); document stored in memory only: %s", doc_id)
    return doc_id
