from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from .config import RetrievalConfig
from .embedding import ChineseClipEncoder
from .ids import media_id_from_pk, video_clip_pk

if TYPE_CHECKING:
    from embed_core.milvus_vector_index import MilvusVectorIndex

    from .metadata_store import MetadataStore


class VideoRetriever:
    """Text/image query against whole-video CLIP vectors in media_video_clip."""

    def __init__(
        self,
        encoder: ChineseClipEncoder,
        index: MilvusVectorIndex,
        retrieval_config: RetrievalConfig,
        metadata_store: MetadataStore | None = None,
    ):
        self.encoder = encoder
        self.index = index
        self.retrieval_config = retrieval_config
        self.metadata_store = metadata_store

    def indexed_frame_count(self) -> int:
        """Indexed video count (kept name for API compatibility)."""
        return self.indexed_video_count()

    def indexed_video_count(self) -> int:
        return self.index.count()

    def close(self) -> None:
        self.index.close()
        if self.metadata_store is not None:
            self.metadata_store.close()

    def resolve_video_path(self, video_id: str) -> str | None:
        pk = video_clip_pk(video_id)
        row = self.index.store.get(pk)
        if row:
            path = str(row.get("object_key") or row.get("path") or "").strip()
            if path:
                return path
        if self.metadata_store is None:
            return None
        records = self.metadata_store.get_video_records([video_id])
        if not records:
            return None
        return str(records[0].get("path") or "").strip() or None

    def search(self, query: str, top_k: int | None = None) -> list[dict]:
        query_embeddings = self.encoder.encode_texts([query])
        if query_embeddings.size == 0:
            query_embeddings = self.encoder.encode_texts([query])

        limit = top_k or self.retrieval_config.result_videos
        recall_k = max(limit, self.retrieval_config.video_recall_top_k)
        hit_lists = self.index.search_entities(
            query_embeddings,
            top_k=recall_k,
            output_fields=["pk", "media_id", "object_key", "path"],
        )

        score_by_video: dict[str, dict] = {}
        for hits in hit_lists:
            for hit in hits:
                pk = str(hit.get("pk") or "")
                media_id = str(hit.get("media_id") or media_id_from_pk(pk)).strip()
                if not media_id:
                    continue
                score = float(hit.get("score", 0.0))
                video_path = str(hit.get("object_key") or hit.get("path") or "").strip()
                existing = score_by_video.get(media_id)
                if existing is None or score > float(existing["score"]):
                    score_by_video[media_id] = {
                        "video_id": media_id,
                        "score": score,
                        "segments": [],
                        "video_path": video_path,
                    }

        results = sorted(score_by_video.values(), key=lambda item: item["score"], reverse=True)
        for item in results:
            item["score"] = round(float(item["score"]), 4)
        return results[:limit]
