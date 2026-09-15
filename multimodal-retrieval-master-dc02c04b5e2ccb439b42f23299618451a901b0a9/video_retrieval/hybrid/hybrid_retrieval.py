from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from embed_core.milvus_store import PictureMilvusStore, hybrid_retrieval_collection, milvus_enabled

from .dense_embeddings import MANIFEST_FILENAME, load_text_embedder
from .metadata_store import MetadataStore
from .milvus_dense import _normalize, hybrid_bge_dim
from ..profile_paths import SearchSource
from .search_text import build_sparse_query

DEFAULT_WARMUP_QUERY = "一个人在室内活动。"


@dataclass
class HybridSearchConfig:
    sparse_top_k: int = 100
    dense_top_k: int = 100
    rrf_k: int = 60


def _embedder_manifest_signature(index_dir: Path) -> tuple[Any, ...]:
    manifest_path = index_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return ("missing",)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return (
        payload.get("backend"),
        payload.get("model_name"),
        payload.get("vector_dim"),
        payload.get("max_length"),
        payload.get("pooling"),
        payload.get("normalize"),
        payload.get("query_instruction"),
    )


class HybridSearchEngine:
    def __init__(
        self,
        *,
        metadata_db_path: str | Path,
        index_dir: str | Path,
        collection_profile: str | None = None,
        search_config: HybridSearchConfig | None = None,
        embedding_device: str = "cuda",
        embedding_batch_size: int = 16,
        embedding_local_files_only: bool = False,
        embedder=None,
    ):
        if not milvus_enabled():
            raise RuntimeError(
                "Hybrid retrieval requires Milvus (VECTOR_STORE=milvus, default). "
                "Sparse search still uses SQLite FTS; dense vectors are stored in Milvus."
            )
        self.metadata_db_path = Path(metadata_db_path)
        self.index_dir = Path(index_dir)
        self.collection_profile = collection_profile
        self.search_config = search_config or HybridSearchConfig()
        self.store = MetadataStore(self.metadata_db_path)
        self.embedder = embedder
        if self.embedder is None:
            self.embedder = load_text_embedder(
                self.index_dir,
                device=embedding_device,
                batch_size=embedding_batch_size,
                local_files_only=embedding_local_files_only,
            )
        self.milvus_store = PictureMilvusStore(
            collection=hybrid_retrieval_collection(collection_profile),
            dim=self.embedder.vector_dim or hybrid_bge_dim(),
        )
        self._load_documents()

    def _load_documents(self) -> None:
        rows = self.store.list_search_documents(require_embeddings=False)
        self.documents_by_id = {row["video_id"]: row for row in rows}

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        sparse_top_k: int | None = None,
        dense_top_k: int | None = None,
        rrf_k: int | None = None,
        query_vector: np.ndarray | None = None,
    ) -> list[dict]:
        text = str(query or "").strip()
        if not text:
            return []

        config = self.search_config
        sparse_limit = max(top_k, sparse_top_k or config.sparse_top_k)
        dense_limit = max(top_k, dense_top_k or config.dense_top_k)
        fusion_k = max(1, rrf_k or config.rrf_k)

        sparse_hits = self.store.search_sparse(build_sparse_query(text), limit=sparse_limit)
        dense_hits = self._search_dense(text, limit=dense_limit, query_vector=query_vector)

        fused: dict[str, dict] = {}
        for rank, row in enumerate(sparse_hits, start=1):
            entry = fused.setdefault(
                row["video_id"],
                {
                    "video_id": row["video_id"],
                    "video_path": row.get("path") or "",
                    "duration_seconds": row.get("duration") or 0.0,
                    "tags": row.get("tags") or [],
                    "description": row.get("description") or row.get("caption_text") or "",
                    "caption": row.get("caption_text") or row.get("description") or "",
                    "score": 0.0,
                    "sparse_rank": None,
                    "dense_rank": None,
                    "sparse_score": None,
                    "dense_score": None,
                    "segments": [],
                },
            )
            entry["score"] += 1.0 / (fusion_k + rank)
            entry["sparse_rank"] = rank
            entry["sparse_score"] = row.get("bm25_score")

        for rank, row in enumerate(dense_hits, start=1):
            entry = fused.setdefault(
                row["video_id"],
                {
                    "video_id": row["video_id"],
                    "video_path": row.get("path") or "",
                    "duration_seconds": row.get("duration") or 0.0,
                    "tags": row.get("tags") or [],
                    "description": row.get("description") or row.get("caption_text") or "",
                    "caption": row.get("caption_text") or row.get("description") or "",
                    "score": 0.0,
                    "sparse_rank": None,
                    "dense_rank": None,
                    "sparse_score": None,
                    "dense_score": None,
                    "segments": [],
                },
            )
            entry["score"] += 1.0 / (fusion_k + rank)
            entry["dense_rank"] = rank
            entry["dense_score"] = row.get("dense_score")

        results = sorted(
            fused.values(),
            key=lambda item: (
                -float(item["score"]),
                float(item["dense_score"] or -1.0),
                -(float(item["sparse_rank"] or 10_000)),
            ),
        )[:top_k]

        for item in results:
            duration = float(item.get("duration_seconds") or 0.0)
            item["segments"] = [
                {"start": 0.0, "end": round(duration, 3), "score": round(float(item["score"]), 6)}
            ]
        return results

    def _search_dense(
        self,
        query: str,
        *,
        limit: int,
        query_vector: np.ndarray | None = None,
    ) -> list[dict]:
        if self.milvus_store.count() == 0:
            return []
        if query_vector is None:
            query_vector = self.embedder.encode_queries([query])[0]
        return self._search_dense_from_vector(query_vector, limit=limit)

    def _search_dense_from_vector(self, query_vector: np.ndarray, *, limit: int) -> list[dict]:
        if self.milvus_store.count() == 0:
            return []
        hits = self.milvus_store.search(_normalize(query_vector), top_k=max(1, limit))
        results: list[dict] = []
        for hit in hits:
            video_id = str(hit.get("video_id") or hit.get("pk") or "")
            if not video_id:
                continue
            row = dict(self.documents_by_id.get(video_id) or hit)
            row["video_id"] = video_id
            row["dense_score"] = float(hit.get("score") or 0.0)
            results.append(row)
        return results

    def warmup(self, query: str = DEFAULT_WARMUP_QUERY) -> None:
        self.search(query, top_k=1, sparse_top_k=1, dense_top_k=1)

    def close(self) -> None:
        self.milvus_store.close()
        self.store.close()


class CombinedHybridSearchEngine:
    def __init__(
        self,
        *,
        sources: list[SearchSource],
        search_config: HybridSearchConfig | None = None,
        embedding_device: str = "cuda",
        embedding_batch_size: int = 16,
        embedding_local_files_only: bool = False,
    ):
        if not sources:
            raise ValueError("At least one search source is required.")
        self.sources = list(sources)
        self.search_config = search_config or HybridSearchConfig()
        self.engines: list[tuple[SearchSource, HybridSearchEngine]] = []
        self.documents_by_id: dict[str, dict] = {}
        self.video_sources: dict[str, list[str]] = {}
        self.shared_embedder = None

        try:
            signatures = {_embedder_manifest_signature(source.index_dir) for source in self.sources}
            can_share_embedder = len(signatures) == 1 and next(iter(signatures))[0] != "missing"
            if can_share_embedder:
                first_source = self.sources[0]
                self.shared_embedder = load_text_embedder(
                    first_source.index_dir,
                    device=embedding_device,
                    batch_size=embedding_batch_size,
                    local_files_only=embedding_local_files_only,
                )
            for source in self.sources:
                engine = HybridSearchEngine(
                    metadata_db_path=source.metadata_db_path,
                    index_dir=source.index_dir,
                    collection_profile=source.name,
                    search_config=self.search_config,
                    embedding_device=embedding_device,
                    embedding_batch_size=embedding_batch_size,
                    embedding_local_files_only=embedding_local_files_only,
                    embedder=self.shared_embedder,
                )
                self.engines.append((source, engine))
                for video_id, row in engine.documents_by_id.items():
                    self.documents_by_id.setdefault(video_id, row)
                    source_list = self.video_sources.setdefault(video_id, [])
                    if source.name not in source_list:
                        source_list.append(source.name)
        except Exception:
            self.close()
            raise

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        sparse_top_k: int | None = None,
        dense_top_k: int | None = None,
        rrf_k: int | None = None,
    ) -> list[dict]:
        if not self.engines:
            return []

        config = self.search_config
        per_source_top_k = max(
            top_k,
            sparse_top_k or config.sparse_top_k,
            dense_top_k or config.dense_top_k,
        )
        merged: dict[str, dict] = {}
        query_vector = None
        if self.shared_embedder is not None:
            query_vector = self.shared_embedder.encode_queries([query])[0]

        for source, engine in self.engines:
            results = engine.search(
                query,
                top_k=per_source_top_k,
                sparse_top_k=sparse_top_k,
                dense_top_k=dense_top_k,
                rrf_k=rrf_k,
                query_vector=query_vector,
            )
            for item in results:
                video_id = str(item["video_id"])
                existing = merged.get(video_id)
                if existing is None:
                    payload = dict(item)
                    payload["source_profiles"] = [source.name]
                    payload["source_profile"] = source.name
                    merged[video_id] = payload
                    continue

                existing["score"] = float(existing.get("score") or 0.0) + float(
                    item.get("score") or 0.0
                )
                for rank_key in ("sparse_rank", "dense_rank"):
                    current = existing.get(rank_key)
                    candidate = item.get(rank_key)
                    if candidate is not None and (current is None or int(candidate) < int(current)):
                        existing[rank_key] = candidate
                for score_key in ("sparse_score", "dense_score"):
                    current = existing.get(score_key)
                    candidate = item.get(score_key)
                    if candidate is not None and (
                        current is None or float(candidate) > float(current)
                    ):
                        existing[score_key] = candidate
                if not existing.get("video_path") and item.get("video_path"):
                    existing["video_path"] = item["video_path"]
                if not existing.get("description") and item.get("description"):
                    existing["description"] = item["description"]
                if not existing.get("caption") and item.get("caption"):
                    existing["caption"] = item["caption"]
                if not existing.get("tags") and item.get("tags"):
                    existing["tags"] = item["tags"]
                source_profiles = existing.setdefault("source_profiles", [])
                if source.name not in source_profiles:
                    source_profiles.append(source.name)

        results = sorted(
            merged.values(),
            key=lambda item: (
                -float(item["score"]),
                float(item["dense_score"] or -1.0),
                -(float(item["sparse_rank"] or 10_000)),
            ),
        )[:top_k]
        for item in results:
            item["source_profiles"] = sorted(
                set(str(name) for name in item.get("source_profiles", []))
            )
            if item["source_profiles"]:
                item["source_profile"] = item["source_profiles"][0]
        return results

    def warmup(self, query: str = DEFAULT_WARMUP_QUERY) -> None:
        self.search(query, top_k=1, sparse_top_k=1, dense_top_k=1)

    def close(self) -> None:
        for _, engine in self.engines:
            engine.close()
        self.engines = []


def build_search_engine(
    *,
    sources: list[SearchSource],
    search_config: HybridSearchConfig | None = None,
    embedding_device: str = "cuda",
    embedding_batch_size: int = 16,
    embedding_local_files_only: bool = False,
) -> HybridSearchEngine | CombinedHybridSearchEngine:
    if len(sources) == 1:
        source = sources[0]
        return HybridSearchEngine(
            metadata_db_path=source.metadata_db_path,
            index_dir=source.index_dir,
            collection_profile=source.name,
            search_config=search_config,
            embedding_device=embedding_device,
            embedding_batch_size=embedding_batch_size,
            embedding_local_files_only=embedding_local_files_only,
        )
    return CombinedHybridSearchEngine(
        sources=sources,
        search_config=search_config,
        embedding_device=embedding_device,
        embedding_batch_size=embedding_batch_size,
        embedding_local_files_only=embedding_local_files_only,
    )
