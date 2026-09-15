"""Load Chinese-CLIP video vectors from embedding_service Milvus collection."""

from __future__ import annotations

import os

from embed_core.milvus_store import PictureMilvusStore, media_video_clip_collection
from embed_core.milvus_vector_index import MilvusVectorIndex


def video_clip_dim() -> int:
    return int(os.environ.get("VIDEO_CLIP_DIM", "1024"))


def load_media_video_clip_index() -> MilvusVectorIndex:
    """Whole-video CLIP index written by embedding_service (collection media_video_clip)."""
    dim = video_clip_dim()
    collection = media_video_clip_collection(None)
    store = PictureMilvusStore(collection=collection, dim=dim)
    index = MilvusVectorIndex(store)
    if index.count() == 0:
        index.close()
        raise RuntimeError(
            f"Milvus collection is empty: {collection}. "
            "Run embedding_service video CLIP jobs with write_vector=true first."
        )
    return index


def load_milvus_video_indexes(profile: str) -> MilvusVectorIndex:
    """Backward-compatible entry; profile is ignored (media_video_clip is global)."""
    del profile
    return load_media_video_clip_index()
