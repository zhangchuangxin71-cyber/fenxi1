from __future__ import annotations

from typing import Any


class EmbeddingServiceError(Exception):
    """Base exception for embedding service."""

    def __init__(self, message: str, code: str = "INTERNAL_ERROR", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


class ConfigurationError(EmbeddingServiceError):
    """Configuration error."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, code="CONFIGURATION_ERROR", details=details)


class MediaDownloadError(EmbeddingServiceError):
    """Media download error."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, code="MEDIA_DOWNLOAD_ERROR", details=details)


class MediaProcessingError(EmbeddingServiceError):
    """Media processing error."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, code="MEDIA_PROCESSING_ERROR", details=details)


class ModelError(EmbeddingServiceError):
    """Model loading or inference error."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, code="MODEL_ERROR", details=details)


class VectorStoreError(EmbeddingServiceError):
    """Vector store error."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, code="VECTOR_STORE_ERROR", details=details)


class TaskLimitError(EmbeddingServiceError):
    """Task limit exceeded error."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, code="TASK_LIMIT_ERROR", details=details)


class ValidationError(EmbeddingServiceError):
    """Input validation error."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, code="VALIDATION_ERROR", details=details)
