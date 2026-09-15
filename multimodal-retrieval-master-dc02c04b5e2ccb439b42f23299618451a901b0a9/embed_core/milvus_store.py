from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import numpy as np


def _require_pymilvus():
    try:
        from pymilvus import MilvusClient
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("pymilvus is required for Milvus storage: pip install pymilvus") from exc
    return MilvusClient


@dataclass(frozen=True)
class MilvusConfig:
    uri: str = "http://localhost:19530"
    token: str | None = None
    db_name: str = "default"
    collection_prefix: str = "chinese_clip"
    metric_type: str = "COSINE"

    @classmethod
    def from_env(cls) -> MilvusConfig:
        return cls(
            uri=os.environ.get("MILVUS_URI", "http://localhost:19530"),
            token=os.environ.get("MILVUS_TOKEN") or None,
            db_name=os.environ.get("MILVUS_DB_NAME", "default"),
            collection_prefix=os.environ.get("MILVUS_COLLECTION_PREFIX", "chinese_clip"),
            metric_type=os.environ.get("MILVUS_METRIC_TYPE", "COSINE"),
        )


def vector_store_name() -> str:
    """Return configured vector store kind. Default is ``milvus``."""
    raw = os.environ.get("VECTOR_STORE", "milvus").strip().lower()
    if raw in ("", "milvus"):
        return "milvus"
    return raw


def milvus_enabled() -> bool:
    return vector_store_name() == "milvus"


def collection_name(kind: str, profile: str | None, *, prefix: str | None = None) -> str:
    safe_profile = (profile or "default").replace("-", "_").replace("/", "_").replace("\\", "_")
    safe_kind = kind.replace("-", "_").replace("/", "_")
    return f"{prefix or MilvusConfig.from_env().collection_prefix}_{safe_kind}_{safe_profile}"


def video_retrieval_collection(level: str, profile: str | None) -> str:
    """Milvus collection for local video CLIP retrieval (frame / segment / representative)."""
    safe_level = level.replace("-", "_").replace("/", "_")
    return collection_name(f"video_clip_{safe_level}", profile)


def hybrid_retrieval_collection(profile: str | None) -> str:
    """Milvus collection for hybrid video BGE dense retrieval."""
    return collection_name("hybrid_video_bge", profile)


def media_video_clip_collection(profile: str | None = None) -> str:
    """Milvus collection for whole-video CLIP vectors (embedding_service write_vector)."""
    return collection_name("media_video_clip", profile)


class PictureMilvusStore:
    """Milvus-backed picture store that keeps vectors and display metadata together."""

    def __init__(self, *, collection: str, dim: int, config: MilvusConfig | None = None):
        self.config = config or MilvusConfig.from_env()
        self.collection = collection
        self.dim = int(dim)
        MilvusClient = _require_pymilvus()
        kwargs: dict[str, Any] = {"uri": self.config.uri, "db_name": self.config.db_name}
        if self.config.token:
            kwargs["token"] = self.config.token
        self.client = MilvusClient(**kwargs)
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if self.client.has_collection(self.collection):
            return
        self.client.create_collection(
            collection_name=self.collection,
            dimension=self.dim,
            metric_type=self.config.metric_type,
            primary_field_name="pk",
            id_type="string",
            vector_field_name="vector",
            max_length=512,
            consistency_level="Strong",
            enable_dynamic_field=True,
        )

    def upsert(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        payload = []
        for row in rows:
            vector = np.asarray(row["vector"], dtype=np.float32).reshape(-1)
            if vector.size != self.dim:
                raise ValueError(f"vector dim mismatch: {vector.size} vs {self.dim}")
            item = dict(row)
            item["vector"] = vector.tolist()
            payload.append(item)
        self.client.upsert(collection_name=self.collection, data=payload)

    def search(
        self,
        vector: np.ndarray,
        *,
        top_k: int,
        output_fields: list[str] | None = None,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        query = np.asarray(vector, dtype=np.float32).reshape(-1)
        if query.size != self.dim:
            raise ValueError(f"query dim mismatch: {query.size} vs {self.dim}")
        result = self.client.search(
            collection_name=self.collection,
            data=[query.tolist()],
            limit=int(top_k),
            filter=filter_expr,
            output_fields=output_fields or ["*"],
        )
        hits: list[dict[str, Any]] = []
        for hit in result[0] if result else []:
            entity = dict(hit.get("entity") or {})
            entity["score"] = float(hit.get("distance", 0.0))
            entity["pk"] = str(hit.get("id") or entity.get("pk") or "")
            hits.append(entity)
        return hits

    def get(self, pk: str, *, output_fields: list[str] | None = None) -> dict[str, Any] | None:
        rows = self.client.get(
            collection_name=self.collection,
            ids=[pk],
            output_fields=output_fields or ["*"],
        )
        return dict(rows[0]) if rows else None

    def count(self) -> int:
        stats = self.client.get_collection_stats(self.collection)
        return int(stats.get("row_count", 0))

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
