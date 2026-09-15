# 模块说明：通用能力，包括日志、路径安全校验和文档扫描。
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.runtime import AssistantRuntime

logger = logging.getLogger(__name__)


class SuppressKnownAsyncioShutdownNoise(logging.Filter):
    """Filter noisy asyncio shutdown errors from httpx AsyncClient close tasks."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "asyncio":
            return True
        message = record.getMessage()
        if (
            "Task exception was never retrieved" in message
            and "AsyncClient.aclose()" in message
            and "Event loop is closed" in message
        ):
            return False
        return True


class AssistantError(Exception):
    """助手模块自定义异常基类。"""
    pass


# 路径越权、安全校验相关错误。
class PathSecurityError(AssistantError):
    """路径安全校验失败异常。"""
    pass


# 用户输入参数不合法错误。
class UserInputError(AssistantError):
    """用户输入参数非法异常。"""
    pass


# 返回候选路径里第一个存在的路径，用于兼容新旧目录。
def first_existing_path(*candidates: Path) -> Path:
    """返回候选路径中第一个存在的路径。

    常用于兼容新旧目录结构：优先使用真实存在的目录，
    若都不存在则返回第一个候选路径（供上层继续处理）。
    """
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


class JsonLogFormatter(logging.Formatter):
    # 输出结构化 JSON 日志，便于程序采集与检索。
    """JSON 日志格式化器。"""
    def format(self, record: logging.LogRecord) -> str:
        """将日志记录格式化为 JSON 文本。"""
        payload = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        structured = getattr(record, "structured", None)
        if isinstance(structured, dict):
            payload.update(structured)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# 初始化全局日志配置，支持 json/text 两种格式。
def setup_logging(level: str, log_format: str = "json"):
    """初始化全局日志系统。

    支持 `json` 与 `text` 两种输出格式，并挂载已知异步关闭噪声过滤器。
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.addFilter(SuppressKnownAsyncioShutdownNoise())
    if (log_format or "json").lower() == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_logger.addHandler(handler)


# 判断 path 是否在 base_dir 范围内（含子目录）。
def _is_within_directory(path: Path, base_dir: Path) -> bool:
    """判断路径是否位于指定基目录内（包含子目录）。"""
    try:
        path.resolve().relative_to(base_dir.resolve())
        return True
    except Exception:
        return False


# 校验输入源文件必须位于 data_dir 下。
def ensure_secure_source_file(path: Path, data_dir: Path) -> Path:
    """校验输入文件路径安全性，只允许位于 data_dir 内。"""
    resolved = path.expanduser().resolve()
    if not _is_within_directory(resolved, data_dir):
        raise PathSecurityError(f"Unsafe source path outside data_dir: {resolved}")
    return resolved


# 校验可选输入路径；为空时返回 None。
def validate_optional_input_path(path: Path | None, data_dir: Path, field_name: str) -> Path | None:
    """校验可选输入路径；为空时直接返回 None。"""
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not _is_within_directory(resolved, data_dir):
        raise PathSecurityError(f"{field_name} must be inside data_dir, got: {resolved}")
    return resolved


# 归一化日志字段类型，避免不可序列化对象导致日志失败。
def _normalize_log_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """归一化日志字段，避免不可序列化对象导致日志失败。"""
    normalized: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, Path):
            normalized[key] = str(value)
        elif isinstance(value, (str, int, float, bool, list, dict)):
            normalized[key] = value
        else:
            normalized[key] = str(value)
    return normalized


# 统一结构化事件日志入口。
def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    message: str | None = None,
    **fields: Any,
):
    """统一的结构化事件日志入口。"""
    payload = {"event": event}
    payload.update(_normalize_log_fields(fields))
    logger.log(level, message or event, extra={"structured": payload})


# 输出运行时指标（缓存命中、熔断、请求量等）。
def log_runtime_metrics(runtime: AssistantRuntime, event: str, **extra: Any):
    """输出运行时关键指标（缓存、熔断器、请求量等）。"""
    payload = {
        "page_cache_hits": runtime.page_content_cache.hits,
        "page_cache_misses": runtime.page_content_cache.misses,
        "qa_cache_hits": runtime.qa_result_cache.hits,
        "qa_cache_misses": runtime.qa_result_cache.misses,
        "llm_breaker_open_events": runtime.llm_breaker.open_events,
        "llm_breaker_rejected_requests": runtime.llm_breaker.rejected_requests,
        "llm_success_events": runtime.llm_breaker.success_events,
        "llm_failure_events": runtime.llm_breaker.failure_events,
    }
    payload.update(runtime.stats)
    payload.update(extra)
    payload["metric_name"] = event
    log_event("runtime_metrics", level=logging.DEBUG, **payload)


# 扫描 data_dir 内可处理文档并执行路径安全校验。
def scan_documents(data_dir: Path) -> list[Path]:
    """扫描 data_dir 内可处理文档并做路径安全校验。"""
    supported_suffixes = {".pdf", ".docx", ".md", ".markdown"}
    return sorted(
        [
            ensure_secure_source_file(path, data_dir)
            for path in data_dir.iterdir()
            if path.is_file() and path.suffix.lower() in supported_suffixes and _is_within_directory(path, data_dir)
        ],
        key=lambda path: path.name.lower(),
    )

