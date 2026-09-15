# 模块说明：命令行参数、配置文件与环境变量合并逻辑。
import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from assistant_components import IndexingOptions
from core.common import log_event
from prompts.defaults import PromptSet


# 加载运行配置文件（YAML/JSON）；失败时返回空配置。
def load_runtime_config(config_path: str | None) -> dict[str, Any]:
    """加载 YAML/JSON 运行配置；失败时返回空配置。"""
    path = Path(config_path).expanduser() if config_path else None
    if path is None or not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml
            except Exception as exc:
                log_event("yaml_dependency_missing", level=logging.WARNING, error=str(exc))
                return {}
            payload = yaml.safe_load(text) or {}
        else:
            payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        log_event("runtime_config_load_failed", level=logging.ERROR, path=str(path), error=str(exc))
        return {}


# 提取命令行里显式传入的选项名，用于后续优先级合并。
def cli_provided_options(argv: list[str] | None = None) -> set[str]:
    """提取 CLI 显式提供的参数名，用于优先级合并。"""
    provided = set()
    for token in (argv or sys.argv[1:]):
        if token.startswith("--"):
            provided.add(token[2:].split("=", 1)[0].replace("-", "_"))
    return provided


# 将环境变量映射到参数对象（仅在 CLI 未显式提供时生效）。
def apply_env_overrides(args: argparse.Namespace, cli_provided: set[str]):
    """将环境变量映射到参数对象（CLI 显式参数优先）。"""
    env_map = {
        "DA_MODEL": "model",
        "DA_RETRIEVE_MODEL": "retrieve_model",
        "DA_DATA_DIR": "data_dir",
        "DA_INDEX_FILE": "index_file",
        "DA_STORAGE_BACKEND": "storage_backend",
        "DA_POSTGRES_DSN": "postgres_dsn",
        "DA_LOG_LEVEL": "log_level",
        "DA_LOG_FORMAT": "log_format",
        "DA_BM25_PREFILTER_TOP_K": "bm25_prefilter_top_k",
        "DA_INDEX_CONCURRENCY": "index_concurrency",
        "DA_SIMPLE_INDEX_PAGE_THRESHOLD": "simple_index_page_threshold",
        "DA_LONG_PDF_HARD_THRESHOLD": "long_pdf_hard_threshold",
        "DA_SIMPLE_INDEX_CHUNK_PAGES": "simple_index_chunk_pages",
        "DA_HYBRID_INDEX_CHUNK_PAGES": "hybrid_index_chunk_pages",
        "DA_COMPLEXITY_SAMPLE_PAGES": "complexity_sample_pages",
        "DA_CHUNK_OVERLAP_RATIO": "chunk_overlap_ratio",
        "DA_PDF_IO_CONCURRENCY": "pdf_io_concurrency",
        "DA_SESSION_ID": "session_id",
        "DA_SUMMARY_ENABLED": "summary_enabled",
        "DA_TABLE_PARSE_MODE": "table_parse_mode",
        "DA_NODE_MAX_TOKENS": "node_max_tokens",
        "DA_MAX_TREE_DEPTH": "max_tree_depth",
        "DA_ENABLE_OCR": "enable_ocr",
        "DA_OCR_MIN_CHARS": "ocr_min_chars",
        "DA_OCR_LANG": "ocr_lang",
        "DA_SUMMARY_CONCURRENCY": "summary_concurrency",
        "DA_INDEX_TIMEOUT_SECONDS": "index_timeout_seconds",
    }
    int_fields = {
        "bm25_prefilter_top_k",
        "index_concurrency",
        "simple_index_page_threshold",
        "long_pdf_hard_threshold",
        "simple_index_chunk_pages",
        "hybrid_index_chunk_pages",
        "complexity_sample_pages",
        "pdf_io_concurrency",
        "node_max_tokens",
        "max_tree_depth",
        "ocr_min_chars",
        "summary_concurrency",
        "index_timeout_seconds",
    }
    float_fields = {"chunk_overlap_ratio"}
    bool_fields = {"summary_enabled", "enable_ocr"}

    for env_name, arg_name in env_map.items():
        value = os.getenv(env_name)
        if value is None or value == "" or arg_name in cli_provided:
            continue
        if arg_name in int_fields:
            try:
                value = int(value)
            except ValueError:
                log_event("env_override_invalid_int", level=logging.WARNING, env_name=env_name, value=value)
                continue
        elif arg_name in float_fields:
            try:
                value = float(value)
            except ValueError:
                log_event("env_override_invalid_float", level=logging.WARNING, env_name=env_name, value=value)
                continue
        elif arg_name in bool_fields:
            value = str(value).strip().lower() in {"1", "true", "yes", "on"}
        setattr(args, arg_name, value)

    # Allow ARK/OpenAI-compatible single-model setups to drive both index and retrieve
    # defaults when dedicated DA_* model overrides are absent.
    model_fallback = (
        str(os.getenv("DA_MODEL", "") or "").strip()
        or str(os.getenv("ARK_MODEL", "") or "").strip()
        or str(os.getenv("ARK_ENDPOINT_ID", "") or "").strip()
    )
    if model_fallback:
        if "model" not in cli_provided and not str(os.getenv("DA_MODEL", "") or "").strip():
            setattr(args, "model", model_fallback)
        if "retrieve_model" not in cli_provided and not str(os.getenv("DA_RETRIEVE_MODEL", "") or "").strip():
            setattr(args, "retrieve_model", model_fallback)

    backend = str(getattr(args, "storage_backend", "") or "").strip().lower()
    if backend and backend != "postgres":
        raise ValueError("Only PostgreSQL storage backend is supported. Set DA_STORAGE_BACKEND=postgres.")


# 把配置文件内容应用到参数对象（CLI 参数优先）。
def apply_file_config(
    args: argparse.Namespace,
    config: dict[str, Any],
    cli_provided: set[str],
    *,
    default_doc_selection_prompt: str,
    default_page_selection_prompt: str,
    default_answer_prompt: str,
    default_agent_system_prompt: str,
):
    """把配置文件内容合并到 args（保留 CLI 优先级）。"""
    if not config:
        return
    defaults = config.get("defaults", {})
    if isinstance(defaults, dict):
        for key, value in defaults.items():
            if key in cli_provided:
                continue
            if hasattr(args, key):
                setattr(args, key, value)
    prompts = config.get("prompts", {})
    if isinstance(prompts, dict):
        setattr(args, "prompt_doc_selection", prompts.get("doc_selection", default_doc_selection_prompt))
        setattr(args, "prompt_page_selection", prompts.get("page_selection", default_page_selection_prompt))
        setattr(args, "prompt_answer", prompts.get("answer", default_answer_prompt))
        setattr(args, "prompt_agent_system", prompts.get("agent_system", default_agent_system_prompt))


# 从参数对象构建统一 PromptSet。
def build_prompt_set_from_args(
    args: argparse.Namespace,
    *,
    default_doc_selection_prompt: str,
    default_page_selection_prompt: str,
    default_answer_prompt: str,
    default_agent_system_prompt: str,
) -> PromptSet:
    """从参数对象构建统一 PromptSet。"""
    return PromptSet(
        doc_selection=getattr(args, "prompt_doc_selection", default_doc_selection_prompt),
        page_selection=getattr(args, "prompt_page_selection", default_page_selection_prompt),
        answer=getattr(args, "prompt_answer", default_answer_prompt),
        agent_system=getattr(args, "prompt_agent_system", default_agent_system_prompt),
    )


# 定义命令行参数。
def build_argument_parser(
    *,
    default_config_path: str,
    default_data_dir: str,
    default_simple_index_page_threshold: int,
    default_long_pdf_hard_threshold: int,
    default_simple_index_chunk_pages: int,
    default_hybrid_index_chunk_pages: int,
    default_complexity_sample_pages: int,
    default_index_concurrency: int,
    default_pdf_io_concurrency: int,
    default_bm25_prefilter_top_k: int,
    default_chunk_overlap_ratio: float,
    default_log_level: str,
    default_log_format: str,
) -> argparse.ArgumentParser:
    """定义命令行参数并返回解析器。"""
    parser = argparse.ArgumentParser(description="Multi-document local document assistant orchestrator based on PageIndex.")
    parser.add_argument("--config", default=default_config_path, help="Path to YAML/JSON runtime config file.")
    parser.add_argument("--data-dir", default=default_data_dir, help="Directory containing PDFs / Markdown documents.")
    parser.add_argument("--index-file", help="Index only one file (absolute path or path relative to data-dir).")
    parser.add_argument("--storage-backend", choices=["postgres"], default="postgres", help="Storage backend (PostgreSQL only).")
    parser.add_argument("--postgres-dsn", help="PostgreSQL DSN (required unless POSTGRES_DSN/DA_POSTGRES_DSN is set).")
    parser.add_argument("--model", help="Override the indexing model.")
    parser.add_argument("--retrieve-model", help="Override the retrieval / QA model.")
    parser.add_argument("--question", help="A single question to ask.")
    parser.add_argument("--session-id", help="Optional session identifier used to isolate multi-turn memory and agent-graph thread.")
    parser.add_argument("--top-k-docs", type=int, default=3, help="Number of candidate documents to route to.")
    parser.add_argument("--restrict-docs", help="Comma-separated doc_id or file names. If set, QA only uses these documents.")
    parser.add_argument("--simple-index-page-threshold", type=int, default=default_simple_index_page_threshold, help="Soft threshold for long PDF routing. Above this value, mode is chosen by complexity.")
    parser.add_argument("--long-pdf-hard-threshold", type=int, default=default_long_pdf_hard_threshold, help="Hard threshold for long PDFs. Very long PDFs prefer hybrid/simple mode in auto strategy.")
    parser.add_argument("--simple-index-chunk-pages", type=int, default=default_simple_index_chunk_pages, help="Chunk size in pages for simple PDF indexing.")
    parser.add_argument("--hybrid-index-chunk-pages", type=int, default=default_hybrid_index_chunk_pages, help="Chunk size in pages for hybrid PDF indexing fallback blocks.")
    parser.add_argument("--long-pdf-mode", choices=["auto", "standard", "hybrid", "simple"], default="auto", help="Long PDF indexing mode. 'auto' uses complexity-aware smooth downgrade.")
    parser.add_argument("--complexity-sample-pages", type=int, default=default_complexity_sample_pages, help="Number of sampled pages for complexity analysis.")
    parser.add_argument("--index-concurrency", type=int, default=default_index_concurrency, help="Maximum concurrent document indexing workers.")
    parser.add_argument("--pdf-io-concurrency", type=int, default=default_pdf_io_concurrency, help="Maximum concurrent PDF file IO/parse tasks.")
    parser.add_argument("--bm25-prefilter-top-k", type=int, default=default_bm25_prefilter_top_k, help="Top-K docs recalled by BM25 before Agent routing. Set <=0 to disable prefilter.")
    parser.add_argument("--chunk-overlap-ratio", type=float, default=default_chunk_overlap_ratio, help="Overlap ratio between page chunks for long-PDF indexing (0.0~0.5).")
    parser.add_argument("--summary-enabled", action=argparse.BooleanOptionalAction, default=True, help="Enable chunk summaries and document-level description generation.")
    parser.add_argument("--table-parse-mode", choices=["off", "auto"], default="auto", help="PDF table extraction mode: off/auto.")
    parser.add_argument("--enable-ocr", action=argparse.BooleanOptionalAction, default=False, help="Enable OCR fallback for scanned PDF pages when extracted text is too short.")
    parser.add_argument("--ocr-min-chars", type=int, default=20, help="Trigger OCR fallback when extracted page text is shorter than this threshold.")
    parser.add_argument("--ocr-lang", default="chi_sim+eng", help="pytesseract language pack string used for OCR fallback.")
    parser.add_argument("--node-max-tokens", type=int, default=512, help="Approximate max tokens per leaf node. Oversized page windows are split by page range.")
    parser.add_argument("--max-tree-depth", type=int, choices=[1, 2, 3], default=3, help="Maximum generated tree depth for simplified indexing.")
    parser.add_argument("--summary-concurrency", type=int, default=0, help="LLM summarization concurrency. <=0 means auto.")
    parser.add_argument("--index-timeout-seconds", type=int, default=0, help="Per-file indexing timeout in seconds. <=0 disables timeout.")
    parser.add_argument("--log-level", default=default_log_level, help="Logging level: DEBUG/INFO/WARNING/ERROR.")
    parser.add_argument("--log-format", choices=["json", "text"], default=default_log_format, help="Log format.")
    parser.add_argument("--verbose", action="store_true", help="Print selected documents and evidence.")
    parser.add_argument(
        "--qa-mode",
        choices=["agent", "agent-graph", "pipeline"],
        default="agent",
        help="Unified QA entry mode (default: agent): agent tool-calling, agent with LangGraph, or legacy pipeline alias.",
    )
    #pipeline已删除，agent为短期记忆，agent-graph为长期记忆;
    parser.add_argument(
        "--index-missing",
        action="store_true",
        help="Scan data_dir and index documents into PostgreSQL before QA. By default, QA uses existing PostgreSQL indexes only.",
    )
    parser.add_argument(
        "--prune-storage",
        action="store_true",
        help="Prune cached documents outside the current data directory before indexing.",
    )
    parser.add_argument(
        "--delete-doc-id",
        action="append",
        default=[],
        help="Delete indexed document(s) by doc_id. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--delete-file",
        action="append",
        default=[],
        help="Delete indexed document(s) by file name/path. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--delete-only",
        action="store_true",
        help="Run delete operation and exit without indexing/QA.",
    )
    return parser


# 组装索引参数，并对 overlap 做安全范围约束。
def build_indexing_options(args: argparse.Namespace, logger: logging.Logger) -> IndexingOptions:
    """组装索引参数，并对关键阈值做安全范围约束。"""
    overlap_ratio = max(0.1, min(0.5, float(args.chunk_overlap_ratio)))
    if overlap_ratio != float(args.chunk_overlap_ratio):
        logger.warning("chunk_overlap_ratio=%s adjusted to %s for stable retrieval continuity.", args.chunk_overlap_ratio, overlap_ratio)
    table_parse_mode = str(getattr(args, "table_parse_mode", "auto") or "auto").strip().lower()
    if table_parse_mode not in {"off", "auto"}:
        logger.warning("table_parse_mode=%s adjusted to auto.", table_parse_mode)
        table_parse_mode = "auto"
    node_max_tokens = max(64, int(getattr(args, "node_max_tokens", 512)))
    if node_max_tokens != int(getattr(args, "node_max_tokens", 512)):
        logger.warning("node_max_tokens=%s adjusted to %s.", getattr(args, "node_max_tokens", 512), node_max_tokens)
    max_tree_depth = int(getattr(args, "max_tree_depth", 3))
    if max_tree_depth not in {1, 2, 3}:
        logger.warning("max_tree_depth=%s adjusted to 3.", max_tree_depth)
        max_tree_depth = 3
    return IndexingOptions(
        simple_index_page_threshold=args.simple_index_page_threshold,
        long_pdf_hard_threshold=args.long_pdf_hard_threshold,
        simple_index_chunk_pages=args.simple_index_chunk_pages,
        hybrid_index_chunk_pages=args.hybrid_index_chunk_pages,
        chunk_overlap_ratio=overlap_ratio,
        long_pdf_mode=args.long_pdf_mode,
        complexity_sample_pages=args.complexity_sample_pages,
        index_concurrency=args.index_concurrency,
        summary_enabled=bool(getattr(args, "summary_enabled", True)),
        table_parse_mode=table_parse_mode,
        node_max_tokens=node_max_tokens,
        max_tree_depth=max_tree_depth,
        summary_concurrency=max(0, int(getattr(args, "summary_concurrency", 0))),
        index_timeout_seconds=max(0, int(getattr(args, "index_timeout_seconds", 0))),
        enable_ocr=bool(getattr(args, "enable_ocr", False)),
        ocr_min_chars=max(0, int(getattr(args, "ocr_min_chars", 20))),
        ocr_lang=str(getattr(args, "ocr_lang", "chi_sim+eng") or "chi_sim+eng").strip(),
    )

