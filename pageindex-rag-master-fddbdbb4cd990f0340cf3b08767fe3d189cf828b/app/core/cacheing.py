# 模块说明：缓存签名、缓存键与 BM25 缓存复用逻辑。
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from assistant_components import BM25Prefilter
from utils.runtime import get_client_runtime

INDEX_CACHE_KEY_VERSION = "v1"
INDEX_CACHE_OPTION_KEYS = {
    "simple_index_page_threshold",
    "long_pdf_hard_threshold",
    "simple_index_chunk_pages",
    "hybrid_index_chunk_pages",
    "chunk_overlap_ratio",
    "long_pdf_mode",
    "complexity_sample_pages",
    "summary_enabled",
    "table_parse_mode",
    "node_max_tokens",
    "max_tree_depth",
    "enable_ocr",
    "ocr_min_chars",
    "ocr_lang",
    "summary_concurrency",
}


# 对文档目录做稳定签名，用于判断缓存是否可复用。
def compute_catalog_signature(catalog: list[dict]) -> str:
    """为当前文档目录生成稳定签名。

    签名用于识别“可影响检索结果”的目录变化，
    例如新增/删除文档、描述变化、页数变化等。
    """
    items = []
    for item in sorted(catalog, key=lambda x: str(x.get("doc_id", ""))):
        items.append(
            {
                "doc_id": item.get("doc_id", ""),
                "doc_name": item.get("doc_name", ""),
                "path": item.get("path", ""),
                "index_mode": item.get("index_mode", ""),
                "page_count": item.get("page_count"),
                "line_count": item.get("line_count"),
                "doc_description": item.get("doc_description", ""),
            }
        )
    raw = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()


# 复用或重建 BM25 预筛器（按目录签名缓存）。
def get_or_build_bm25_prefilter(client, catalog: list[dict]) -> BM25Prefilter:
    """复用或重建 BM25 预筛器。

    仅当目录签名变化时才重建，避免每轮问答重复构建索引。
    """
    runtime = get_client_runtime(client)
    signature = compute_catalog_signature(catalog)
    cached = runtime.bm25_prefilter_cache
    if cached is not None and cached[0] == signature:
        return cached[1]
    prefilter = BM25Prefilter(catalog)
    runtime.bm25_prefilter_cache = (signature, prefilter)
    return prefilter


# 构建 QA 结果缓存键，覆盖问题、模式、文档范围与模型等要素。
def build_qa_cache_key(
    *,
    question: str,
    qa_mode: str,
    top_k_docs: int,
    allowed_doc_ids: set[str] | None,
    bm25_prefilter_top_k: int,
    retrieve_model: str,
    catalog_signature: str,
    session_scope: str = "",
    context_scope: str = "",
) -> str:
    """构建 QA 结果缓存键。

    缓存键覆盖问题文本、模式、候选范围、模型、会话/上下文范围等维度，
    目的是避免“同问异环境”误命中。
    """
    payload = {
        "question": normalize_question_for_cache(question),
        "qa_mode": qa_mode,
        "top_k_docs": int(top_k_docs),
        "allowed_doc_ids": sorted(list(allowed_doc_ids)) if allowed_doc_ids is not None else None,
        "bm25_prefilter_top_k": int(bm25_prefilter_top_k),
        "retrieve_model": retrieve_model or "",
        "catalog_signature": catalog_signature,
        "session_scope": session_scope or "",
        "context_scope": context_scope or "",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()


def build_index_cache_key(
    *,
    file_path: str | Path,
    fingerprint: dict[str, Any] | None,
    options: Any,
    model: str,
    retrieve_model: str,
) -> str:
    """构建单文件索引结果缓存键。

    键由文件绝对路径、文件指纹、索引参数、模型版本共同决定，
    可精确识别“同文件不同配置”场景。
    """
    opt_payload: dict[str, Any] = {}
    for key in sorted(INDEX_CACHE_OPTION_KEYS):
        if hasattr(options, key):
            opt_payload[key] = getattr(options, key)

    file_abs = str(Path(file_path).expanduser().resolve())
    payload = {
        "version": INDEX_CACHE_KEY_VERSION,
        "file_path": file_abs,
        "fingerprint": {
            "file_size": int((fingerprint or {}).get("file_size", 0) or 0),
            "file_mtime": int((fingerprint or {}).get("file_mtime", 0) or 0),
            "file_head_md5": str((fingerprint or {}).get("file_head_md5", "") or ""),
        },
        "file_ext": Path(file_abs).suffix.lower(),
        "model": str(model or ""),
        "retrieve_model": str(retrieve_model or ""),
        "options": opt_payload,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()


def normalize_question_for_cache(question: str) -> str:
    """归一化问题文本以提高缓存命中率。

    处理策略：
    - NFKC 归一化与小写化；
    - 合并常见口语同义问法（如“如何/怎样/咋样”）；
    - 清理尾部语气词与多余标点。
    """
    raw = unicodedata.normalize("NFKC", str(question or "")).strip().lower()
    if not raw:
        return ""

    normalized = raw
    replacements = {
        "咋样": "怎么样",
        "怎样": "怎么样",
        "如何": "怎么样",
        "咋么样": "怎么样",
        "怎麽样": "怎么样",
        "如何看待": "怎么看",
    }
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)

    normalized = re.sub(r"\s+", "", normalized)
    normalized = re.sub(r"[？?！!。,.，、；;:：~～]+$", "", normalized)
    normalized = re.sub(r"(啊|呀|吧|呢|嘛|么)+$", "", normalized)
    return normalized or raw


