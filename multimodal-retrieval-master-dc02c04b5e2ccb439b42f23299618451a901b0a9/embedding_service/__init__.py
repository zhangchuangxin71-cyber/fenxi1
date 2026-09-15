"""
Embedding Service - Media and text embedding service with CLIP and BGE models.

Version: 0.3.0
"""

from .config import ServiceConfig
from .errors import (
    ConfigurationError,
    EmbeddingServiceError,
    MediaDownloadError,
    MediaProcessingError,
    ModelError,
    TaskLimitError,
    ValidationError,
    VectorStoreError,
)
from .model_registry import ModelRegistry
from .tasks import TaskManager, TaskRecord

__version__ = "0.3.0"

__all__ = [
    "ServiceConfig",
    "ModelRegistry",
    "TaskManager",
    "TaskRecord",
    "EmbeddingServiceError",
    "ConfigurationError",
    "MediaDownloadError",
    "MediaProcessingError",
    "ModelError",
    "TaskLimitError",
    "ValidationError",
    "VectorStoreError",
]
