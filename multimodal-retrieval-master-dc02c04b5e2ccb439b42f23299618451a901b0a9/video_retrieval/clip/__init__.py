"""Chinese-CLIP video retrieval (Milvus)."""

from .config import PROJECT_ROOT, RetrievalConfig
from .retrieval import VideoRetriever
from .retriever_factory import build_retriever

__all__ = [
    "PROJECT_ROOT",
    "RetrievalConfig",
    "VideoRetriever",
    "build_retriever",
]
