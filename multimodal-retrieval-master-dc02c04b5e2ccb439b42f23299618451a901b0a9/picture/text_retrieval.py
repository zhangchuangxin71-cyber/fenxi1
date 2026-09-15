from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .image_resolve import resolve_image_file
from .milvus_store import PictureMilvusStore, collection_name, milvus_enabled
from .profile_paths import default_caption_metadata_db_path, default_output_dir, resolve_path
from .text_metadata_store import CaptionMetadataStore
from .vector_utils import l2_normalize


@dataclass
class TextSearchFilters:
    subject: str | None = None
    color: str | None = None
    action: str | None = None
    style: str | None = None

    def active(self) -> bool:
        return any(
            str(v or "").strip() for v in (self.subject, self.color, self.action, self.style)
        )


class PictureTextRetriever:
    def __init__(
        self,
        *,
        output_dir: Path,
        metadata_db: Path,
        bge_device: str = "cuda",
        bge_batch_size: int = 16,
        search_roots: list[Path] | None = None,
    ):
        if not milvus_enabled():
            raise RuntimeError(
                "Caption retrieval requires Milvus (VECTOR_STORE=milvus, default). "
                "FAISS indexes are no longer supported."
            )
        self.output_dir = output_dir
        self.search_roots = search_roots or []
        self.store = CaptionMetadataStore(metadata_db)
        self.bge_device = bge_device
        self.bge_batch_size = bge_batch_size
        profile = output_dir.parent.name if output_dir.name == "output" else None
        self.milvus_store: PictureMilvusStore | None = None
        self._milvus_collection_profile = profile
        self._embedder = None
        self._cache = {item[0]: item[2] for item in self.store.list_done_with_embeddings()}

    def _get_embedder(self):
        if self._embedder is None:
            from video_retrieval.hybrid.dense_embeddings import HuggingFaceBgeTextEmbedder

            from .config import DEFAULT_BGE_MODEL_NAME

            bge_dir = self.output_dir / "bge_embedder"
            if (bge_dir / "embedder_manifest.json").exists():
                self._embedder = HuggingFaceBgeTextEmbedder.load(
                    bge_dir, device=self.bge_device, batch_size=self.bge_batch_size
                )
            else:
                self._embedder = HuggingFaceBgeTextEmbedder(
                    model_name=DEFAULT_BGE_MODEL_NAME, device=self.bge_device
                )
        return self._embedder

    def _ensure_milvus(self, dim: int) -> PictureMilvusStore:
        if self.milvus_store is None:
            self.milvus_store = PictureMilvusStore(
                collection=collection_name("picture_bge", self._milvus_collection_profile),
                dim=dim,
            )
        return self.milvus_store

    def search_text(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: TextSearchFilters | None = None,
    ) -> tuple[list[dict], float]:
        filters = filters or TextSearchFilters()
        embedder = self._get_embedder()
        query_vec = l2_normalize(embedder.encode_queries([query])[0])
        started = time.perf_counter()

        if filters.active():
            candidates = self.store.list_records_matching_filters(
                subject=filters.subject,
                color=filters.color,
                action=filters.action,
                style=filters.style,
            )
            scored = []
            for record in candidates:
                vec = self.store.load_embedding(record)
                if vec is None:
                    continue
                scored.append((float(np.dot(l2_normalize(vec), query_vec)), record))
            scored.sort(key=lambda x: x[0], reverse=True)
            hits = scored[:top_k]
            results = [self._record_to_dict(rec, sc) for sc, rec in hits]
        else:
            milvus = self._ensure_milvus(query_vec.size)
            hits = milvus.search(query_vec, top_k=top_k)
            results = [self._milvus_hit_to_dict(hit) for hit in hits]

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return results, elapsed_ms

    def _record_to_dict(self, record: dict, score: float) -> dict:
        image_id = str(record.get("image_id") or "")
        path_text = str(record.get("path") or image_id)
        return {
            "image_id": image_id,
            "score": float(score),
            "path": path_text,
            "description": str(record.get("description") or ""),
            "subject": str(record.get("subject") or ""),
            "color": str(record.get("color") or ""),
            "action": str(record.get("action") or ""),
            "style": str(record.get("style") or ""),
        }

    def _milvus_hit_to_dict(self, hit: dict) -> dict:
        image_id = str(hit.get("image_id") or hit.get("pk") or "")
        return {
            "image_id": image_id,
            "score": float(hit.get("score") or 0.0),
            "path": str(hit.get("path") or image_id),
            "description": str(hit.get("description") or ""),
            "subject": str(hit.get("subject") or ""),
            "color": str(hit.get("color") or ""),
            "action": str(hit.get("action") or ""),
            "style": str(hit.get("style") or ""),
        }

    def resolve_path(self, image_id: str, image_root: Path | None = None) -> Path | None:
        milvus = self._ensure_milvus(1)
        record = milvus.get(image_id)
        if not record:
            record = self.store.get_record(image_id)
        if not record:
            return None
        roots: list[Path] = []
        if image_root:
            roots.append(Path(image_root))
        roots.extend(self.search_roots)
        return resolve_image_file(image_id, str(record.get("path") or ""), roots)

    def index_count(self) -> int:
        if self.milvus_store is not None:
            return self.milvus_store.count()
        return len(self._cache)

    def close(self) -> None:
        self.store.close()
        if self.milvus_store is not None:
            self.milvus_store.close()


def build_text_retriever(
    *,
    profile: str | None,
    output_dir: Path | None = None,
    metadata_db: Path | None = None,
    bge_device: str = "cuda",
    search_roots: list[Path] | None = None,
) -> PictureTextRetriever:
    resolved_output = output_dir or default_output_dir(profile)
    resolved_db = metadata_db or default_caption_metadata_db_path(profile)
    retriever = PictureTextRetriever(
        output_dir=resolved_output,
        metadata_db=resolved_db,
        bge_device=bge_device,
        search_roots=search_roots,
    )
    if search_roots:
        retriever.search_roots = [Path(p).resolve() for p in search_roots]
    return retriever
