from __future__ import annotations

import re
from typing import Any

from .errors import EmbeddingServiceError

# Stable reason codes for frontend branching / i18n extension.
REASON_FILE_TOO_LARGE = "FILE_TOO_LARGE"
REASON_DOWNLOAD_TIMEOUT = "DOWNLOAD_TIMEOUT"
REASON_OSS_NOT_CONFIGURED = "OSS_NOT_CONFIGURED"
REASON_OSS_DOWNLOAD_FAILED = "OSS_DOWNLOAD_FAILED"
REASON_OSS_NOT_FOUND = "OSS_NOT_FOUND"
REASON_EMPTY_FILE = "EMPTY_FILE"
REASON_IMAGE_LOAD_FAILED = "IMAGE_LOAD_FAILED"
REASON_FFMPEG_FAILED = "FFMPEG_FAILED"
REASON_NO_SEGMENTS = "NO_SEGMENTS"
REASON_NO_EMBEDDINGS = "NO_EMBEDDINGS"
REASON_EMPTY_DESCRIPTION = "EMPTY_DESCRIPTION"
REASON_INTERRUPTED = "INTERRUPTED"
REASON_TASK_LIMIT = "TASK_LIMIT"
REASON_MODEL_ERROR = "MODEL_ERROR"
REASON_MILVUS_ERROR = "MILVUS_ERROR"
REASON_MILVUS_NOT_ENABLED = "MILVUS_NOT_ENABLED"
REASON_INTERNAL = "INTERNAL"
REASON_EMPTY_TEXT = "EMPTY_TEXT"

_REASON_MESSAGES: dict[str, str] = {
    REASON_FILE_TOO_LARGE: "文件大小超过限制（上限 {max_mb} MB）",
    REASON_DOWNLOAD_TIMEOUT: "文件下载超时，请稍后重试（单次超时 {timeout_seconds} 秒，已重试 {attempts} 次）",
    REASON_OSS_NOT_CONFIGURED: "OSS 未配置，无法下载媒体文件",
    REASON_OSS_DOWNLOAD_FAILED: "从 OSS 下载文件失败，请检查网络或文件路径",
    REASON_OSS_NOT_FOUND: "文件不存在或无权访问",
    REASON_EMPTY_FILE: "OSS 文件为空（0 字节），请检查是否上传成功",
    REASON_IMAGE_LOAD_FAILED: "图片无法解析，可能已损坏或格式不支持",
    REASON_FFMPEG_FAILED: "视频处理失败：{summary}",
    REASON_NO_SEGMENTS: "无法从视频中生成分段，文件可能已损坏或格式不支持",
    REASON_NO_EMBEDDINGS: "未能从视频中提取有效画面向量",
    REASON_EMPTY_DESCRIPTION: "描述文字不能为空",
    REASON_EMPTY_TEXT: "查询文本不能为空",
    REASON_INTERRUPTED: "服务重启导致任务中断，请重新提交",
    REASON_TASK_LIMIT: "任务队列已满，请稍后重试",
    REASON_MODEL_ERROR: "模型加载或推理失败，请稍后重试",
    REASON_MILVUS_ERROR: "向量库写入失败，请稍后重试",
    REASON_MILVUS_NOT_ENABLED: "未启用向量库，无法写入向量（需设置 VECTOR_STORE=milvus）",
    REASON_INTERNAL: "服务内部错误，请稍后重试",
}

_CODE_DEFAULT_MESSAGES: dict[str, str] = {
    "VALIDATION_ERROR": "请求参数或业务校验失败",
    "CONFIGURATION_ERROR": "服务配置错误",
    "MEDIA_DOWNLOAD_ERROR": "媒体文件下载失败",
    "MEDIA_PROCESSING_ERROR": "媒体文件处理失败",
    "MODEL_ERROR": "模型加载或推理失败",
    "VECTOR_STORE_ERROR": "向量库操作失败",
    "TASK_LIMIT_ERROR": "任务队列已满",
    "INTERNAL_ERROR": "服务内部错误",
}


def _format_bytes_mb(value: int | float | None) -> str | None:
    if value is None:
        return None
    return f"{float(value) / (1024 * 1024):.1f}"


def _mb_from_bytes(value: int | float | None) -> str | None:
    if value is None:
        return None
    return f"{int(float(value) / (1024 * 1024))}"


def _format_template(template: str, details: dict[str, Any]) -> str:
    payload = dict(details)
    if "max_mb" not in payload:
        if payload.get("max_video_mb") is not None:
            payload["max_mb"] = payload["max_video_mb"]
        elif payload.get("max_bytes") is not None:
            payload["max_mb"] = _mb_from_bytes(payload["max_bytes"])
        elif payload.get("max_image_mb") is not None:
            payload["max_mb"] = payload["max_image_mb"]
    try:
        return template.format_map(_SafeFormatDict(payload))
    except Exception:
        return template


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def format_service_error(exc: EmbeddingServiceError) -> dict[str, Any]:
    """Convert a service exception to a frontend-friendly Chinese payload."""
    details = dict(exc.details or {})
    reason = details.get("reason")
    if not reason:
        reason = _infer_reason_from_message(exc.code, exc.message)
        if reason:
            details["reason"] = reason

    message = None
    if reason and reason in _REASON_MESSAGES:
        message = _format_template(_REASON_MESSAGES[reason], details)
    if not message:
        message = _CODE_DEFAULT_MESSAGES.get(exc.code, exc.message)

    return {
        "error": message,
        "code": exc.code,
        "details": details,
    }


def format_exception(exc: Exception) -> dict[str, Any]:
    """Map unexpected exceptions to Chinese user messages when possible."""
    if isinstance(exc, EmbeddingServiceError):
        return format_service_error(exc)

    text = str(exc)
    details: dict[str, Any] = {"reason": REASON_INTERNAL}

    if isinstance(exc, ValueError) and text.strip().lower() == "empty text":
        details["reason"] = REASON_EMPTY_TEXT
        return {
            "error": _REASON_MESSAGES[REASON_EMPTY_TEXT],
            "code": "VALIDATION_ERROR",
            "details": details,
        }

    if "ffmpeg segment failed" in text:
        summary = text.split("ffmpeg segment failed for", 1)[-1].strip()
        if ":" in summary:
            summary = summary.split(":", 1)[-1].strip()
        details.update({"reason": REASON_FFMPEG_FAILED, "summary": summary or "未知错误"})
        return {
            "error": _format_template(_REASON_MESSAGES[REASON_FFMPEG_FAILED], details),
            "code": "MEDIA_PROCESSING_ERROR",
            "details": details,
        }

    if "Failed to download OSS object after" in text or "Failed to download from OSS after" in text:
        details["reason"] = REASON_DOWNLOAD_TIMEOUT
        return {
            "error": _format_template(
                _REASON_MESSAGES[REASON_DOWNLOAD_TIMEOUT],
                {"timeout_seconds": 300, "attempts": 5, **details},
            ),
            "code": "MEDIA_DOWNLOAD_ERROR",
            "details": details,
        }

    if "exceeds max size" in text.lower() or "exceeds max size" in text:
        details["reason"] = REASON_FILE_TOO_LARGE
        size_match = re.search(r"(\d+)\s*bytes", text)
        if size_match:
            details["size_bytes"] = int(size_match.group(1))
        return {
            "error": _format_template(_REASON_MESSAGES[REASON_FILE_TOO_LARGE], details),
            "code": "VALIDATION_ERROR",
            "details": details,
        }

    if "oss2 is required" in text:
        details["reason"] = REASON_OSS_NOT_CONFIGURED
        return {
            "error": _REASON_MESSAGES[REASON_OSS_NOT_CONFIGURED],
            "code": "CONFIGURATION_ERROR",
            "details": details,
        }

    return {
        "error": _REASON_MESSAGES[REASON_INTERNAL],
        "code": "INTERNAL_ERROR",
        "details": {**details, "message": text},
    }


def _infer_reason_from_message(code: str, message: str) -> str | None:
    lowered = message.lower()
    if "exceeds max size" in lowered or "超过" in message:
        return REASON_FILE_TOO_LARGE
    if "after" in lowered and "attempts" in lowered:
        return REASON_DOWNLOAD_TIMEOUT
    if "oss is not configured" in lowered:
        return REASON_OSS_NOT_CONFIGURED
    if "failed to download from oss" in lowered:
        return REASON_OSS_DOWNLOAD_FAILED
    if "failed to load image" in lowered:
        return REASON_IMAGE_LOAD_FAILED
    if "no video segments" in lowered:
        return REASON_NO_SEGMENTS
    if "no segment embeddings" in lowered:
        return REASON_NO_EMBEDDINGS
    if "empty image description" in lowered or "empty video description" in lowered:
        return REASON_EMPTY_DESCRIPTION
    if "write_vector=true requires" in lowered:
        return REASON_MILVUS_NOT_ENABLED
    if "milvus write failed" in lowered:
        return REASON_MILVUS_ERROR
    if "task limit reached" in lowered:
        return REASON_TASK_LIMIT
    if code == "MODEL_ERROR":
        return REASON_MODEL_ERROR
    return None
