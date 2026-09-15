"""Re-export from embed_core for backward compatibility."""

from embed_core.milvus_store import (
    MilvusConfig,
    PictureMilvusStore,
    collection_name,
    milvus_enabled,
    vector_store_name,
)

__all__ = [
    "MilvusConfig",
    "PictureMilvusStore",
    "collection_name",
    "milvus_enabled",
    "vector_store_name",
]
