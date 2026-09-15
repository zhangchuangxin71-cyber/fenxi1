"""文档助手主入口，负责索引、缓存、检索和问答调度。"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
import uuid
import hashlib
from pathlib import Path
from typing import Any
from pydantic import ValidationError

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover - fallback path
    fitz = None

try:
    import PyPDF2
except Exception:  # pragma: no cover - optional dependency
    PyPDF2 = None

try:
    from docx import Document as DocxDocument
except Exception:  # pragma: no cover - optional dependency
    DocxDocument = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.env_bootstrap import bootstrap_runtime_env

bootstrap_runtime_env(Path(__file__).resolve().parent.parent)

from pageindex import PageIndexClient
import assistant_components as assistant_components_module
from assistant_components import BM25Prefilter, IndexingOptions
from cli.output import format_doc_names_for_log
from core.cacheing import (
    build_qa_cache_key,
    compute_catalog_signature,
    get_or_build_bm25_prefilter,
)
from core.cli_config import (
    apply_env_overrides,
    apply_file_config,
    build_argument_parser,
    build_indexing_options,
    build_prompt_set_from_args,
    cli_provided_options,
    load_runtime_config,
)
from core.common import (
    PathSecurityError,
    UserInputError,
    ensure_secure_source_file,
    first_existing_path,
    log_event,
    log_runtime_metrics,
    scan_documents,
    setup_logging,
)
from core.document_tools import (
    list_documents_tool,
    prune_untrusted_cached_documents,
    resolve_restricted_doc_ids,
)
from core.llm_ops import (
    DEFAULT_NO_INFO_ANSWER,
    GENERAL_KNOWLEDGE_SOURCE_LABEL,
    async_llm_completion,
    generate_general_knowledge_answer,
    is_no_info_answer,
)
from models.schemas import (
    DocumentInfo,
    build_chunk_node,
)
from prompts.defaults import (
    DEFAULT_AGENT_SYSTEM_PROMPT,
    DEFAULT_ANSWER_PROMPT,
    DEFAULT_DOC_SELECTION_PROMPT,
    DEFAULT_PAGE_SELECTION_PROMPT,
)
from qa.session import run_qa_mode
from utils.runtime import (
    AssistantRuntime,
    get_client_runtime,
)
try:
    from agents import set_tracing_disabled
    from qa.agent_mode import query_agent_multi_doc
    from qa.langgraph_adapter import (
        langgraph_available,
        run_langgraph_agent_qa,
    )
    _AGENT_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - dependency-gated path
    _AGENT_IMPORT_ERROR = exc

    def set_tracing_disabled(*args, **kwargs):
        return None

    def _raise_agent_dependency_error():
        raise RuntimeError(
            "The multi-document agent flow requires the `openai-agents` package "
            "and its runtime dependencies. Install requirements.txt before running QA."
        ) from _AGENT_IMPORT_ERROR

    async def query_agent_multi_doc(*args, **kwargs):
        _raise_agent_dependency_error()

    def langgraph_available():
        return False

    async def run_langgraph_agent_qa(*args, **kwargs):
        _raise_agent_dependency_error()

try:
    import jieba
except Exception:  # pragma: no cover - optional dependency
    jieba = None
else:
    # 抑制 jieba 初始化时的冗长输出，避免干扰用户侧流程日志。
    try:
        jieba.setLogLevel(logging.WARNING)
    except Exception:
        pass



BASE_DIR = Path(__file__).resolve().parent


DEFAULT_DATA_DIR = first_existing_path(BASE_DIR / "data")
MAX_INDEX_ATTEMPTS = 2
DEFAULT_SIMPLE_INDEX_PAGE_THRESHOLD = 80
DEFAULT_SIMPLE_INDEX_CHUNK_PAGES = 10
DEFAULT_LONG_PDF_HARD_THRESHOLD = 180
DEFAULT_HYBRID_INDEX_CHUNK_PAGES = 6
DEFAULT_INDEX_CONCURRENCY = min(4, max(1, (os.cpu_count() or 2)))
DEFAULT_INDEX_LLM_CONCURRENCY = min(8, max(2, DEFAULT_INDEX_CONCURRENCY * 2))
DEFAULT_COMPLEXITY_SAMPLE_PAGES = 12
DEFAULT_BM25_PREFILTER_TOP_K = 8
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FORMAT = "json"
DEFAULT_CHUNK_OVERLAP_RATIO = 0.15
DEFAULT_CONFIG_PATH = BASE_DIR / "document_assistant_config.yaml"
DEFAULT_PDF_IO_CONCURRENCY = 2
DEFAULT_BM25_STOPWORDS = {
    "的", "了", "吗", "呢", "啊", "呀", "吧", "和", "及", "与", "并", "且",
    "在", "是", "为", "对", "就", "都", "而", "及其", "一个", "一种", "这个",
    "那个", "我们", "你们", "他们", "它们", "请", "问", "中", "上", "下", "后",
    "前", "里", "外", "有", "无", "把", "被", "将", "及", "或", "及", "等",
}

logger = logging.getLogger(__name__)


def _parse_cli_list_values(values: list[str] | None) -> list[str]:
    """解析可重复/逗号分隔的 CLI 列表参数。"""
    items: list[str] = []
    for raw in values or []:
        text = str(raw or "").strip()
        if not text:
            continue
        for token in text.split(","):
            candidate = str(token or "").strip()
            if candidate:
                items.append(candidate)
    return items


def _resolve_delete_doc_ids(client: PageIndexClient, *, raw_doc_ids: list[str], raw_files: list[str]) -> tuple[set[str], list[str]]:
    """把删除参数解析为 doc_id 集合，并返回未命中项。"""
    catalog = build_document_catalog(client)
    by_id = {str(item.get("doc_id", "") or ""): str(item.get("doc_id", "") or "") for item in catalog}
    by_name = {str(item.get("doc_name", "") or "").strip().lower(): str(item.get("doc_id", "") or "") for item in catalog}
    by_path_name = {
        Path(str(item.get("path", "") or "")).name.strip().lower(): str(item.get("doc_id", "") or "")
        for item in catalog
    }
    by_stem: dict[str, str] = {}
    for item in catalog:
        doc_id = str(item.get("doc_id", "") or "")
        doc_name = str(item.get("doc_name", "") or "").strip()
        if doc_name:
            by_stem.setdefault(Path(doc_name).stem.lower(), doc_id)
        file_name = Path(str(item.get("path", "") or "")).name.strip()
        if file_name:
            by_stem.setdefault(Path(file_name).stem.lower(), doc_id)

    resolved: set[str] = set()
    unresolved: list[str] = []

    for token in raw_doc_ids:
        hit = by_id.get(token)
        if not hit:
            unresolved.append(token)
            continue
        resolved.add(hit)

    for token in raw_files:
        key = token.strip().lower()
        hit = by_name.get(key) or by_path_name.get(key) or by_stem.get(key)
        if not hit:
            unresolved.append(token)
            continue
        resolved.add(hit)

    return resolved, unresolved


def _delete_docs(client: PageIndexClient, doc_ids: set[str]) -> int:
    """删除指定 doc_id 文档并返回删除数量。"""
    if not doc_ids:
        return 0
    store = getattr(client, "_store", None)
    if store is None:
        raise RuntimeError("Storage backend is not initialized; cannot delete documents.")
    deleted = 0
    for doc_id in doc_ids:
        with contextlib.suppress(Exception):
            store.delete_doc(doc_id)
            deleted += 1
        client.documents.pop(doc_id, None)
    return deleted


def _install_asyncio_shutdown_exception_filter() -> None:
    """Suppress known httpx/anyio shutdown noise when loop is closing."""
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def _handler(current_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        message = str(context.get("message", "") or "")
        exc = context.get("exception")
        task = context.get("task") or context.get("future")
        task_repr = repr(task) if task is not None else ""
        if (
            isinstance(exc, RuntimeError)
            and "Event loop is closed" in str(exc)
            and "Task exception was never retrieved" in message
            and "AsyncClient.aclose()" in task_repr
        ):
            logger.debug("suppressed_asyncio_shutdown_noise")
            return
        if previous_handler is not None:
            previous_handler(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


def _estimate_recommendation_item_count(text: str) -> int:
    raw = str(text or "").strip()
    if not raw:
        return 0
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    numbered = sum(1 for line in lines if re.match(r"^\d+\s*[\.\)、)]\s*", line))
    if numbered >= 2:
        return min(6, numbered)
    bullet = sum(1 for line in lines if re.match(r"^[-*•]\s+", line))
    if bullet >= 2:
        return min(6, bullet)
    return 0


def _safe_console_print(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        import sys
        encoded = str(text or "").encode(sys.stdout.encoding or "utf-8", errors="replace")
        print(encoded.decode(sys.stdout.encoding or "utf-8", errors="replace"))


def _print_evidence_details(result: dict):
    citations = result.get("citations", []) if isinstance(result, dict) else []
    evidence = result.get("evidence", []) if isinstance(result, dict) else []
    answer_text = str(result.get("answer", "") or "").strip() if isinstance(result, dict) else ""
    answer_has_citation_block = "参考依据：" in answer_text
    recommendation_item_count = _estimate_recommendation_item_count(answer_text)
    snippet_show_limit = 2 if recommendation_item_count <= 0 else max(2, min(6, recommendation_item_count))

    if citations and not answer_has_citation_block:
        _safe_console_print("\n参考依据：")
        seen_citations: set[tuple[str, str]] = set()
        for item in citations:
            doc_name = str(item.get("doc_name", "") or "").strip()
            pages = str(item.get("pages", "") or "").strip()
            if not (doc_name and pages):
                continue
            key = (doc_name, pages)
            if key in seen_citations:
                continue
            seen_citations.add(key)
            _safe_console_print(f"- 《{doc_name}》 第{pages}页")

    printed_snippet_header = False
    for item in evidence:
        if not isinstance(item, dict):
            continue
        doc_name = str(item.get("doc_name", "") or "").strip()
        snippets = item.get("snippets", [])
        if not isinstance(snippets, list) or not snippets:
            continue
        if not printed_snippet_header:
            _safe_console_print("\n证据片段：")
            printed_snippet_header = True
        shown = 0
        for snippet in snippets:
            if not isinstance(snippet, dict):
                continue
            page = snippet.get("page")
            page_range = str(snippet.get("page_range", "") or "").strip()
            content = str(snippet.get("content", "") or "").strip()
            source_tool = str(snippet.get("source_tool", "") or "").strip()
            if page is None or not content:
                continue
            page_label = page_range if page_range else str(page)
            if source_tool:
                _safe_console_print(f"- 《{doc_name}》 第{page_label}页（来源: {source_tool}）：{content}")
            else:
                _safe_console_print(f"- 《{doc_name}》 第{page_label}页：{content}")
            shown += 1
            if shown >= snippet_show_limit:
                break




# 读取 PDF 总页数；优先使用 PyMuPDF，失败时回退到 PyPDF2。
def get_pdf_page_count(file_path: Path) -> int:
    """读取 PDF 总页数。优先使用 PyMuPDF，失败时回退到 PyPDF2。"""
    try:
        if fitz is not None:
            with fitz.open(file_path) as doc:
                return doc.page_count
        if PyPDF2 is not None:
            with open(file_path, "rb") as f:
                return len(PyPDF2.PdfReader(f).pages)
        raise RuntimeError("Neither PyMuPDF nor PyPDF2 is available for PDF page counting.")
    except Exception as exc:
        raise RuntimeError(f"Failed to read PDF page count: {exc}") from exc


def _normalize_whitespace(text: str) -> str:
    """压缩多余空白和空行，让文本更适合后续检索与摘要。"""
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text or "")).strip()


_MOJIBAKE_HINT_CHARS = set("銆鍏鍦鏄鐨鍙鍚鍒鍥閲锛锟鈥鎴鏃涓婁笅骞")


def estimate_mojibake_ratio(text: str) -> float:
    """估算文本乱码占比，用于比较提取后文本质量。"""
    sample = _normalize_whitespace(text or "")
    if not sample:
        return 0.0
    total = len(sample)
    hint_count = sum(1 for ch in sample if ch in _MOJIBAKE_HINT_CHARS)
    replacement_count = sample.count("\ufffd")
    weird_latin = len(re.findall(r"[\u00c0-\u024f]", sample))
    signal = hint_count + replacement_count * 2 + weird_latin
    return min(1.0, signal / max(1, total))


def _sanitize_extracted_text(text: str) -> str:
    """清洗提取文本：删除明显乱码行和相邻重复行。"""
    normalized = _normalize_whitespace(text or "")
    if not normalized:
        return ""
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not lines:
        return ""
    kept: list[str] = []
    for line in lines:
        if len(line) >= 24 and estimate_mojibake_ratio(line) >= 0.16:
            continue
        kept.append(line)
    if not kept:
        kept = lines
    deduped: list[str] = []
    prev = ""
    for line in kept:
        if line != prev:
            deduped.append(line)
        prev = line
    return _normalize_whitespace("\n".join(deduped))


def _score_pdf_pages_quality(pages: list[dict]) -> float:
    """给 PDF 提取结果打分；分数越高表示越适合后续摘要与检索。"""
    if not pages:
        return 0.0
    total_chars = 0
    non_empty_pages = 0
    mojibake_weighted = 0.0
    for item in pages:
        content = str((item or {}).get("content", "") or "")
        text = _normalize_whitespace(content)
        if text:
            non_empty_pages += 1
        char_count = len(text)
        total_chars += char_count
        mojibake_weighted += estimate_mojibake_ratio(text) * char_count
    if total_chars <= 0:
        return 0.0
    non_empty_ratio = non_empty_pages / max(1, len(pages))
    avg_chars = total_chars / max(1, len(pages))
    density = min(1.0, avg_chars / 500.0)
    mojibake_ratio = mojibake_weighted / max(1, total_chars)
    return (0.55 * non_empty_ratio) + (0.35 * density) + (0.25 * (1.0 - mojibake_ratio))


# 尽量保留版面信息提取单页文本：
# 1) 普通文本流
# 2) block 文本回退
# 3) 表格文本（若可用）
def extract_page_text_with_layout(page, *, table_parse_mode: str = "auto") -> str:
    """尽量保留版面信息提取单页文本，必要时补充 block 和表格内容。"""
    text = ""
    try:
        text = page.get_text("text", sort=True) or ""
    except Exception:
        text = ""

    blocks_text = ""
    if len(text.strip()) < 80:
        try:
            blocks = page.get_text("blocks", sort=True) or []
            rows = []
            for block in blocks:
                if len(block) >= 5 and isinstance(block[4], str):
                    cell = block[4].strip()
                    if cell:
                        rows.append(cell)
            blocks_text = "\n".join(rows)
        except Exception:
            blocks_text = ""

    table_text = ""
    if str(table_parse_mode or "auto").strip().lower() != "off":
        try:
            finder = getattr(page, "find_tables", None)
            if callable(finder):
                tables = finder()
                table_rows = []
                for table in getattr(tables, "tables", [])[:2]:
                    extracted = table.extract()
                    if not extracted:
                        continue
                    for row in extracted:
                        cells = [str(cell).strip() if cell is not None else "" for cell in row]
                        table_rows.append(" | ".join(cells).strip())
                if table_rows:
                    table_text = "\n".join(table_rows)
        except Exception:
            table_text = ""

    merged_parts = [_normalize_whitespace(text)]
    if blocks_text:
        merged_parts.append(_normalize_whitespace(blocks_text))
    if table_text:
        merged_parts.append(_normalize_whitespace(table_text))
    merged = "\n\n".join(part for part in merged_parts if part)
    return _normalize_whitespace(merged)


def _extract_pdf_pages_with_pymupdf(file_path: Path, *, table_parse_mode: str = "auto") -> list[dict]:
    pages: list[dict] = []
    if fitz is None:
        return pages
    with fitz.open(file_path) as doc:
        for i, page in enumerate(doc, 1):
            try:
                text = extract_page_text_with_layout(page, table_parse_mode=table_parse_mode)
            except Exception as exc:
                log_event(
                    "pdf_page_extract_failed",
                    level=logging.WARNING,
                    file_name=file_path.name,
                    page=i,
                    backend="pymupdf",
                    error=str(exc),
                )
                text = ""
            pages.append({"page": i, "content": _sanitize_extracted_text(text)})
    return pages


def _extract_pdf_pages_with_pypdf2(file_path: Path) -> list[dict]:
    pages: list[dict] = []
    if PyPDF2 is None:
        return pages
    with open(file_path, "rb") as f:
        pdf_reader = PyPDF2.PdfReader(f)
        for i, page in enumerate(pdf_reader.pages, 1):
            try:
                text = _normalize_whitespace(page.extract_text() or "")
            except Exception as exc:
                log_event(
                    "pdf_page_extract_failed",
                    level=logging.WARNING,
                    file_name=file_path.name,
                    page=i,
                    backend="pypdf2",
                    error=str(exc),
                )
                text = ""
            pages.append({"page": i, "content": _sanitize_extracted_text(text)})
    return pages


def read_pdf_pages(file_path: Path, *, table_parse_mode: str = "auto") -> list[dict]:
    """一次性读取 PDF 页面内容，并在多后端间按质量择优。"""
    candidates: list[tuple[str, list[dict], float]] = []
    try:
        pymupdf_pages = _extract_pdf_pages_with_pymupdf(file_path, table_parse_mode=table_parse_mode)
        if pymupdf_pages:
            candidates.append(("pymupdf", pymupdf_pages, _score_pdf_pages_quality(pymupdf_pages)))
    except Exception as exc:
        log_event("pdf_extract_backend_failed", level=logging.INFO, file_name=file_path.name, backend="pymupdf", error=str(exc))

    try:
        pypdf2_pages = _extract_pdf_pages_with_pypdf2(file_path)
        if pypdf2_pages:
            candidates.append(("pypdf2", pypdf2_pages, _score_pdf_pages_quality(pypdf2_pages)))
    except Exception as exc:
        log_event("pdf_extract_backend_failed", level=logging.INFO, file_name=file_path.name, backend="pypdf2", error=str(exc))

    if not candidates:
        raise RuntimeError("Failed to read PDF pages: no available extraction backend.")

    backend, pages, score = max(candidates, key=lambda item: item[2])
    log_event(
        "pdf_extract_backend_selected",
        file_name=file_path.name,
        backend=backend,
        quality_score=round(float(score), 4),
    )
    return pages


# 将整份文档按页切块，并支持固定比例重叠，降低切块边界导致的召回损失。
def chunk_windows(total_pages: int, chunk_pages: int, overlap_ratio: float) -> list[tuple[int, int]]:
    """按页数切分窗口，并保留一定重叠，减少切块边界丢信息。"""
    if total_pages <= 0:
        return []
    chunk_pages = max(1, chunk_pages)
    overlap_ratio = min(0.5, max(0.0, overlap_ratio))
    overlap_pages = int(round(chunk_pages * overlap_ratio))
    step = max(1, chunk_pages - overlap_pages)
    windows = []
    start = 1
    while start <= total_pages:
        end = min(total_pages, start + chunk_pages - 1)
        windows.append((start, end))
        if end == total_pages:
            break
        start += step
    return windows


# 中文 BM25 分词器：优先 jieba；没有 jieba 时使用正则回退。
# 同时为连续中文词补充二元切分，提升短语部分命中能力。
def tokenize_for_bm25_zh(text: str) -> list[str]:
    """中文 BM25 分词器：优先用 jieba，没有就回退到正则。"""
    if not text:
        return []
    lowered = text.lower().strip()
    if not lowered:
        return []

    raw_tokens: list[str]
    if jieba is not None:
        raw_tokens = [tok.strip().lower() for tok in jieba.cut(lowered, cut_all=False)]
    else:
        raw_tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", lowered)

    cleaned: list[str] = []
    for tok in raw_tokens:
        if not tok or tok in DEFAULT_BM25_STOPWORDS:
            continue
        if re.fullmatch(r"[\W_]+", tok):
            continue
        cleaned.append(tok)
        # Add bi-gram fallback for long CJK tokens to improve partial phrase matching.
        if re.fullmatch(r"[\u4e00-\u9fff]{2,}", tok):
            for i in range(len(tok) - 1):
                bi = tok[i:i + 2]
                if bi not in DEFAULT_BM25_STOPWORDS:
                    cleaned.append(bi)
    return cleaned


def enable_bm25_zh_tokenizer() -> str:
    """把中文 BM25 分词器挂到 assistant_components 上，并返回使用的后端名。"""
    assistant_components_module.tokenize_for_bm25 = tokenize_for_bm25_zh
    return "jieba" if jieba is not None else "regex-fallback"


# 判断页面结尾是否像“语义敏感边界”（章节标题/表格/附录等），
# 用于后续微调分块边界，减少把完整语义切断。
def _looks_like_sensitive_boundary(text: str) -> bool:
    """判断一页内容是否像章节边界、表格区或附录等敏感切分点。"""
    if not text:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()][:20]
    if not lines:
        return False
    heading_pattern = re.compile(r"^\s*(第[一二三四五六七八九十百零\d]+[章节部分篇]|[0-9]+(\.[0-9]+){0,2})")
    if any(heading_pattern.match(line) and len(line) <= 40 for line in lines[:8]):
        return True
    table_like_lines = sum(1 for line in lines[:12] if "|" in line or "│" in line or "\t" in line)
    if table_like_lines >= 2:
        return True
    if any(("表" in line[:6] or "附录" in line[:6]) and len(line) <= 30 for line in lines[:8]):
        return True
    return False


def adjust_windows_for_semantic_boundaries(
    windows: list[tuple[int, int]],
    pages: list[dict],
    *,
    max_shift_pages: int = 1,
) -> list[tuple[int, int]]:
    """根据页面内容微调切块边界，尽量不要把语义硬切断。"""
    if not windows or not pages:
        return windows
    page_text_by_no = {
        int(item.get("page", 0)): str(item.get("content", "") or "")
        for item in pages
        if isinstance(item, dict)
    }
    max_page = max(page_text_by_no.keys(), default=0)
    if max_page <= 0:
        return windows

    adjusted: list[tuple[int, int]] = []
    for start_page, end_page in windows:
        start_page = max(1, min(start_page, max_page))
        end_page = max(start_page, min(end_page, max_page))
        candidate_end = end_page
        if _looks_like_sensitive_boundary(page_text_by_no.get(end_page, "")):
            for shift in range(1, max_shift_pages + 1):
                probe = end_page + shift
                if probe > max_page:
                    break
                if not _looks_like_sensitive_boundary(page_text_by_no.get(probe, "")):
                    candidate_end = probe
                    break
        adjusted.append((start_page, candidate_end))
    return adjusted


# 若检测到目录锚点，则以目录项构造章节窗口，并在前后增加轻量重叠。
def build_toc_windows_with_overlap(
    toc_entries: list[tuple[str, int]],
    page_count: int,
    overlap_ratio: float,
) -> list[tuple[str, int, int]]:
    """根据目录锚点生成章节窗口，并在相邻章节间保留轻量重叠。"""
    if not toc_entries or page_count <= 0:
        return []
    overlap_pages = max(1, int(round(max(0.1, min(0.5, overlap_ratio)) * 6)))
    windows: list[tuple[str, int, int]] = []
    for idx, (title, anchor_page) in enumerate(toc_entries):
        next_anchor = toc_entries[idx + 1][1] if idx + 1 < len(toc_entries) else page_count + 1
        base_start = max(1, min(page_count, int(anchor_page)))
        base_end = min(page_count, max(base_start, int(next_anchor) - 1))
        start_page = max(1, base_start - (overlap_pages if idx > 0 else 0))
        end_page = min(page_count, base_end + (overlap_pages if idx + 1 < len(toc_entries) else 0))
        if end_page < start_page:
            end_page = start_page
        windows.append((title, start_page, end_page))
    return windows


def analyze_pdf_complexity(file_path: Path, sample_pages: int = DEFAULT_COMPLEXITY_SAMPLE_PAGES) -> dict:
    """采样分析 PDF 的稀疏程度和结构化程度，用于选择索引模式。"""
    try:
        if fitz is not None:
            with fitz.open(file_path) as doc:
                page_count = doc.page_count
                if page_count == 0:
                    return {"avg_chars": 0.0, "empty_ratio": 1.0, "heading_ratio": 0.0}
                sample_ids = sorted(
                    set(
                        list(range(min(sample_pages // 2, page_count)))
                        + list(range(max(0, page_count - (sample_pages // 2)), page_count))
                    )
                )
                texts = [(doc[idx].get_text("text", sort=True) or "") for idx in sample_ids]
        else:
            texts = []
            with open(file_path, "rb") as f:
                pdf_reader = PyPDF2.PdfReader(f)
                page_count = len(pdf_reader.pages)
                if page_count == 0:
                    return {"avg_chars": 0.0, "empty_ratio": 1.0, "heading_ratio": 0.0}
                sample_ids = sorted(
                    set(
                        list(range(min(sample_pages // 2, page_count)))
                        + list(range(max(0, page_count - (sample_pages // 2)), page_count))
                    )
                )
                for idx in sample_ids:
                    texts.append(pdf_reader.pages[idx].extract_text() or "")

        empty_pages = sum(1 for text in texts if not text.strip())
        avg_chars = sum(len(text) for text in texts) / max(1, len(texts))
        heading_lines = 0
        total_lines = 0
        heading_pattern = re.compile(r"^\s*(第[一二三四五六七八九十百零\d]+[章节部分篇]|[0-9]+(\.[0-9]+){0,2})")
        for text in texts:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            total_lines += len(lines)
            for line in lines[:80]:
                if len(line) <= 36 and heading_pattern.match(line):
                    heading_lines += 1
        heading_ratio = heading_lines / max(1, total_lines)
        empty_ratio = empty_pages / max(1, len(texts))
        return {"avg_chars": avg_chars, "empty_ratio": empty_ratio, "heading_ratio": heading_ratio}
    except Exception as exc:
        log_event("pdf_complexity_analyze_failed", level=logging.WARNING, file_name=file_path.name, error=str(exc))
        return {"avg_chars": 0.0, "empty_ratio": 1.0, "heading_ratio": 0.0}


# 长文档策略选择器：
# - standard: 走原始索引
# - simple: 稀疏/低信息文档，走简化分块摘要索引
# - hybrid: 结构化明显文档，优先目录/章节驱动分块
def choose_pdf_index_mode(
    page_count: int,
    complexity: dict,
    *,
    soft_threshold: int,
    hard_threshold: int,
    forced_mode: str,
) -> str:
    """根据页数和复杂度，在 standard/simple/hybrid 中选一个索引模式。"""
    if forced_mode in {"standard", "hybrid", "simple"}:
        return forced_mode

    empty_ratio = float(complexity.get("empty_ratio", 1.0))
    heading_ratio = float(complexity.get("heading_ratio", 0.0))
    avg_chars = float(complexity.get("avg_chars", 0.0))

    page_span = max(1, hard_threshold - soft_threshold)
    page_pressure = max(0.0, min(1.0, (page_count - soft_threshold) / page_span))
    sparse_score = 0.7 * empty_ratio + 0.3 * max(0.0, 1.0 - (avg_chars / 260.0))
    structure_score = min(1.0, heading_ratio / 0.04) + 0.2 * min(1.0, avg_chars / 600.0)

    if sparse_score >= 0.82 and page_pressure >= 0.2:
        return "simple"
    if structure_score >= 0.82:
        return "hybrid"
    if page_pressure >= 0.55:
        if sparse_score >= 0.62:
            return "simple"
        if structure_score >= 0.4:
            return "hybrid"
    if empty_ratio >= 0.8 and avg_chars < 80:
        return "simple"
    if heading_ratio >= 0.03 and avg_chars >= 150:
        return "hybrid"
    return "standard"


def detect_summary_language(text: str) -> str:
    """粗略判断摘要提示词应使用中文还是英文。"""
    sample = (text or "").strip()
    if not sample:
        return "zh"
    if len(sample) > 6000:
        sample = sample[:6000]

    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", sample))
    english_words = len(re.findall(r"\b[a-zA-Z]{2,}\b", sample))

    if cjk_chars >= max(20, english_words * 2):
        return "zh"
    if english_words >= max(20, cjk_chars // 2):
        return "en"
    return "zh" if cjk_chars >= english_words else "en"


def _estimate_token_count(text: str) -> int:
    sample = str(text or "")
    if not sample:
        return 0
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", sample))
    latin_words = len(re.findall(r"\b[a-zA-Z0-9_]+\b", sample))
    return cjk_chars + max(1, int(latin_words * 1.3))


def _split_page_range_by_token_budget(
    page_map: dict[int, str],
    start_page: int,
    end_page: int,
    node_max_tokens: int,
) -> list[tuple[int, int, str]]:
    budget = max(64, int(node_max_tokens))
    segments: list[tuple[int, int, str]] = []
    cur_start = start_page
    cur_end = start_page - 1
    cur_parts: list[str] = []
    cur_tokens = 0

    for page_no in range(start_page, end_page + 1):
        page_text = str(page_map.get(page_no, "") or "")
        if not page_text.strip():
            continue
        page_tokens = max(1, _estimate_token_count(page_text))
        if cur_parts and (cur_tokens + page_tokens) > budget:
            segments.append((cur_start, cur_end, "\n".join(cur_parts)))
            cur_start = page_no
            cur_end = page_no
            cur_parts = [page_text]
            cur_tokens = page_tokens
            continue
        if not cur_parts:
            cur_start = page_no
        cur_parts.append(page_text)
        cur_end = page_no
        cur_tokens += page_tokens

    if cur_parts:
        segments.append((cur_start, cur_end, "\n".join(cur_parts)))
    return segments or [(start_page, end_page, "")]


def _fallback_chunk_summary(text: str, start_page: int, end_page: int) -> str:
    compact = _normalize_whitespace(text)
    if not compact:
        return f"第{start_page}-{end_page}页可提取文本较少。"
    head = compact[:200]
    return f"第{start_page}-{end_page}页要点：{head}"


async def summarize_page_chunk_async(
    text: str,
    start_page: int,
    end_page: int,
    model: str,
    runtime: AssistantRuntime,
) -> str:
    """调用 LLM 总结一个分页块，供后续索引和检索使用。"""
    compact_text = (text or "").strip()
    if len(compact_text) > 4000:
        compact_text = compact_text[:4000]

    if not compact_text:
        return f"第{start_page}-{end_page}页可提取文本较少。"

    summary_lang = detect_summary_language(compact_text)
    if summary_lang == "zh":
        prompt = (
            "你是文档索引助手。请将下面页面内容总结为简体中文摘要，"
            "用于检索与问答。保留关键主题、时间、地点、人物、事实与结论。"
            "只输出摘要正文，不要输出解释。\n\n"
            f"页码范围: {start_page}-{end_page}\n\n"
            f"文档片段:\n{compact_text}"
        )
    else:
        prompt = (
            "You are a document indexing assistant. Summarize the following page chunk in English "
            "for retrieval and QA. Keep key topics, places, times, people, facts, and conclusions. "
            "Output only the summary.\n\n"
            f"Page range: {start_page}-{end_page}\n\n"
            f"Document chunk:\n{compact_text}"
        )
    summary = await async_llm_completion(model=model, prompt=prompt, breaker=runtime.llm_breaker, runtime=runtime)
    return summary or f"第{start_page}-{end_page}页摘要生成失败。"

# 基于全部分块摘要，生成文档级一句话描述，便于文档路由阶段使用。
async def generate_simple_doc_description_async(
    doc_name: str,
    chunk_summaries: list[str],
    model: str,
    runtime: AssistantRuntime,
) -> str:
    """根据所有分块摘要，生成一句文档级简介。"""
    joined = "\n".join(summary for summary in chunk_summaries if summary.strip())
    if len(joined) > 6000:
        joined = joined[:6000]
    prompt = (
        "You are a document description assistant. Based on the chunk summaries below, "
        "write one concise document description in Simplified Chinese. Output only one sentence.\n\n"
        f"Document name: {doc_name}\n\n"
        f"Chunk summaries:\n{joined}"
    )
    description = await async_llm_completion(model=model, prompt=prompt, breaker=runtime.llm_breaker, runtime=runtime)
    return description or f"{doc_name} 的简化索引文档。"


# 简化索引模式：按窗口并发生成摘要，拼成结构树节点。
async def build_simple_pdf_structure_async(
    file_path: Path,
    pages: list[dict],
    model: str,
    chunk_pages: int,
    overlap_ratio: float,
    runtime: AssistantRuntime,
    *,
    llm_concurrency: int = DEFAULT_INDEX_LLM_CONCURRENCY,
    summary_enabled: bool = True,
    node_max_tokens: int = 512,
    max_tree_depth: int = 3,
) -> tuple[list[dict], str]:
    """simple 模式下构建 PDF 结构：切块、摘要、生成结构节点。"""
    windows = chunk_windows(len(pages), chunk_pages, overlap_ratio)
    windows = adjust_windows_for_semantic_boundaries(windows, pages)
    semaphore = asyncio.Semaphore(max(1, llm_concurrency))
    page_map = {int(item["page"]): item.get("content", "") for item in pages if isinstance(item, dict) and "page" in item}

    async def summarize_one(node_counter: int, start_page: int, end_page: int, chunk_text: str):
        """总结一个页窗口，并带回窗口编号。"""
        if summary_enabled:
            async with semaphore:
                summary = await summarize_page_chunk_async(chunk_text, start_page, end_page, model, runtime)
        else:
            summary = _fallback_chunk_summary(chunk_text, start_page, end_page)
        return node_counter, start_page, end_page, summary

    chunk_segments: list[tuple[int, int, str]] = []
    for start_page, end_page in windows:
        chunk_segments.extend(_split_page_range_by_token_budget(page_map, start_page, end_page, node_max_tokens))
    tasks = [
        asyncio.create_task(summarize_one(node_counter, start_page, end_page, chunk_text))
        for node_counter, (start_page, end_page, chunk_text) in enumerate(chunk_segments, 1)
    ]
    results = await asyncio.gather(*tasks) if tasks else []
    results.sort(key=lambda row: row[0])

    if max_tree_depth <= 1:
        merged_summary = "；".join(summary for _, _, _, summary in results if summary.strip())[:1200]
        node_summary = merged_summary or f"{file_path.name} 内容摘要。"
        structure = [build_chunk_node("文档总览", "S0001", 1, len(pages), node_summary)]
        doc_description = node_summary[:180]
        return structure, doc_description

    structure: list[dict] = []
    chunk_summaries: list[str] = []
    for node_counter, start_page, end_page, summary in results:
        chunk_summaries.append(summary)
        structure.append(build_chunk_node(f"第{start_page}-{end_page}页", f"S{node_counter:04d}", start_page, end_page, summary))

    if summary_enabled:
        doc_description = await generate_simple_doc_description_async(file_path.name, chunk_summaries, model, runtime)
    else:
        doc_description = f"{file_path.name} 的文档索引。"
    return structure, doc_description


def detect_toc_entries(pages: list[dict], page_count: int, max_scan_pages: int = 20) -> list[tuple[str, int]]:
    """从前几页里尝试识别目录项，返回标题和锚点页码。"""
    entries = []
    pattern = re.compile(r"^\s*(.+?)\.{2,}\s*(\d{1,4})\s*$")
    for item in pages[:max_scan_pages]:
        for raw_line in item.get("content", "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = pattern.match(line)
            if not match:
                continue
            title = match.group(1).strip()
            page_str = match.group(2)
            try:
                page_no = int(page_str)
            except ValueError:
                continue
            if 1 <= page_no <= page_count:
                entries.append((title, page_no))

    deduped = []
    seen = set()
    for title, page_no in sorted(entries, key=lambda x: x[1]):
        key = (title, page_no)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


# 为非目录切分场景自动挑一个可读标题。
def pick_chunk_title(chunk: list[dict], start_page: int, end_page: int) -> str:
    """给一个页块自动挑一个可读标题；挑不到就用页码范围。"""
    for item in chunk[:2]:
        for line in item.get("content", "").splitlines():
            stripped = line.strip()
            if 3 <= len(stripped) <= 40:
                return f"{stripped} (第{start_page}-{end_page}页)"
    return f"第{start_page}-{end_page}页"


def _has_page_signal(text: str) -> bool:
    """判断页面是否含有足够信息，避免把纯空页或噪声页拿去摘要。"""
    if not text:
        return False
    normalized = re.sub(r"\s+", "", text)
    if len(normalized) >= 20:
        return True
    if re.search(r"[A-Za-z0-9]{6,}", normalized):
        return True
    if re.search(r"[|│\t]", text):
        return True
    if re.search(r"(第[一二三四五六七八九十百零\d]+[章节部分篇]|[0-9]+(\.[0-9]+){0,2})", text):
        return True
    return False


# 过滤“几乎无有效信息”的页面，避免空页干扰摘要与检索。
def filter_sparse_pages(pages: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """过滤掉几乎没有有效信息的页面，并返回统计信息。"""
    if not pages:
        return pages, {"total_pages": 0, "kept_pages": 0, "dropped_pages": 0}
    kept = []
    dropped = 0
    for item in pages:
        content = str(item.get("content", "") or "")
        if _has_page_signal(content):
            kept.append(item)
        else:
            dropped += 1
    if not kept:
        kept = pages[:1]
        dropped = max(0, len(pages) - len(kept))
    return kept, {"total_pages": len(pages), "kept_pages": len(kept), "dropped_pages": dropped}


def _looks_like_docx_toc_entry(text: str) -> bool:
    """判断 DOCX 段落是否像目录项。"""
    sample = _normalize_whitespace(text or "")
    if not sample:
        return False
    if sample.lower() in {"目录", "contents"}:
        return True
    if re.search(r"(?:[.\u2026·•。．\s]{3,}|\t+)\s*\d{1,4}\s*$", sample):
        return True
    return False


def _detect_docx_heading_level(para: Any, text: str) -> int | None:
    """识别 DOCX 标题层级，风格与 conversion.py 保持一致。"""
    style_name = ""
    try:
        style_name = str(getattr(getattr(para, "style", None), "name", "") or "")
    except Exception:
        style_name = ""
    match = re.search(r"heading\s*(\d+)", style_name, flags=re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))
    patterns: list[tuple[str, int]] = [
        (r"^\s*chapter\s*\d+", 1),
        (r"^\s*第[一二三四五六七八九十百千万0-9]+章", 1),
        (r"^\s*[（(]?[一二三四五六七八九十]+[)）]\s*", 2),
        (r"^\s*\d+[.、．]\s*", 3),
        (r"^\s*\d+\.\d+\s*", 3),
        (r"^\s*\d+\.\d+\.\d+\s*", 4),
    ]
    for pattern, level in patterns:
        if re.match(pattern, text, flags=re.IGNORECASE):
            return level
    return None


def read_docx_pages_and_sections(file_path: Path, *, paragraphs_per_page: int = 5) -> tuple[list[dict], list[dict], int]:
    """读取 DOCX 并构建伪分页和章节段落结构。"""
    if DocxDocument is None:
        raise RuntimeError("python-docx is not installed. Install: pip install python-docx")
    document = DocxDocument(str(file_path))

    paragraphs: list[str] = []
    sections: list[dict] = []
    stack: list[tuple[int, dict]] = []
    paragraph_index = 0
    raw_non_empty_index = 0

    for para in document.paragraphs:
        text = _normalize_whitespace(getattr(para, "text", "") or "")
        if not text:
            continue
        raw_non_empty_index += 1
        if raw_non_empty_index <= 150 and _looks_like_docx_toc_entry(text):
            continue

        paragraph_index += 1
        paragraphs.append(text)

        heading_level = _detect_docx_heading_level(para, text)
        if heading_level is not None:
            section = {
                "title": text,
                "level": int(heading_level),
                "start_paragraph": paragraph_index,
                "end_paragraph": paragraph_index,
                "_body_chunks": [],
            }
            while stack and stack[-1][0] >= heading_level:
                stack.pop()
            sections.append(section)
            stack.append((heading_level, section))
            continue

        if stack:
            current = stack[-1][1]
            current.setdefault("_body_chunks", []).append(text)
            current["end_paragraph"] = paragraph_index
        else:
            implicit = {
                "title": text[:30] if len(text) > 30 else text,
                "level": 1,
                "start_paragraph": paragraph_index,
                "end_paragraph": paragraph_index,
                "_body_chunks": [text],
            }
            sections.append(implicit)

    pages: list[dict] = []
    if paragraphs:
        for start in range(0, len(paragraphs), max(1, int(paragraphs_per_page))):
            chunk = paragraphs[start : start + max(1, int(paragraphs_per_page))]
            pages.append({"page": len(pages) + 1, "content": _normalize_whitespace("\n".join(chunk))})

    def paragraph_to_page(paragraph_no: int) -> int:
        if paragraph_no <= 0:
            return 1
        return max(1, ((paragraph_no - 1) // max(1, int(paragraphs_per_page))) + 1)

    normalized_sections: list[dict] = []
    for idx, section in enumerate(sections, 1):
        title = _normalize_whitespace(section.get("title", ""))
        if not title:
            continue
        body_text = _normalize_whitespace("\n".join(section.pop("_body_chunks", []) or []))
        text = f"{title}\n{body_text}" if body_text and body_text != title else title
        start_paragraph = int(section.get("start_paragraph", 1) or 1)
        end_paragraph = int(section.get("end_paragraph", start_paragraph) or start_paragraph)
        start_page = paragraph_to_page(start_paragraph)
        end_page = paragraph_to_page(max(start_paragraph, end_paragraph))
        normalized_sections.append(
            {
                "order": idx,
                "title": title,
                "text": text,
                "level": int(section.get("level", 1) or 1),
                "start_page": start_page,
                "end_page": max(start_page, end_page),
            }
        )

    return pages, normalized_sections, paragraph_index


# 计算文档语义内容哈希，用于跨文件名/跨路径去重。
def compute_document_content_hash(doc_data: dict) -> str:
    """根据文档主要内容计算内容哈希，用于跨路径去重。"""
    md5 = hashlib.md5()
    doc_type = str(doc_data.get("type", "") or "")
    md5.update(doc_type.encode("utf-8", errors="ignore"))
    md5.update(str(doc_data.get("doc_name", "") or "").encode("utf-8", errors="ignore"))
    md5.update(str(doc_data.get("file_size", "") or "").encode("utf-8", errors="ignore"))
    md5.update(str(doc_data.get("file_mtime", "") or "").encode("utf-8", errors="ignore"))
    md5.update(str(doc_data.get("file_head_md5", "") or doc_data.get("file_md5", "") or "").encode("utf-8", errors="ignore"))

    if doc_type == "pdf":
        for page in doc_data.get("pages", []) or []:
            content = str((page or {}).get("content", "") or "").strip()
            if content:
                md5.update(content.encode("utf-8", errors="ignore"))
    else:
        page_entries = doc_data.get("pages", []) or []
        if isinstance(page_entries, list) and page_entries:
            for page in page_entries:
                content = str((page or {}).get("content", "") or "").strip()
                if content:
                    md5.update(content.encode("utf-8", errors="ignore"))
        body = doc_data.get("content") or doc_data.get("text") or ""
        body = str(body).strip()
        if body:
            md5.update(body.encode("utf-8", errors="ignore"))
    return md5.hexdigest()


def find_doc_id_by_content_hash(client: PageIndexClient, content_hash: str | None) -> str | None:
    """按内容哈希在当前工作区里查重，命中则返回已有 doc_id。"""
    if not content_hash:
        return None
    for doc_id, doc in client.documents.items():
        if doc.get("content_hash") == content_hash:
            return doc_id
    return None


# 混合索引模式：有目录时优先目录窗口；否则回退到重叠分块窗口。
async def build_hybrid_pdf_structure_async(
    file_path: Path,
    pages: list[dict],
    model: str,
    chunk_pages: int,
    overlap_ratio: float,
    runtime: AssistantRuntime,
    *,
    llm_concurrency: int = DEFAULT_INDEX_LLM_CONCURRENCY,
    summary_enabled: bool = True,
    node_max_tokens: int = 512,
    max_tree_depth: int = 3,
) -> tuple[list[dict], str]:
    """hybrid 模式下构建 PDF 结构：优先按目录切块，否则回退到页窗口。"""
    page_count = len(pages)
    toc_entries = detect_toc_entries(pages, page_count=page_count)
    if len(toc_entries) >= 3:
        windows_with_title = build_toc_windows_with_overlap(toc_entries, page_count, overlap_ratio)
    else:
        windows = chunk_windows(page_count, chunk_pages, overlap_ratio)
        windows = adjust_windows_for_semantic_boundaries(windows, pages)
        windows_with_title = []
        for start_page, end_page in windows:
            chunk = [item for item in pages if start_page <= item["page"] <= end_page]
            windows_with_title.append((pick_chunk_title(chunk, start_page, end_page), start_page, end_page))

    semaphore = asyncio.Semaphore(max(1, llm_concurrency))
    page_map = {int(item["page"]): item.get("content", "") for item in pages if isinstance(item, dict) and "page" in item}

    async def summarize_one(node_counter: int, title: str, start_page: int, end_page: int, chunk_text: str):
        """总结一个带标题的页窗口，并带回窗口编号。"""
        if summary_enabled:
            async with semaphore:
                summary = await summarize_page_chunk_async(chunk_text, start_page, end_page, model, runtime)
        else:
            summary = _fallback_chunk_summary(chunk_text, start_page, end_page)
        return node_counter, title, start_page, end_page, summary

    expanded_windows_with_title: list[tuple[str, int, int, str]] = []
    for title, start_page, end_page in windows_with_title:
        if not (0 < start_page <= page_count):
            continue
        segments = _split_page_range_by_token_budget(page_map, start_page, end_page, node_max_tokens)
        for seg_start, seg_end, seg_text in segments:
            expanded_windows_with_title.append((title, seg_start, seg_end, seg_text))
    tasks = [
        asyncio.create_task(summarize_one(idx, title, start_page, end_page, chunk_text))
        for idx, (title, start_page, end_page, chunk_text) in enumerate(expanded_windows_with_title, 1)
    ]
    results = await asyncio.gather(*tasks) if tasks else []
    results.sort(key=lambda row: row[0])

    if max_tree_depth <= 1:
        merged_summary = "；".join(summary for _, _, _, _, summary in results if summary.strip())[:1200]
        node_summary = merged_summary or f"{file_path.name} 内容摘要。"
        structure = [build_chunk_node("文档总览", "H0001", 1, page_count, node_summary)]
        doc_description = node_summary[:180]
        return structure, doc_description

    structure: list[dict] = []
    chunk_summaries: list[str] = []
    for node_counter, title, start_page, end_page, summary in results:
        chunk_summaries.append(summary)
        structure.append(build_chunk_node(title or f"第{start_page}-{end_page}页", f"H{node_counter:04d}", start_page, end_page, summary))

    if summary_enabled:
        doc_description = await generate_simple_doc_description_async(file_path.name, chunk_summaries, model, runtime)
    else:
        doc_description = f"{file_path.name} 的文档索引。"
    return structure, doc_description


def save_simplified_pdf_document(
    client: PageIndexClient,
    file_path: Path,
    pages: list[dict],
    structure: list[dict],
    doc_description: str,
    index_mode: str,
) -> str:
    # 以最小必需字段落盘，同时保留结构与分页内容供后续检索。
    """把 simple/hybrid 模式生成的 PDF 索引结果保存到 client 和 PostgreSQL。"""
    doc_id = str(uuid.uuid4())
    payload = {
        "id": doc_id,
        "type": "pdf",
        "path": str(file_path.resolve()),
        "doc_name": file_path.name,
        "doc_description": doc_description,
        "page_count": len(pages),
        "structure": structure,
        "pages": pages,
        "index_mode": index_mode,
    }
    try:
        payload["content_hash"] = compute_document_content_hash(payload)
        payload = DocumentInfo.model_validate(
            {
                "id": payload["id"],
                "type": payload["type"],
                "path": payload["path"],
                "doc_name": payload["doc_name"],
                "doc_description": payload.get("doc_description", ""),
                "page_count": payload.get("page_count"),
                "index_mode": payload.get("index_mode", "standard"),
                "content_hash": payload.get("content_hash", ""),
            }
        ).model_dump() | {
            "structure": payload["structure"],
            "pages": payload["pages"],
        }
    except ValidationError as exc:
        logger.exception("Invalid simplified PDF payload for %s", file_path.name)
        raise RuntimeError(f"Invalid simplified PDF payload: {exc}") from exc
    client.documents[doc_id] = payload
    log_event("pdf_index_saved", doc_id=doc_id, index_mode=index_mode, file_name=file_path.name)
    client._save_doc(doc_id)
    return doc_id


def save_structured_docx_document(
    client: PageIndexClient,
    file_path: Path,
    pages: list[dict],
    structure: list[dict],
    doc_description: str,
    *,
    line_count: int,
    index_mode: str = "structured",
) -> str:
    """保存 DOCX 结构化索引结果，字段风格对齐 PDF 简化索引。"""
    doc_id = str(uuid.uuid4())
    payload = {
        "id": doc_id,
        "type": "docx",
        "path": str(file_path.resolve()),
        "doc_name": file_path.name,
        "doc_description": doc_description,
        "page_count": len(pages),
        "line_count": int(max(0, line_count)),
        "structure": structure,
        "pages": pages,
        "index_mode": index_mode,
    }
    try:
        payload["content_hash"] = compute_document_content_hash(payload)
        payload = DocumentInfo.model_validate(
            {
                "id": payload["id"],
                "type": payload["type"],
                "path": payload["path"],
                "doc_name": payload["doc_name"],
                "doc_description": payload.get("doc_description", ""),
                "page_count": payload.get("page_count"),
                "line_count": payload.get("line_count"),
                "index_mode": payload.get("index_mode", "standard"),
                "content_hash": payload.get("content_hash", ""),
            }
        ).model_dump() | {
            "structure": payload["structure"],
            "pages": payload["pages"],
        }
    except ValidationError as exc:
        logger.exception("Invalid structured DOCX payload for %s", file_path.name)
        raise RuntimeError(f"Invalid structured DOCX payload: {exc}") from exc
    client.documents[doc_id] = payload
    log_event("docx_index_saved", doc_id=doc_id, index_mode=index_mode, file_name=file_path.name)
    client._save_doc(doc_id)
    return doc_id


async def index_docx_with_structured_mode_async(
    client: PageIndexClient,
    file_path: Path,
    runtime: AssistantRuntime,
    *,
    llm_concurrency: int = DEFAULT_INDEX_LLM_CONCURRENCY,
    summary_enabled: bool = True,
    max_tree_depth: int = 3,
) -> str:
    """DOCX 结构化索引入口：按标题段落建结构，并生成摘要。"""
    log_event("docx_index_mode_selected", mode="structured", file_name=file_path.name)
    pages, sections, paragraph_count = await asyncio.to_thread(read_docx_pages_and_sections, file_path)
    if not pages:
        pages = [{"page": 1, "content": ""}]

    semaphore = asyncio.Semaphore(max(1, llm_concurrency))
    page_map = {int(item["page"]): str(item.get("content", "") or "") for item in pages if isinstance(item, dict) and "page" in item}

    if not sections:
        windows = chunk_windows(len(pages), max(2, min(8, len(pages))), 0.1)
        sections = []
        for idx, (start_page, end_page) in enumerate(windows, 1):
            chunk_text = _normalize_whitespace("\n".join(page_map.get(page_no, "") for page_no in range(start_page, end_page + 1)))
            sections.append(
                {
                    "order": idx,
                    "title": f"第{start_page}-{end_page}页",
                    "text": chunk_text,
                    "start_page": start_page,
                    "end_page": end_page,
                    "level": 1,
                }
            )

    async def summarize_one(section: dict) -> tuple[int, str, int, int, str]:
        order = int(section.get("order", 0) or 0)
        title = str(section.get("title", "") or "").strip() or f"章节{order}"
        start_page = int(section.get("start_page", 1) or 1)
        end_page = int(section.get("end_page", start_page) or start_page)
        text = _normalize_whitespace(str(section.get("text", "") or ""))
        if summary_enabled:
            async with semaphore:
                summary = await summarize_page_chunk_async(text, start_page, end_page, client.model, runtime)
        else:
            summary = _fallback_chunk_summary(text, start_page, end_page)
        return order, title, start_page, end_page, summary

    tasks = [asyncio.create_task(summarize_one(section)) for section in sections]
    results = await asyncio.gather(*tasks) if tasks else []
    results.sort(key=lambda row: row[0])

    if max_tree_depth <= 1:
        merged_summary = "；".join(summary for _, _, _, _, summary in results if summary.strip())[:1200]
        node_summary = merged_summary or f"{file_path.name} 内容摘要。"
        structure = [build_chunk_node("文档总览", "D0001", 1, len(pages), node_summary)]
        doc_description = node_summary[:180]
        return save_structured_docx_document(
            client,
            file_path,
            pages,
            structure,
            doc_description,
            line_count=paragraph_count,
            index_mode="structured",
        )

    structure: list[dict] = []
    chunk_summaries: list[str] = []
    for order, title, start_page, end_page, summary in results:
        chunk_summaries.append(summary)
        structure.append(build_chunk_node(title, f"D{order:04d}", start_page, end_page, summary))

    if summary_enabled:
        doc_description = await generate_simple_doc_description_async(file_path.name, chunk_summaries, client.model, runtime)
    else:
        doc_description = f"{file_path.name} 的结构化索引文档。"
    return save_structured_docx_document(
        client,
        file_path,
        pages,
        structure,
        doc_description,
        line_count=paragraph_count,
        index_mode="structured",
    )


# simple 模式索引入口：读页 -> 过滤稀疏页 -> 分块摘要 -> 持久化。
async def index_pdf_with_simple_mode_async(
    client: PageIndexClient,
    file_path: Path,
    chunk_pages: int,
    overlap_ratio: float,
    runtime: AssistantRuntime,
    llm_concurrency: int = DEFAULT_INDEX_LLM_CONCURRENCY,
    summary_enabled: bool = True,
    table_parse_mode: str = "auto",
    node_max_tokens: int = 512,
    max_tree_depth: int = 3,
) -> str:
    """simple 模式的索引入口：读页、过滤、摘要、落盘。"""
    log_event("pdf_index_mode_selected", mode="simple", file_name=file_path.name)
    async with runtime.pdf_io_semaphore:
        pages = await asyncio.to_thread(read_pdf_pages, file_path, table_parse_mode=table_parse_mode)
    pages, stats = filter_sparse_pages(pages)
    log_event(
        "pdf_page_filter",
        file_name=file_path.name,
        total_pages=stats["total_pages"],
        kept_pages=stats["kept_pages"],
        dropped_pages=stats["dropped_pages"],
    )
    structure, doc_description = await build_simple_pdf_structure_async(
        file_path=file_path,
        pages=pages,
        model=client.model,
        chunk_pages=chunk_pages,
        overlap_ratio=overlap_ratio,
        runtime=runtime,
        llm_concurrency=llm_concurrency,
        summary_enabled=summary_enabled,
        node_max_tokens=node_max_tokens,
        max_tree_depth=max_tree_depth,
    )
    return save_simplified_pdf_document(client, file_path, pages, structure, doc_description, "simple")


# hybrid 模式索引入口：流程同 simple，但结构构建策略更偏章节语义。
async def index_pdf_with_hybrid_mode_async(
    client: PageIndexClient,
    file_path: Path,
    chunk_pages: int,
    overlap_ratio: float,
    runtime: AssistantRuntime,
    llm_concurrency: int = DEFAULT_INDEX_LLM_CONCURRENCY,
    summary_enabled: bool = True,
    table_parse_mode: str = "auto",
    node_max_tokens: int = 512,
    max_tree_depth: int = 3,
) -> str:
    """hybrid 模式的索引入口：流程和 simple 类似，但优先按章节构建结构。"""
    log_event("pdf_index_mode_selected", mode="hybrid", file_name=file_path.name)
    async with runtime.pdf_io_semaphore:
        pages = await asyncio.to_thread(read_pdf_pages, file_path, table_parse_mode=table_parse_mode)
    pages, stats = filter_sparse_pages(pages)
    log_event(
        "pdf_page_filter",
        file_name=file_path.name,
        total_pages=stats["total_pages"],
        kept_pages=stats["kept_pages"],
        dropped_pages=stats["dropped_pages"],
    )
    structure, doc_description = await build_hybrid_pdf_structure_async(
        file_path=file_path,
        pages=pages,
        model=client.model,
        chunk_pages=chunk_pages,
        overlap_ratio=overlap_ratio,
        runtime=runtime,
        llm_concurrency=llm_concurrency,
        summary_enabled=summary_enabled,
        node_max_tokens=node_max_tokens,
        max_tree_depth=max_tree_depth,
    )
    return save_simplified_pdf_document(client, file_path, pages, structure, doc_description, "hybrid")


# 文件指纹用于“同路径文档是否变化”的快速判断（轻量，不读全文件）。
def compute_file_fingerprint(file_path: Path) -> dict[str, Any]:
    """计算文件指纹，用于快速判断缓存是否还能复用。"""
    stat = file_path.stat()
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        head = f.read(8192)
        md5.update(head)
    return {
        "file_size": int(stat.st_size),
        "file_mtime": int(stat.st_mtime),
        "file_head_md5": md5.hexdigest(),
    }


def attach_file_fingerprint(doc_data: dict, fingerprint: dict[str, Any]) -> dict:
    """把文件指纹补到文档数据里，并顺手更新内容哈希。"""
    payload = dict(doc_data)
    payload.update(fingerprint)
    payload["content_hash"] = compute_document_content_hash(payload)
    return payload


# 捕获第三方索引器的 stdout/stderr，避免污染主日志并保留调试线索。
class BoundedLogBuffer:
    """限制日志捕获长度，避免第三方索引器输出刷屏。"""
    def __init__(self, max_chars: int = 65536):
        """初始化一个带最大容量限制的文本缓冲区。"""
        self.max_chars = max(1024, int(max_chars))
        self._chunks: list[str] = []
        self._size = 0
        self.truncated = False

    def write(self, text: str) -> int:
        """写入文本；超出上限时只保留前半部分并标记截断。"""
        if not text:
            return 0
        remaining = self.max_chars - self._size
        if remaining <= 0:
            self.truncated = True
            return len(text)
        piece = text[:remaining]
        if len(text) > remaining:
            self.truncated = True
        self._chunks.append(piece)
        self._size += len(piece)
        return len(text)

    def flush(self):
        """兼容 file-like 接口；这里不需要真正 flush。"""
        return None

    def getvalue(self) -> str:
        """返回当前缓冲区里已经收集到的全部文本。"""
        return "".join(self._chunks)


def index_with_captured_output(client: PageIndexClient, file_path: Path) -> str:
    """调用底层索引器并捕获其标准输出，避免污染主日志。"""
    stream = BoundedLogBuffer(max_chars=65536)
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        doc_id = client.index(str(file_path))
    raw = stream.getvalue().strip()
    if raw:
        for line in raw.splitlines()[:80]:
            logger.debug("index_backend_log file=%s text=%s", file_path.name, line.strip())
        if stream.truncated:
            logger.debug("index_backend_log_truncated file=%s", file_path.name)
    return doc_id


# 允许 mtime 漂移但内容头+大小不变时仍视为命中，避免无意义重建。
def is_fingerprint_match(doc: dict, fingerprint: dict[str, Any]) -> bool:
    """判断缓存文档与当前文件指纹是否仍然一致。"""
    if not doc:
        return False
    size_match = doc.get("file_size") == fingerprint.get("file_size")
    if "file_head_md5" in doc and "file_head_md5" in fingerprint:
        head_match = doc.get("file_head_md5") == fingerprint.get("file_head_md5")
        # Accept incremental hit when content head + size are stable even if mtime changes.
        if size_match and head_match:
            return True
    mtime_match = doc.get("file_mtime") == fingerprint.get("file_mtime")
    if not (size_match and mtime_match):
        return False
    if "file_head_md5" in doc and "file_head_md5" in fingerprint:
        return doc.get("file_head_md5") == fingerprint.get("file_head_md5")
    if "file_md5" in doc and "file_md5" in fingerprint:
        return doc.get("file_md5") == fingerprint.get("file_md5")
    return True


# 某些旧缓存缺少指纹字段时，尝试从 PostgreSQL 全量载荷回填。
def hydrate_cached_doc_fingerprint(client: PageIndexClient, doc_id: str, doc: dict) -> dict:
    """旧缓存缺少指纹字段时，尝试从 PostgreSQL 全量载荷回填。"""
    if not isinstance(doc, dict):
        return doc
    if doc.get("file_size") is not None and doc.get("file_mtime") is not None:
        return doc
    store = getattr(client, "_store", None)
    if store is not None:
        try:
            full = store.load_full_doc(doc_id)
            if isinstance(full, dict):
                for field in ("file_size", "file_mtime", "file_head_md5", "file_md5", "content_hash"):
                    if field in full:
                        doc[field] = full.get(field)
                if full.get("path"):
                    doc["path"] = full.get("path")
                client.documents[doc_id] = doc
                if doc.get("file_size") is not None and doc.get("file_mtime") is not None:
                    return doc
        except Exception as exc:
            logger.debug("Failed to hydrate cached fingerprint from store for doc_id=%s: %s", doc_id, exc)
    return doc


# 按“同绝对路径 + 指纹匹配”查找可复用缓存文档。
def find_cached_doc_id(client: PageIndexClient, file_path: Path, fingerprint: dict[str, Any] | None = None) -> str | None:
    """按绝对路径和指纹查找可复用的缓存文档。"""
    absolute_path = Path(file_path).resolve()
    candidates: list[tuple[str, dict]] = []
    for doc_id, doc in client.documents.items():
        doc = hydrate_cached_doc_fingerprint(client, doc_id, doc)
        try:
            same_path = Path(doc.get("path", "")).resolve() == absolute_path
        except Exception:
            same_path = False
        if same_path:
            candidates.append((doc_id, doc))

    if not candidates:
        return None
    if fingerprint is None:
        return candidates[0][0]

    for doc_id, doc in candidates:
        if is_fingerprint_match(doc, fingerprint):
            return doc_id

    log_event(
        "cache_fingerprint_mismatch",
        file_name=file_path.name,
        candidate_doc_ids=[item[0] for item in candidates],
    )
    return None


# 清理同路径重复文档，保持 PostgreSQL 缓存稳定。
def _cleanup_duplicate_docs(client: PageIndexClient, kept_doc_id: str, duplicate_doc_ids: list[str]):
    """删除同路径重复缓存，只保留一个文档及其元信息。"""
    if not duplicate_doc_ids:
        return
    store = getattr(client, "_store", None)
    for duplicate_id in duplicate_doc_ids:
        client.documents.pop(duplicate_id, None)
        if store is not None:
            try:
                store.delete_doc(duplicate_id)
                continue
            except Exception as exc:
                log_event(
                    "postgres_duplicate_remove_failed",
                    level=logging.WARNING,
                    path=duplicate_id,
                    error=str(exc),
                )


# 将新文档并入主 client，必要时覆盖同路径旧文档并做去重清理。
def persist_indexed_document(
    client: PageIndexClient,
    doc_data: dict,
    *,
    reuse_existing_same_path: bool = True,
) -> str:
    """把新索引结果并入主 client。

    By default, same-path payloads reuse an existing doc_id to keep historical
    CLI behavior. API ingestion can set reuse_existing_same_path=False to force
    a new persisted doc_id per submission.
    """
    new_doc = dict(doc_data)
    existing_same_path: list[str] = []
    try:
        new_path = Path(new_doc.get("path", "")).resolve()
    except Exception:
        new_path = None

    if new_path is not None:
        for existing_id, existing_doc in client.documents.items():
            try:
                if Path(existing_doc.get("path", "")).resolve() == new_path:
                    existing_same_path.append(existing_id)
            except Exception:
                continue

    if existing_same_path and reuse_existing_same_path:
        doc_id = existing_same_path[0]
        new_doc["id"] = doc_id
    else:
        doc_id = new_doc.get("id") or str(uuid.uuid4())
        while doc_id in client.documents:
            doc_id = str(uuid.uuid4())
        new_doc["id"] = doc_id
    try:
        DocumentInfo.model_validate(
            {
                "id": new_doc["id"],
                "type": new_doc.get("type", ""),
                "path": new_doc.get("path", ""),
                "doc_name": new_doc.get("doc_name", ""),
                "doc_description": new_doc.get("doc_description", ""),
                "page_count": new_doc.get("page_count"),
                "line_count": new_doc.get("line_count"),
                "index_mode": new_doc.get("index_mode", "standard"),
                "content_hash": new_doc.get("content_hash", ""),
            }
        )
    except ValidationError as exc:
        logger.exception("Invalid document payload before persist: doc_id=%s", doc_id)
        raise RuntimeError(f"Invalid document payload: {exc}") from exc
    client.documents[doc_id] = new_doc
    if getattr(client, "_store", None) is not None:
        client._save_doc(doc_id)
    # Keep only one document per path when reuse mode is enabled.
    if reuse_existing_same_path and existing_same_path and len(existing_same_path) > 1:
        duplicates = [item for item in existing_same_path if item != doc_id]
        _cleanup_duplicate_docs(client, doc_id, duplicates)
        log_event(
            "postgres_same_path_dedup",
            kept_doc_id=doc_id,
            removed_doc_ids=duplicates,
            path=new_doc.get("path", ""),
        )
    return doc_id


# 单文档索引总入口：
# - PDF 走模式决策（standard/simple/hybrid）
# - 非 PDF 走原始 index 并带重试
async def load_or_index_document(
    model: str,
    retrieve_model: str,
    file_path: Path,
    options: IndexingOptions,
    runtime: AssistantRuntime,
    fingerprint: dict[str, Any] | None = None,
) -> dict | None:
    """单文档索引总入口：自动决定索引模式并返回文档载荷。"""
    worker_client = PageIndexClient(
        model=model,
        retrieve_model=retrieve_model,
        # Worker client is intentionally memory-only; merged payload is persisted by the main PostgreSQL client.
        storage_backend="memory",
    )
    if fingerprint is None:
        async with runtime.pdf_io_semaphore:
            fingerprint = await asyncio.to_thread(compute_file_fingerprint, file_path)

    if file_path.suffix.lower() == ".pdf":
        try:
            async with runtime.pdf_io_semaphore:
                page_count = await asyncio.to_thread(get_pdf_page_count, file_path)
        except Exception as exc:
            log_event("pdf_unreadable_skipped", level=logging.WARNING, file_name=file_path.name, error=str(exc))
            return None

        async with runtime.pdf_io_semaphore:
            complexity = await asyncio.to_thread(
                analyze_pdf_complexity,
                file_path,
                options.complexity_sample_pages,
            )
        selected_mode = choose_pdf_index_mode(
            page_count=page_count,
            complexity=complexity,
            soft_threshold=options.simple_index_page_threshold,
            hard_threshold=options.long_pdf_hard_threshold,
            forced_mode=options.long_pdf_mode,
        )
        log_event(
            "pdf_index_mode_decision",
            file_name=file_path.name,
            selected_mode=selected_mode,
            page_count=page_count,
            avg_chars=round(float(complexity["avg_chars"]), 2),
            empty_ratio=round(float(complexity["empty_ratio"]), 4),
            heading_ratio=round(float(complexity["heading_ratio"]), 4),
        )

        if selected_mode == "hybrid":
            try:
                doc_id = await index_pdf_with_hybrid_mode_async(
                    worker_client,
                    file_path,
                    options.hybrid_index_chunk_pages,
                    options.chunk_overlap_ratio,
                    runtime=runtime,
                    llm_concurrency=max(1, options.index_concurrency * 2),
                    summary_enabled=options.summary_enabled,
                    table_parse_mode=options.table_parse_mode,
                    node_max_tokens=options.node_max_tokens,
                    max_tree_depth=options.max_tree_depth,
                )
                return attach_file_fingerprint(worker_client.documents.get(doc_id, {}), fingerprint)
            except Exception as exc:
                log_event(
                    "pdf_hybrid_failed_fallback_simple",
                    level=logging.WARNING,
                    file_name=file_path.name,
                    error=str(exc),
                )
                try:
                    doc_id = await index_pdf_with_simple_mode_async(
                        worker_client,
                        file_path,
                        options.simple_index_chunk_pages,
                        options.chunk_overlap_ratio,
                        runtime=runtime,
                        llm_concurrency=max(1, options.index_concurrency * 2),
                        summary_enabled=options.summary_enabled,
                        table_parse_mode=options.table_parse_mode,
                        node_max_tokens=options.node_max_tokens,
                        max_tree_depth=options.max_tree_depth,
                    )
                    return attach_file_fingerprint(worker_client.documents.get(doc_id, {}), fingerprint)
                except Exception as inner_exc:
                    log_event(
                        "pdf_simple_fallback_failed",
                        level=logging.ERROR,
                        file_name=file_path.name,
                        error=str(inner_exc),
                    )
                    return None

        if selected_mode == "simple":
            try:
                doc_id = await index_pdf_with_simple_mode_async(
                    worker_client,
                    file_path,
                    options.simple_index_chunk_pages,
                    options.chunk_overlap_ratio,
                    runtime=runtime,
                    llm_concurrency=max(1, options.index_concurrency * 2),
                    summary_enabled=options.summary_enabled,
                    table_parse_mode=options.table_parse_mode,
                    node_max_tokens=options.node_max_tokens,
                    max_tree_depth=options.max_tree_depth,
                )
                return attach_file_fingerprint(worker_client.documents.get(doc_id, {}), fingerprint)
            except Exception as exc:
                log_event("pdf_simple_mode_failed", level=logging.ERROR, file_name=file_path.name, error=str(exc))
                return None

    if file_path.suffix.lower() == ".docx":
        try:
            doc_id = await index_docx_with_structured_mode_async(
                worker_client,
                file_path,
                runtime=runtime,
                llm_concurrency=max(1, options.index_concurrency * 2),
                summary_enabled=options.summary_enabled,
                max_tree_depth=options.max_tree_depth,
            )
            return attach_file_fingerprint(worker_client.documents.get(doc_id, {}), fingerprint)
        except Exception as exc:
            log_event("docx_structured_mode_failed", level=logging.ERROR, file_name=file_path.name, error=str(exc))
            return None

    for attempt in range(1, MAX_INDEX_ATTEMPTS + 1):
        try:
            log_event(
                "document_index_attempt",
                file_name=file_path.name,
                attempt=attempt,
                max_attempts=MAX_INDEX_ATTEMPTS,
            )
            doc_id = await asyncio.to_thread(index_with_captured_output, worker_client, file_path)
            log_event("document_index_complete", file_name=file_path.name, doc_id=doc_id, attempt=attempt)
            return attach_file_fingerprint(worker_client.documents.get(doc_id, {}), fingerprint)
        except Exception as exc:
            log_event(
                "document_index_attempt_failed",
                level=logging.WARNING,
                file_name=file_path.name,
                attempt=attempt,
                error=str(exc),
            )
            if attempt >= MAX_INDEX_ATTEMPTS:
                log_event(
                    "document_index_skipped_after_retries",
                    level=logging.ERROR,
                    file_name=file_path.name,
                    max_attempts=MAX_INDEX_ATTEMPTS,
                )
                return None
    return None


# 多文档并发索引入口：负责缓存命中、并发控制、内容去重、错误收集。
async def index_documents_async(
    client: PageIndexClient,
    file_paths: list[Path],
    options: IndexingOptions,
    runtime: AssistantRuntime,
) -> tuple[list[str], list[dict]]:
    """多文档并发索引入口，负责缓存命中、去重和错误收集。"""
    semaphore = asyncio.Semaphore(max(1, options.index_concurrency))
    merge_lock = asyncio.Lock()
    doc_ids: list[str] = []
    failed_docs: list[dict] = []
    content_hash_to_doc_id: dict[str, str] = {}

    for doc_id, doc in client.documents.items():
        content_hash = doc.get("content_hash")
        if isinstance(content_hash, str) and content_hash:
            content_hash_to_doc_id.setdefault(content_hash, doc_id)

    async def worker(file_path: Path):
        """处理单个文件：先查缓存，未命中再索引，再合并结果。"""
        nonlocal doc_ids, failed_docs
        started = time.perf_counter()
        absolute_path = Path(file_path).resolve()
        try:
            async with runtime.pdf_io_semaphore:
                fingerprint = await asyncio.to_thread(compute_file_fingerprint, absolute_path)
        except Exception as exc:
            log_event("file_fingerprint_failed", level=logging.WARNING, path=str(absolute_path), error=str(exc))
            fingerprint = None
        async with merge_lock:
            cached = find_cached_doc_id(client, absolute_path, fingerprint=fingerprint)
            if cached is not None:
                log_event("cached_document_loaded", file_name=file_path.name, doc_id=cached)
                doc_ids.append(cached)
                cached_hash = client.documents.get(cached, {}).get("content_hash")
                if isinstance(cached_hash, str) and cached_hash:
                    content_hash_to_doc_id.setdefault(cached_hash, cached)
                runtime.stats["index_success"] = int(runtime.stats.get("index_success", 0)) + 1
                log_event(
                    "index_trace",
                    file_name=file_path.name,
                    mode="cache",
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                return

        async with semaphore:
            doc_payload = await load_or_index_document(
                model=client.model,
                retrieve_model=client.retrieve_model,
                file_path=absolute_path,
                options=options,
                runtime=runtime,
                fingerprint=fingerprint,
            )

        if doc_payload is None:
            async with merge_lock:
                failed_docs.append({"path": str(absolute_path)})
                runtime.stats["index_fail"] = int(runtime.stats.get("index_fail", 0)) + 1
                log_event(
                    "index_trace",
                    level=logging.WARNING,
                    file_name=file_path.name,
                    mode="failed",
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            return

        async with merge_lock:
            content_hash = doc_payload.get("content_hash")
            if isinstance(content_hash, str) and content_hash:
                duplicate_doc_id = content_hash_to_doc_id.get(content_hash) or find_doc_id_by_content_hash(client, content_hash)
                if duplicate_doc_id:
                    log_event(
                        "duplicate_document_skipped",
                        file_name=absolute_path.name,
                        existing_doc_id=duplicate_doc_id,
                    )
                    doc_ids.append(duplicate_doc_id)
                    runtime.stats["index_success"] = int(runtime.stats.get("index_success", 0)) + 1
                    log_event(
                        "index_trace",
                        file_name=file_path.name,
                        mode="duplicate",
                        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                    )
                    return
            # Double check in case another task finished this file first.
            cached = find_cached_doc_id(client, absolute_path, fingerprint=fingerprint)
            if cached is not None:
                doc_ids.append(cached)
                runtime.stats["index_success"] = int(runtime.stats.get("index_success", 0)) + 1
                log_event(
                    "index_trace",
                    file_name=file_path.name,
                    mode="cache-race",
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                return
            merged_id = persist_indexed_document(client, doc_payload)
            if isinstance(content_hash, str) and content_hash:
                content_hash_to_doc_id[content_hash] = merged_id
            doc_ids.append(merged_id)
            runtime.stats["index_success"] = int(runtime.stats.get("index_success", 0)) + 1
            log_event(
                "index_trace",
                file_name=file_path.name,
                mode="indexed",
                elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            )

    tasks = [asyncio.create_task(worker(path)) for path in file_paths]
    await asyncio.gather(*tasks)
    return doc_ids, failed_docs


async def index_documents(
    client: PageIndexClient,
    file_paths: list[Path],
    options: IndexingOptions,
    runtime: AssistantRuntime,
) -> tuple[list[str], list[dict]]:
    """index_documents_async 的简单包装。"""
    return await index_documents_async(
        client,
        file_paths,
        options=options,
        runtime=runtime,
    )


def build_document_catalog(client: PageIndexClient, allowed_doc_ids: set[str] | None = None) -> list[dict]:
    """构建当前文档目录，供日志、缓存键和问答流程使用。"""
    return list_documents_tool(client, allowed_doc_ids=allowed_doc_ids)


def _sanitize_session_token(raw: str | None) -> str:
    token = str(raw or "").strip().lower()
    if not token:
        return ""
    token = re.sub(r"[^a-z0-9_-]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-_")
    if not token:
        return ""
    return token[:48]


def _resolve_agent_graph_thread_id(client: PageIndexClient, session_id: str | None = None) -> str:
    runtime = get_client_runtime(client)
    explicit_token = _sanitize_session_token(session_id)
    if explicit_token:
        thread_id = f"agent-{explicit_token}"
        setattr(runtime, "agent_graph_thread_id", thread_id)
        return thread_id

    cached_thread_id = str(getattr(runtime, "agent_graph_thread_id", "") or "").strip()
    if cached_thread_id:
        return cached_thread_id

    generated = f"agent-{uuid.uuid4().hex[:12]}"
    setattr(runtime, "agent_graph_thread_id", generated)
    return generated


def _needs_dialogue_context(question: str) -> bool:
    text = str(question or "").strip()
    if not text:
        return False
    deictic_patterns = (
        "这个",
        "那个",
        "这位",
        "那位",
        "这家",
        "那家",
        "这件",
        "那件",
        "这类",
        "那类",
        "这种",
        "那种",
        "这样",
        "那样",
        "上面说的",
        "前面说的",
        "刚才说的",
        "之前说的",
        "前者",
        "后者",
        "它",
        "他",
        "她",
    )
    if any(token in text for token in deictic_patterns):
        return True
    # Short follow-up questions are often context-dependent.
    if len(text) <= 14 and ("?" in text or "？" in text or "吗" in text or "呢" in text):
        return True
    return False


def _build_recent_dialogue_context(runtime: AssistantRuntime, max_turns: int = 3) -> str:
    turns = list(getattr(runtime, "recent_turns", []) or [])
    if not turns:
        return ""
    sliced = turns[-max(1, int(max_turns)) :]
    lines: list[str] = []
    for turn in sliced:
        q = str(turn.get("question", "") or "").strip()
        a = str(turn.get("answer", "") or "").strip()
        if not q:
            continue
        if len(a) > 180:
            a = a[:180] + "..."
        lines.append(f"用户：{q}")
        if a:
            lines.append(f"助手：{a}")
    return "\n".join(lines).strip()


def _augment_query_with_context(question: str, dialogue_context: str) -> str:
    """Inject short conversation context for deictic follow-up questions."""
    q = str(question or "").strip()
    if not q or not dialogue_context:
        return q
    return (
        "你正在进行多轮问答。请先完成指代消解，再基于文档工具检索回答。\n"
        "若“它/这个/前者/后者”等指代不明确，请优先结合最近对话中的最近明确实体。\n\n"
        f"最近对话：\n{dialogue_context}\n\n"
        f"当前问题：{q}"
    )


def _push_recent_turn(runtime: AssistantRuntime, question: str, answer: str, max_turns: int = 8) -> None:
    q = str(question or "").strip()
    a = str(answer or "").strip()
    if not q:
        return
    turns = list(getattr(runtime, "recent_turns", []) or [])
    turns.append({"question": q, "answer": a})
    if len(turns) > max(1, int(max_turns)):
        turns = turns[-max(1, int(max_turns)) :]
    setattr(runtime, "recent_turns", turns)


# QA 执行统一入口：
# 1) 计算缓存键并尝试命中
# 2) agent / agent-graph 分流（pipeline 兼容映射到 agent）
# 3) 无信息答案时执行通用知识兜底
async def execute_qa(
    client: PageIndexClient,
    question: str,
    *,
    qa_mode: str,
    verbose: bool,
    stream_output: bool,
    top_k_docs: int,
    allowed_doc_ids: set[str] | None,
    bm25_prefilter: BM25Prefilter | None,
    bm25_prefilter_top_k: int,
    session_id: str | None = None,
) -> dict:
    """统一问答入口：查缓存、按模式执行、必要时做通用知识兜底。"""
    if qa_mode == "pipeline":
        qa_mode = "agent"

    runtime = get_client_runtime(client)
    runtime.stats["qa_requests"] = int(runtime.stats.get("qa_requests", 0)) + 1
    started = time.perf_counter()
    agent_graph_thread_id = _resolve_agent_graph_thread_id(client, session_id=session_id) if qa_mode == "agent-graph" else ""
    session_scope = f"{qa_mode}:{agent_graph_thread_id}" if agent_graph_thread_id else (_sanitize_session_token(session_id) if session_id else "")
    recent_dialogue_context = _build_recent_dialogue_context(runtime) if _needs_dialogue_context(question) else ""
    effective_question = _augment_query_with_context(question, recent_dialogue_context)
    context_scope = (
        hashlib.md5(recent_dialogue_context.encode("utf-8", errors="ignore")).hexdigest()
        if recent_dialogue_context
        else ""
    )
    catalog_signature = compute_catalog_signature(
        build_document_catalog(client, allowed_doc_ids=allowed_doc_ids)
    )
    cache_key = build_qa_cache_key(
        question=question,
        qa_mode=qa_mode,
        top_k_docs=top_k_docs,
        allowed_doc_ids=allowed_doc_ids,
        bm25_prefilter_top_k=bm25_prefilter_top_k,
        retrieve_model=str(getattr(client, "retrieve_model", "") or ""),
        catalog_signature=catalog_signature,
        session_scope=session_scope,
        context_scope=context_scope,
    )
    cached_result = runtime.qa_result_cache.get(cache_key)
    if cached_result is not None:
        _push_recent_turn(runtime, question, str(cached_result.get("answer", "") or ""))
        log_event("qa_cache_hit", qa_mode=qa_mode)
        log_runtime_metrics(runtime, "qa_finished", qa_mode=qa_mode, cache_hit=True, elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
        return cached_result

    if qa_mode == "agent-graph":
        if not langgraph_available():
            log_event("langgraph_unavailable_fallback_agent", level=logging.WARNING, qa_mode=qa_mode)
            qa_mode = "agent"
        else:
            graph_result = await run_langgraph_agent_qa(
                client=client,
                query=effective_question,
                prompts=runtime.prompts,
                retrieve_model=client.retrieve_model,
                top_k_docs=top_k_docs,
                verbose=verbose,
                stream_output=stream_output,
                allowed_doc_ids=allowed_doc_ids,
                bm25_prefilter=bm25_prefilter,
                bm25_prefilter_top_k=bm25_prefilter_top_k,
                thread_id=agent_graph_thread_id or _resolve_agent_graph_thread_id(client, session_id=session_id),
                log_event=log_event,
            )
            if not isinstance(graph_result, dict):
                graph_result = {}
            answer_text = str(graph_result.get("answer", "") or DEFAULT_NO_INFO_ANSWER)
            used_general_knowledge_fallback = False
            if is_no_info_answer(answer_text):
                answer_text = await generate_general_knowledge_answer(
                    client,
                    question,
                    dialogue_context=recent_dialogue_context,
                )
                used_general_knowledge_fallback = True
            final = {
                "answer": answer_text,
                "selected_documents": [] if used_general_knowledge_fallback else graph_result.get("selected_documents", []),
                "evidence": [] if used_general_knowledge_fallback else graph_result.get("evidence", []),
                "citations": [] if used_general_knowledge_fallback else graph_result.get("citations", []),
                "evidence_check": graph_result.get("evidence_check", {}),
                "retrieval_attempts": graph_result.get("retrieval_attempts", 1),
                "mode": "agent-graph",
            }
            runtime.qa_result_cache.set(cache_key, final)
            _push_recent_turn(runtime, question, answer_text)
            log_runtime_metrics(runtime, "qa_finished", qa_mode=qa_mode, cache_hit=False, elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
            return final

    agent_result = await query_agent_multi_doc(
        client,
        effective_question,
        log_event=log_event,
        verbose=verbose,
        stream_output=stream_output,
        top_k_docs=top_k_docs,
        allowed_doc_ids=allowed_doc_ids,
        bm25_prefilter=bm25_prefilter,
        bm25_prefilter_top_k=bm25_prefilter_top_k,
        return_details=True,
    )
    if not isinstance(agent_result, dict):
        agent_result = {"answer": agent_result or DEFAULT_NO_INFO_ANSWER}
    answer_text = str(agent_result.get("answer", "") or DEFAULT_NO_INFO_ANSWER)
    used_general_knowledge_fallback = False
    if is_no_info_answer(answer_text):
        answer_text = await generate_general_knowledge_answer(
            client,
            question,
            dialogue_context=recent_dialogue_context,
        )
        used_general_knowledge_fallback = True
    final = {
        "answer": answer_text,
        "selected_documents": [] if used_general_knowledge_fallback else agent_result.get("selected_documents", []),
        "evidence": [] if used_general_knowledge_fallback else agent_result.get("evidence", []),
        "citations": [] if used_general_knowledge_fallback else agent_result.get("citations", []),
        "mode": "agent",
    }
    runtime.qa_result_cache.set(cache_key, final)
    _push_recent_turn(runtime, question, answer_text)
    log_runtime_metrics(runtime, "qa_finished", qa_mode=qa_mode, cache_hit=False, elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
    return final

# 主业务流程：
# 参数生效 -> PostgreSQL 缓存安全清理 -> 按需建索引 -> 构建预筛 -> 进入问答模式。
async def run_pipeline(args: argparse.Namespace):
    """主业务流程：加载配置、索引文档、构建检索器并进入问答模式。"""
    set_tracing_disabled(True)

    data_dir = Path(args.data_dir).expanduser().resolve()
    storage_backend = str(getattr(args, "storage_backend", "") or "").strip().lower()
    if storage_backend != "postgres":
        raise UserInputError("Unsupported storage backend. Use --storage-backend=postgres.")

    explicit_dsn = str(getattr(args, "postgres_dsn", "") or "").strip()
    env_postgres_dsn = str(os.getenv("POSTGRES_DSN", "") or "").strip()
    env_da_postgres_dsn = str(os.getenv("DA_POSTGRES_DSN", "") or "").strip()
    env_dsn = env_postgres_dsn or env_da_postgres_dsn
    effective_dsn = explicit_dsn or env_dsn
    dsn_source = None
    if storage_backend == "postgres":
        if not effective_dsn:
            raise UserInputError("PostgreSQL DSN is required. Use --postgres-dsn or POSTGRES_DSN/DA_POSTGRES_DSN.")
        dsn_source = "arg" if explicit_dsn else ("POSTGRES_DSN" if env_postgres_dsn else "DA_POSTGRES_DSN")

    client = PageIndexClient(
        model=args.model,
        retrieve_model=args.retrieve_model,
        storage_backend="postgres",
        postgres_dsn=effective_dsn or None,
        user_id=str(os.getenv("DA_USER_ID", "") or os.getenv("PAGEINDEX_USER_ID", "") or "system"),
        # Keep storage scope stable across chat sessions; session_id is used for QA memory only.
        session_id=None,
    )
    runtime = AssistantRuntime(
        prompts=build_prompt_set_from_args(
            args,
            default_doc_selection_prompt=DEFAULT_DOC_SELECTION_PROMPT,
            default_page_selection_prompt=DEFAULT_PAGE_SELECTION_PROMPT,
            default_answer_prompt=DEFAULT_ANSWER_PROMPT,
            default_agent_system_prompt=DEFAULT_AGENT_SYSTEM_PROMPT,
        ),
        pdf_io_concurrency=max(1, int(args.pdf_io_concurrency)),
    )
    setattr(client, "_assistant_runtime", runtime)

    logger.debug("Using index model: %s", client.model)
    logger.debug("Using retrieve model: %s", client.retrieve_model)
    logger.debug("Storage backend: postgres; dsn_source=%s", dsn_source)
    logger.debug("Loaded %s cached document(s) from PostgreSQL storage.", len(client.documents))

    raw_delete_doc_ids = _parse_cli_list_values(getattr(args, "delete_doc_id", []))
    raw_delete_files = _parse_cli_list_values(getattr(args, "delete_file", []))
    if raw_delete_doc_ids or raw_delete_files:
        target_doc_ids, unresolved = _resolve_delete_doc_ids(
            client,
            raw_doc_ids=raw_delete_doc_ids,
            raw_files=raw_delete_files,
        )
        deleted_count = _delete_docs(client, target_doc_ids)
        if deleted_count > 0:
            logger.info("Deleted %s document(s) from %s storage.", deleted_count, storage_backend)
        else:
            logger.info("No documents matched delete arguments.")
        if unresolved:
            logger.warning("Delete targets not found: %s", ", ".join(unresolved))
        if getattr(args, "delete_only", False):
            return
    elif getattr(args, "delete_only", False):
        logger.info("Delete-only requested but no delete arguments were provided.")
        return

    if getattr(args, "prune_storage", False):
        if not data_dir.exists():
            raise FileNotFoundError(f"Data directory not found: {data_dir}")
        removed_untrusted = prune_untrusted_cached_documents(client, data_dir)
        if removed_untrusted > 0:
            logger.warning("Removed %s cached document(s) outside data_dir for security.", removed_untrusted)
        else:
            logger.info("Storage prune completed. No out-of-scope cached documents were removed.")
    else:
        logger.debug("Storage prune skipped. Pass --prune-storage to remove cached documents outside data_dir.")

    failed_docs: list[dict] = []
    if getattr(args, "index_missing", False):
        if not data_dir.exists():
            raise FileNotFoundError(f"Data directory not found: {data_dir}")
        file_paths = scan_documents(data_dir)
        index_file_arg = str(getattr(args, "index_file", "") or "").strip()
        if index_file_arg:
            requested_path = Path(index_file_arg).expanduser()
            if not requested_path.is_absolute():
                requested_path = data_dir / requested_path
            requested_path = ensure_secure_source_file(requested_path, data_dir)
            if not requested_path.exists() or not requested_path.is_file():
                raise FileNotFoundError(f"Index file not found: {requested_path}")
            if requested_path.suffix.lower() not in {".pdf", ".docx", ".md", ".markdown"}:
                raise UserInputError(
                    f"Unsupported index file type: {requested_path.suffix}. Supported: .pdf, .docx, .md, .markdown."
                )
            file_paths = [requested_path]
            logger.info("Single-file indexing enabled: %s", requested_path)
        if not file_paths and not client.documents:
            raise FileNotFoundError(f"No supported documents found in: {data_dir}")
        logger.info("Discovered %s document(s) in: %s", len(file_paths), data_dir)

        indexing_options = build_indexing_options(args, logger)
        indexed_doc_ids, failed_docs = await index_documents(
            client,
            file_paths,
            options=indexing_options,
            runtime=runtime,
        )
        log_runtime_metrics(runtime, "indexing_finished", indexed_docs=len(indexed_doc_ids), failed_docs=len(failed_docs))
        if not indexed_doc_ids and not client.documents:
            raise RuntimeError("All documents failed to index.")
    else:
        logger.debug("Database-only QA mode enabled. Pass --index-missing to scan data_dir and build indexes into PostgreSQL.")

    catalog = build_document_catalog(client)
    if not catalog:
        raise RuntimeError(
            "No indexed documents available in PostgreSQL storage. "
            "Run once with --index-missing to build indexes from data_dir into PostgreSQL."
        )
    allowed_doc_ids = resolve_restricted_doc_ids(client, args.restrict_docs)
    visible_catalog = build_document_catalog(client, allowed_doc_ids=allowed_doc_ids)

    display_names: list[str] = []
    seen_display: set[str] = set()
    for item in visible_catalog:
        raw_name = str(item.get("doc_name", "") or "").strip()
        if not raw_name:
            continue
        # 面向用户展示时去掉扩展名并做去重，减少视觉噪音。
        pretty_name = Path(raw_name).stem
        if pretty_name in seen_display:
            continue
        seen_display.add(pretty_name)
        display_names.append(pretty_name)
    if display_names:
        if allowed_doc_ids is None:
            print("思考：资料库中有以下文档：")
        else:
            print("思考：当前问答范围限定为以下文档：")
        print("\n".join(f"- {name}" for name in display_names))
    else:
        print("思考：资料库中暂无可用文档。")
    if failed_docs:
        logger.debug("Skipped documents:\n%s", json.dumps(failed_docs, ensure_ascii=False, indent=2))

    if allowed_doc_ids is not None:
        logger.debug("Restricted document names for QA:\n%s", format_doc_names_for_log(visible_catalog))

    bm25_prefilter = None
    if args.bm25_prefilter_top_k > 0:
        bm25_tokenizer_backend = enable_bm25_zh_tokenizer()
        bm25_prefilter = get_or_build_bm25_prefilter(client, catalog)
        backend_name = "rank_bm25" if bm25_prefilter.backend is not None else "fallback-overlap"
        logger.debug(
            "BM25 prefilter enabled. top_k=%s, backend=%s, tokenizer=%s",
            args.bm25_prefilter_top_k,
            backend_name,
            bm25_tokenizer_backend,
        )
        if bm25_tokenizer_backend != "jieba":
            logger.warning("jieba is not installed; BM25 uses regex fallback tokenizer.")
    else:
        logger.debug("BM25 prefilter disabled.")
    logger.debug("QA mode: %s", args.qa_mode)

    qa_result = await run_qa_mode(
        args,
        client=client,
        execute_qa_fn=execute_qa,
        allowed_doc_ids=allowed_doc_ids,
        bm25_prefilter=bm25_prefilter,
    )
    if isinstance(qa_result, dict) and qa_result.get("answer"):
        answer_text = str(qa_result.get("answer", "") or "").strip()
        if answer_text.startswith(GENERAL_KNOWLEDGE_SOURCE_LABEL):
            print(f"\n最终回答：{answer_text}")
        _print_evidence_details(qa_result)
        logger.debug("answer=%s", qa_result["answer"])


# 程序入口：负责配置加载、日志初始化、异常分类退出码。
async def main():
    """程序入口：解析参数、初始化日志并启动主流程。"""
    parser = build_argument_parser(
        default_config_path=str(DEFAULT_CONFIG_PATH),
        default_data_dir=str(DEFAULT_DATA_DIR),
        default_simple_index_page_threshold=DEFAULT_SIMPLE_INDEX_PAGE_THRESHOLD,
        default_long_pdf_hard_threshold=DEFAULT_LONG_PDF_HARD_THRESHOLD,
        default_simple_index_chunk_pages=DEFAULT_SIMPLE_INDEX_CHUNK_PAGES,
        default_hybrid_index_chunk_pages=DEFAULT_HYBRID_INDEX_CHUNK_PAGES,
        default_complexity_sample_pages=DEFAULT_COMPLEXITY_SAMPLE_PAGES,
        default_index_concurrency=DEFAULT_INDEX_CONCURRENCY,
        default_pdf_io_concurrency=DEFAULT_PDF_IO_CONCURRENCY,
        default_bm25_prefilter_top_k=DEFAULT_BM25_PREFILTER_TOP_K,
        default_chunk_overlap_ratio=DEFAULT_CHUNK_OVERLAP_RATIO,
        default_log_level=DEFAULT_LOG_LEVEL,
        default_log_format=DEFAULT_LOG_FORMAT,
    )
    args = parser.parse_args()
    cli_provided = cli_provided_options()
    file_config = load_runtime_config(args.config)
    apply_file_config(
        args,
        file_config,
        cli_provided,
        default_doc_selection_prompt=DEFAULT_DOC_SELECTION_PROMPT,
        default_page_selection_prompt=DEFAULT_PAGE_SELECTION_PROMPT,
        default_answer_prompt=DEFAULT_ANSWER_PROMPT,
        default_agent_system_prompt=DEFAULT_AGENT_SYSTEM_PROMPT,
    )
    apply_env_overrides(args, cli_provided)
    setup_logging(args.log_level, args.log_format)
    _install_asyncio_shutdown_exception_filter()
    try:
        if file_config:
            logger.debug("Loaded runtime config from: %s", args.config)
        await run_pipeline(args)
    except PathSecurityError as exc:
        logger.error("Path security validation failed: %s", exc)
        raise SystemExit(2) from exc
    except (FileNotFoundError, UserInputError, ValueError) as exc:
        logger.error("Input error: %s", exc)
        raise SystemExit(2) from exc
    except Exception as exc:
        logger.exception("Unexpected runtime error")
        logger.error("程序执行失败，请检查日志中的 exception 字段。")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    asyncio.run(main())



