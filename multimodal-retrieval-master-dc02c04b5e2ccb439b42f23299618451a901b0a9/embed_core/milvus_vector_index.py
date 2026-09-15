"""Milvus-backed vector index with a batch search() interface for video CLIP recall."""

from __future__ import annotations

import numpy as np

from .milvus_store import PictureMilvusStore


def _normalize_rows(vectors: np.ndarray) -> np.ndarray:
    rows = np.asarray(vectors, dtype=np.float32)
    if rows.ndim == 1:
        rows = rows.reshape(1, -1)
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-12)
    return (rows / norms).astype(np.float32)


class MilvusVectorIndex:
    """Batch vector search wrapper over PictureMilvusStore."""

    def __init__(self, store: PictureMilvusStore):
        self.store = store
        self.dim = store.dim
        self.item_ids: list[str] = []

    def search(self, query_embeddings: np.ndarray, top_k: int) -> list[list[tuple[str, float]]]:
        entity_hits = self.search_entities(query_embeddings, top_k=top_k, output_fields=["pk"])
        return [
            [(str(hit.get("pk") or ""), float(hit.get("score", 0.0))) for hit in hits if hit.get("pk")]
            for hits in entity_hits
        ]

    def search_entities(
        self,
        query_embeddings: np.ndarray,
        *,
        top_k: int,
        output_fields: list[str] | None = None,
    ) -> list[list[dict]]:
        queries = _normalize_rows(query_embeddings)
        fields = output_fields or ["pk", "media_id", "object_key", "path"]
        results: list[list[dict]] = []
        for row in queries:
            hits = self.store.search(row, top_k=top_k, output_fields=fields)
            results.append(hits)
        return results

    def count(self) -> int:
        return self.store.count()

    def close(self) -> None:
        self.store.close()
