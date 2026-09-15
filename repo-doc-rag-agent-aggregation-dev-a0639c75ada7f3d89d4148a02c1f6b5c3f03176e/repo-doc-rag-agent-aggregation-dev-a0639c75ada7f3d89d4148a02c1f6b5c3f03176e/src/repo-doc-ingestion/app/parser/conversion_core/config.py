import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .exceptions import ConfigError

try:
    import yaml
except Exception:
    yaml = None

PAGEINDEX_OPT_KEYS = {
    "model",
    "toc_check_page_num",
    "max_page_num_each_node",
    "max_token_num_each_node",
    "if_add_node_id",
    "if_add_node_summary",
    "if_add_doc_description",
    "if_add_node_text",
}

PAGEINDEX_OPT_ALIASES: Dict[str, str] = {
    "toc_check_pages": "toc_check_page_num",
    "max_pages_per_node": "max_page_num_each_node",
    "max_tokens_per_node": "max_token_num_each_node",
}


BOOL_TEXT_MAP = {
    "yes": True,
    "no": False,
    "true": True,
    "false": False,
    "1": True,
    "0": False,
    "on": True,
    "off": False,
}


def coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in BOOL_TEXT_MAP:
            return BOOL_TEXT_MAP[normalized]
    raise ValueError(f"Invalid boolean value: {value}")


class RuntimeConfig(BaseModel):
    """Strongly typed runtime options validated at startup."""
    model_config = ConfigDict(extra="forbid")

    log_level: str = "INFO"
    if_thinning: bool = False
    thinning_threshold: int = 5000
    summary_token_threshold: int = 800
    max_file_size_mb: int = 200
    txt_chars_per_node: int = 1500
    txt_lines_per_node: int = 200
    max_xlsx_rows_per_sheet: int = 50000
    xlsx_batch_size: int = 1000
    summary_mode: str = "llm"
    summary_input_chars: int = 4000
    summary_max_chars: int = 320
    keyword_topk: int = 8
    min_summary_text_chars: int = 320
    force_summary: bool = True
    summary_concurrency: int = 32
    summary_rate_limit_per_sec: float = Field(default_factory=lambda: float(os.getenv("INGEST_SUMMARY_RATE_LIMIT_PER_SEC", os.getenv("ARK_SUMMARY_RATE_LIMIT_PER_SEC", "1.0"))))
    summary_timeout_seconds: float = 35.0
    summary_retry_times: int = 3
    summary_retry_backoff_base: float = 1.2
    summary_prompt_template: Optional[str] = None
    summary_model: Optional[str] = None
    cache_enabled: bool = False
    cache_dir: str = "./.conversion_cache"
    timeout_seconds: int = 0
    batch_concurrency: int = 4
    recursive: bool = True
    glob: str = "*"
    show_progress: bool = True
    memory_monitor: bool = True
    output_dir: str = "./results"
    pdf_max_depth: int = 5
    pdf_low_quality_fallback: str = "flat"
    pdf_parser: str = "auto"
    pdf_vision_model: Optional[str] = None
    pdf_vision_concurrency: int = 32
    pdf_table_mode: str = "auto"
    parser_backend: str = Field(default_factory=lambda: os.getenv("INGEST_PARSER_BACKEND", "native"))
    mineru_api_url: str = Field(default_factory=lambda: os.getenv("MINERU_API_URL", "http://127.0.0.1:8000"))
    mineru_backend: str = Field(default_factory=lambda: os.getenv("MINERU_BACKEND", "pipeline"))
    mineru_parse_method: str = Field(default_factory=lambda: os.getenv("MINERU_PARSE_METHOD", "auto"))
    mineru_lang: str = Field(default_factory=lambda: os.getenv("MINERU_LANG", "ch"))
    mineru_use_async_tasks: bool = Field(
        default_factory=lambda: coerce_bool(os.getenv("MINERU_USE_ASYNC_TASKS"), default=True)
    )
    mineru_fallback_to_native: bool = Field(
        default_factory=lambda: coerce_bool(os.getenv("MINERU_FALLBACK_TO_NATIVE"), default=True)
    )
    mineru_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("MINERU_PARSE_TIMEOUT_SECONDS", "1800"))
    )
    mineru_retry_times: int = Field(default_factory=lambda: int(os.getenv("MINERU_PARSE_RETRY_TIMES", "2")))
    mineru_retry_backoff_base: float = Field(
        default_factory=lambda: float(os.getenv("MINERU_PARSE_RETRY_BACKOFF_BASE", "1.5"))
    )
    mineru_poll_interval_seconds: float = Field(
        default_factory=lambda: float(os.getenv("MINERU_TASK_POLL_INTERVAL_SECONDS", "2"))
    )
    mineru_client_concurrency: int = Field(default_factory=lambda: int(os.getenv("MINERU_CLIENT_CONCURRENCY", "8")))
    weak_heading_split_enabled: bool = Field(
        default_factory=lambda: coerce_bool(os.getenv("INGEST_WEAK_HEADING_SPLIT_ENABLED"), default=False)
    )
    weak_heading_split_min_chars: int = Field(
        default_factory=lambda: int(os.getenv("INGEST_WEAK_HEADING_SPLIT_MIN_CHARS", "800"))
    )
    weak_heading_split_max_chars: int = Field(
        default_factory=lambda: int(os.getenv("INGEST_WEAK_HEADING_SPLIT_MAX_CHARS", "8000"))
    )

    @field_validator(
        "if_thinning",
        "force_summary",
        "cache_enabled",
        "recursive",
        "show_progress",
        "memory_monitor",
        "mineru_use_async_tasks",
        "mineru_fallback_to_native",
        "weak_heading_split_enabled",
        mode="before",
    )
    @classmethod
    def parse_bool_fields(cls, value: Any) -> bool:
        return coerce_bool(value, default=False)

    @field_validator("summary_mode")
    @classmethod
    def validate_summary_mode(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"llm", "doubao"}:
            raise ValueError("summary_mode must be one of: llm, doubao")
        return normalized

    @field_validator("pdf_low_quality_fallback")
    @classmethod
    def validate_pdf_low_quality_fallback(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"flat", "page"}:
            raise ValueError("pdf_low_quality_fallback must be one of: flat, page")
        return normalized

    @field_validator("pdf_parser")
    @classmethod
    def validate_pdf_parser(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"auto", "pymupdf", "ark_vision"}:
            raise ValueError("pdf_parser must be one of: auto, pymupdf, ark_vision")
        return normalized

    @field_validator("pdf_table_mode")
    @classmethod
    def validate_pdf_table_mode(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"auto", "off", "vision"}:
            raise ValueError("pdf_table_mode must be one of: auto, off, vision")
        return normalized

    @field_validator("parser_backend")
    @classmethod
    def validate_parser_backend(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"native", "mineru"}:
            raise ValueError("parser_backend must be one of: native, mineru")
        return normalized


class PageIndexConfig(BaseModel):
    """Strongly typed PageIndex options validated at startup."""
    model_config = ConfigDict(extra="forbid")

    model: Optional[str] = None
    toc_check_page_num: Optional[int] = None
    max_page_num_each_node: Optional[int] = None
    max_token_num_each_node: Optional[int] = None
    if_add_node_id: Optional[bool] = None
    if_add_node_summary: Optional[bool] = None
    if_add_doc_description: Optional[bool] = None
    if_add_node_text: Optional[bool] = None

    @field_validator(
        "if_add_node_id",
        "if_add_node_summary",
        "if_add_doc_description",
        "if_add_node_text",
        mode="before",
    )
    @classmethod
    def parse_optional_bool_fields(cls, value: Any) -> Optional[bool]:
        if value is None:
            return None
        return coerce_bool(value, default=False)

    def to_config_loader_payload(self) -> Dict[str, Any]:
        """Convert typed booleans to legacy yes/no payload expected by ConfigLoader."""
        payload = self.model_dump(exclude_none=True)
        for key in ["if_add_node_id", "if_add_node_summary", "if_add_doc_description", "if_add_node_text"]:
            if key in payload:
                payload[key] = "yes" if payload[key] else "no"
        return payload


def _normalize_user_config_keys(user_cfg: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key, value in user_cfg.items():
        mapped = PAGEINDEX_OPT_ALIASES.get(key, key)
        normalized[mapped] = value
    return normalized


def load_user_config(config_path: Optional[str]) -> Dict[str, Any]:
    """Load and normalize user config file from JSON/YAML."""
    if not config_path:
        return {}

    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"User config file not found: {config_path}", stage="config_load")

    suffix = path.suffix.lower()
    raw_text = path.read_text(encoding="utf-8", errors="replace")

    if suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise ConfigError(
                "PyYAML is required for yaml config: pip install pyyaml",
                stage="config_load",
            )
        data = yaml.safe_load(raw_text) or {}
    elif suffix == ".json":
        data = json.loads(raw_text or "{}")
    else:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            if yaml is None:
                raise ConfigError("Unsupported config extension. Use .json/.yaml/.yml", stage="config_load")
            data = yaml.safe_load(raw_text) or {}

    if not isinstance(data, dict):
        raise ConfigError("User config must be a JSON/YAML object", stage="config_load")

    return _normalize_user_config_keys(data)


def split_runtime_and_pageindex(raw_cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Split merged raw config into runtime and pageindex subsets."""
    pageindex_cfg = {k: v for k, v in raw_cfg.items() if k in PAGEINDEX_OPT_KEYS}
    runtime_cfg = {k: v for k, v in raw_cfg.items() if k not in PAGEINDEX_OPT_KEYS}
    return runtime_cfg, pageindex_cfg
