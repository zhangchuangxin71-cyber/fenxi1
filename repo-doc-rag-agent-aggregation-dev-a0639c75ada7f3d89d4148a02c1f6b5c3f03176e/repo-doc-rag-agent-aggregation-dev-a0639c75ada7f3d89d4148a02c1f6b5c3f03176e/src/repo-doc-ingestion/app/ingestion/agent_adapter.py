from __future__ import annotations

"""Standalone ingestion adapter.

主要职责：
1) 调用统一多格式解析器；
2) 固定入库算法的输入/输出契约；
3) 收口索引默认参数来源：优先环境变量，其次使用内置兜底值。
"""

from dataclasses import dataclass
import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from app.parser.multiformat_parser import (
    is_supported_document,
    parse_document_to_structure,
)


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        logging.getLogger(__name__).warning("invalid %s=%r; fallback=%s", name, raw, default)
        return int(default)


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except Exception:
        logging.getLogger(__name__).warning("invalid %s=%r; fallback=%s", name, raw, default)
        return float(default)


def _env_int_chain(names: tuple[str, ...], default: int) -> int:
    for name in names:
        raw = str(os.getenv(name, "") or "").strip()
        if raw:
            return _env_int(name, default)
    return int(default)


def _normalize_doc_type(doc_type: str) -> str:
    normalized = str(doc_type or "").strip().lower().lstrip(".")
    return "docx" if normalized == "doc" else normalized


@dataclass(frozen=True)
class AgentIndexDefaults:
    """索引默认参数集合。"""

    simple_index_page_threshold: int
    long_pdf_hard_threshold: int
    simple_index_chunk_pages: int
    hybrid_index_chunk_pages: int
    complexity_sample_pages: int
    chunk_overlap_ratio: float
    index_concurrency: int


@dataclass(frozen=True)
class NormalizedIndexedDocument:
    """统一后的解析输出契约。"""

    doc_id: str | None
    page_count: int | None
    tree_node_count: int
    structure: list[dict[str, Any]]
    payload: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NormalizedIndexedDocument":
        data = dict(payload or {})
        doc_id = str(data.get("doc_id") or data.get("id") or "").strip() or None
        page_count_raw = data.get("page_count")
        try:
            page_count = int(page_count_raw) if page_count_raw is not None else None
        except Exception:
            page_count = None
        raw_structure = data.get("structure")
        structure = raw_structure if isinstance(raw_structure, list) else []
        if doc_id:
            data.setdefault("doc_id", doc_id)
            data.setdefault("id", doc_id)
        return cls(
            doc_id=doc_id,
            page_count=page_count,
            tree_node_count=len(structure),
            structure=structure,
            payload=data,
        )


class DocumentAssistantAdapter:
    """Standalone ingestion adapter for parsing and persistence."""

    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)
        self.defaults = self._resolve_defaults()

    def _resolve_defaults(self) -> AgentIndexDefaults:
        fallback = {
            "simple_index_page_threshold": 80,
            "long_pdf_hard_threshold": 180,
            "simple_index_chunk_pages": 10,
            "hybrid_index_chunk_pages": 6,
            "complexity_sample_pages": 12,
            "chunk_overlap_ratio": 0.15,
            "index_concurrency": max(1, min(8, os.cpu_count() or 4)),
        }
        return AgentIndexDefaults(
            simple_index_page_threshold=max(
                1, _env_int("INGEST_SIMPLE_INDEX_PAGE_THRESHOLD", fallback["simple_index_page_threshold"])
            ),
            long_pdf_hard_threshold=max(1, _env_int("INGEST_LONG_PDF_HARD_THRESHOLD", fallback["long_pdf_hard_threshold"])),
            simple_index_chunk_pages=max(1, _env_int("INGEST_SIMPLE_INDEX_CHUNK_PAGES", fallback["simple_index_chunk_pages"])),
            hybrid_index_chunk_pages=max(1, _env_int("INGEST_HYBRID_INDEX_CHUNK_PAGES", fallback["hybrid_index_chunk_pages"])),
            complexity_sample_pages=max(1, _env_int("INGEST_COMPLEXITY_SAMPLE_PAGES", fallback["complexity_sample_pages"])),
            chunk_overlap_ratio=max(0.1, min(0.5, _env_float("INGEST_DEFAULT_CHUNK_OVERLAP_RATIO", fallback["chunk_overlap_ratio"]))),
            index_concurrency=max(1, _env_int("INGEST_INDEX_CONCURRENCY", fallback["index_concurrency"])),
        )

    def build_indexing_options(
        self,
        *,
        resolved_overlap_ratio: float | None,
        summary_enabled: bool,
        table_parse_mode: str,
        node_max_tokens: int,
        max_tree_depth: int,
        enable_ocr: bool,
        summary_rate_limit_per_sec: float | None = None,
    ) -> Any:
        """构建统一索引参数对象。"""

        overlap = float(resolved_overlap_ratio) if resolved_overlap_ratio is not None else float(self.defaults.chunk_overlap_ratio)
        return StandaloneIndexingOptions(
            simple_index_page_threshold=self.defaults.simple_index_page_threshold,
            long_pdf_hard_threshold=self.defaults.long_pdf_hard_threshold,
            simple_index_chunk_pages=self.defaults.simple_index_chunk_pages,
            hybrid_index_chunk_pages=self.defaults.hybrid_index_chunk_pages,
            chunk_overlap_ratio=max(0.1, min(0.5, overlap)),
            long_pdf_mode="auto",
            complexity_sample_pages=self.defaults.complexity_sample_pages,
            index_concurrency=self.defaults.index_concurrency,
            summary_enabled=bool(summary_enabled),
            table_parse_mode=str(table_parse_mode),
            node_max_tokens=int(node_max_tokens),
            max_tree_depth=int(max_tree_depth),
            enable_ocr=bool(enable_ocr),
            summary_concurrency=max(1, _env_int("INGEST_SUMMARY_CONCURRENCY", _env_int("SUMMARY_CONCURRENCY", 32))),
            summary_rate_limit_per_sec=max(
                0.0,
                float(summary_rate_limit_per_sec)
                if summary_rate_limit_per_sec is not None
                else _env_float(
                    "INGEST_SUMMARY_RATE_LIMIT_PER_SEC",
                    _env_float("ARK_SUMMARY_RATE_LIMIT_PER_SEC", 1.0),
                ),
            ),
        )

    async def compute_file_fingerprint(self, file_path: Path) -> dict[str, Any]:
        return await asyncio.to_thread(self._compute_file_fingerprint_sync, file_path)

    async def find_cached_doc_id(self, client: Any, file_path: Path, fingerprint: dict[str, Any] | None) -> str | None:
        return await asyncio.to_thread(self._find_cached_doc_id_sync, client, fingerprint)

    @staticmethod
    def apply_source_metadata(payload: dict[str, Any], source_metadata: dict[str, Any] | None) -> dict[str, Any]:
        """Preserve original upload metadata after internal format conversion."""
        data = dict(payload or {})
        meta = source_metadata or {}
        doc_name = str(meta.get("doc_name") or "").strip()
        doc_type = _normalize_doc_type(str(meta.get("doc_type") or meta.get("type") or ""))
        source_path = str(meta.get("path") or meta.get("source_path") or "").strip()
        file_oss_key = str(meta.get("file_oss_key") or meta.get("oss_key") or "").strip()
        if doc_name:
            data["doc_name"] = doc_name
        if doc_type:
            data["type"] = doc_type
        if source_path:
            data["path"] = source_path
        if file_oss_key:
            data["file_oss_key"] = file_oss_key
        return data

    async def load_or_index_document(
        self,
        *,
        model: str,
        retrieve_model: str,
        file_path: Path,
        options: Any,
        runtime: Any,
        fingerprint: dict[str, Any] | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> NormalizedIndexedDocument | None:
        payload = await asyncio.to_thread(
            parse_document_to_structure,
            file_path,
            runtime_overrides={
                "summary_concurrency": max(1, int(getattr(options, "summary_concurrency", 32) or 32)),
                "summary_rate_limit_per_sec": max(
                    0.0,
                    float(getattr(options, "summary_rate_limit_per_sec", 1.0) or 0.0),
                ),
                "pdf_vision_concurrency": max(
                    1,
                    _env_int_chain(
                        (
                            "INGEST_PDF_VISION_CONCURRENCY",
                            "DA_PDF_VISION_CONCURRENCY",
                            "PDF_VISION_CONCURRENCY",
                            "ARK_VISION_CONCURRENCY",
                        ),
                        32,
                    ),
                ),
            },
            pageindex_overrides={
                "model": model,
                "if_add_node_id": True,
                "if_add_node_summary": bool(getattr(options, "summary_enabled", True)),
                "if_add_doc_description": True,
                "if_add_node_text": True,
            },
            generate_summary=bool(getattr(options, "summary_enabled", True)),
            generate_doc_description=True,
        )
        payload = self.apply_source_metadata(dict(payload), source_metadata)
        if fingerprint:
            payload.update(fingerprint)
            payload.setdefault("content_hash", fingerprint.get("file_md5") or fingerprint.get("file_head_md5"))
        return NormalizedIndexedDocument.from_payload(dict(payload))

    async def index_documents_async(
        self,
        client: Any,
        file_paths: list[Path],
        *,
        target_doc_ids: list[str] | None = None,
        source_metadata: list[dict[str, Any]] | None = None,
        options: Any,
        runtime: Any,
    ) -> tuple[list[str], list[dict]]:
        target_doc_ids = [str(item) for item in (target_doc_ids or [])]
        concurrency = max(1, int(getattr(options, "index_concurrency", 1) or 1))
        semaphore = asyncio.Semaphore(concurrency)
        ordered_doc_ids: list[str | None] = [None] * len(file_paths)
        failed_docs_by_index: list[tuple[int, dict[str, Any]]] = []

        async def index_one(index: int, file_path: Path) -> None:
            async with semaphore:
                try:
                    indexed = await self.load_or_index_document(
                        model=getattr(client, "model", "") or "",
                        retrieve_model=getattr(client, "retrieve_model", "") or "",
                        file_path=file_path,
                        options=options,
                        runtime=runtime,
                        source_metadata=(source_metadata[index] if source_metadata and index < len(source_metadata) else None),
                    )
                    if indexed is None:
                        raise RuntimeError("parser returned empty payload")
                    payload = dict(indexed.payload)
                    if index < len(target_doc_ids) and target_doc_ids[index]:
                        payload["id"] = target_doc_ids[index]
                        payload["doc_id"] = target_doc_ids[index]
                    ordered_doc_ids[index] = self.persist_indexed_document(client, payload)
                except Exception as exc:
                    failed_docs_by_index.append((index, {"file_path": str(file_path), "error": str(exc)}))

        tasks = [asyncio.create_task(index_one(index, file_path)) for index, file_path in enumerate(file_paths)]
        if tasks:
            await asyncio.gather(*tasks)

        doc_ids = [doc_id for doc_id in ordered_doc_ids if doc_id]
        failed_docs = [item for _, item in sorted(failed_docs_by_index, key=lambda pair: pair[0])]
        return doc_ids, failed_docs

    def persist_indexed_document(self, client: Any, payload: dict[str, Any]) -> str:
        doc_id = str(payload.get("doc_id") or payload.get("id") or "").strip()
        if not doc_id:
            raise ValueError("payload missing doc_id/id")
        payload = dict(payload)
        payload["id"] = doc_id
        payload["doc_id"] = doc_id
        client.documents[doc_id] = payload
        client._save_doc(doc_id)
        return doc_id

    def get_pdf_page_count(self, file_path: Path) -> int:
        return self.get_document_page_count(file_path)

    def get_docx_page_count(self, file_path: Path) -> int:
        return self.get_document_page_count(file_path)

    def get_document_page_count(self, file_path: Path) -> int:
        """Use the unified multi-format parser to estimate logical page count."""
        if not is_supported_document(file_path):
            return 1
        payload = parse_document_to_structure(
            file_path,
            generate_summary=False,
            generate_doc_description=False,
        )
        page_count = payload.get("page_count")
        if isinstance(page_count, int) and page_count > 0:
            return int(page_count)
        pages = payload.get("pages")
        if isinstance(pages, list) and pages:
            return len(pages)
        return 1

    @staticmethod
    def _compute_file_fingerprint_sync(file_path: Path) -> dict[str, Any]:
        path = Path(file_path)
        stat = path.stat()
        hasher = hashlib.md5()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                hasher.update(chunk)
        return {
            "file_size": int(stat.st_size),
            "file_mtime": float(stat.st_mtime),
            "file_md5": hasher.hexdigest(),
        }

    @staticmethod
    def _find_cached_doc_id_sync(client: Any, fingerprint: dict[str, Any] | None) -> str | None:
        if not fingerprint:
            return None
        wanted = str(fingerprint.get("file_md5") or "").strip()
        if not wanted:
            return None
        for doc_id, doc in getattr(client, "documents", {}).items():
            if str(doc.get("file_md5") or "").strip() == wanted:
                return str(doc_id)
        return None


@dataclass(frozen=True)
class StandaloneIndexingOptions:
    simple_index_page_threshold: int
    long_pdf_hard_threshold: int
    simple_index_chunk_pages: int
    hybrid_index_chunk_pages: int
    chunk_overlap_ratio: float
    long_pdf_mode: str
    complexity_sample_pages: int
    index_concurrency: int
    summary_enabled: bool
    table_parse_mode: str
    node_max_tokens: int
    max_tree_depth: int
    enable_ocr: bool
    summary_concurrency: int = 32
    summary_rate_limit_per_sec: float = 1.0
