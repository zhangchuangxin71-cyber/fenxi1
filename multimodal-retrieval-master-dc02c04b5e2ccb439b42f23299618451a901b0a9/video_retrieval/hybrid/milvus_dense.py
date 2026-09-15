"""Milvus dense index for hybrid BGE video retrieval."""

from __future__ import annotations

import os

import numpy as np

from embed_core.milvus_store import PictureMilvusStore, hybrid_retrieval_collection

from .metadata_store import MetadataStore

MILVUS_UPSERT_BATCH = 500


def hybrid_bge_dim() -> int:
    return int(os.environ.get("VIDEO_BGE_DIM", "1024"))


def _normalize(vector: np.ndarray) -> np.ndarray:
    row = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(row))
    if norm > 0:
        row = row / norm
    return row.astype(np.float32)


def upsert_hybrid_dense_to_milvus(
    store: MetadataStore,
    profile: str | None,
    *,
    dim: int | None = None,
) -> int:
    rows = store.list_search_documents(require_embeddings=True)
    if not rows:
        return 0

    resolved_dim = dim or hybrid_bge_dim()
    milvus = PictureMilvusStore(
        collection=hybrid_retrieval_collection(profile),
        dim=resolved_dim,
    )
    try:
        payload: list[dict] = []
        for row in rows:
            vector = row.get("embedding_vector")
            if vector is None:
                continue
            video_id = str(row["video_id"])
            payload.append(
                {
                    "pk": video_id,
                    "video_id": video_id,
                    "path": str(row.get("path") or ""),
                    "description": str(row.get("description") or ""),
                    "caption_text": str(row.get("caption_text") or ""),
                    "tags_json": str(row.get("tags_json") or "[]"),
                    "modality": "video_hybrid",
                    "vector": _normalize(vector),
                }
            )

        for start in range(0, len(payload), MILVUS_UPSERT_BATCH):
            milvus.upsert(payload[start : start + MILVUS_UPSERT_BATCH])
        return milvus.count()
    finally:
        milvus.close()
