"""Unified video search service (CLIP + hybrid) for API and CLI."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.parse import quote

from .clip.metadata_store import MetadataStore as ClipMetadataStore
from .clip.retriever_factory import build_retriever_from_layout
from .hybrid.metadata_store import MetadataStore as HybridMetadataStore
from .portable_paths import resolve_portable_path
from .config import (
    DEFAULT_HYBRID_DENSE_TOP_K,
    DEFAULT_HYBRID_RRF_K,
    DEFAULT_HYBRID_SPARSE_TOP_K,
    DEFAULT_MODEL_PATH,
)
from .hybrid.hybrid_retrieval import HybridSearchConfig, build_search_engine
from .profile_paths import ProfileLayout, SearchSource, resolve_profile_layout
from .schemas import SearchRequest, SearchResponse, SearchResultBlock


def build_hybrid_engine(
    layout: ProfileLayout,
    *,
    embedding_device: str = "cuda",
    embedding_batch_size: int = 16,
    embedding_local_files_only: bool = False,
    sparse_top_k: int = DEFAULT_HYBRID_SPARSE_TOP_K,
    dense_top_k: int = DEFAULT_HYBRID_DENSE_TOP_K,
    rrf_k: int = DEFAULT_HYBRID_RRF_K,
):
    search_config = HybridSearchConfig(
        sparse_top_k=sparse_top_k,
        dense_top_k=dense_top_k,
        rrf_k=rrf_k,
    )
    sources = [
        SearchSource(
            name=layout.hybrid_profile,
            metadata_db_path=layout.hybrid_metadata_db,
            index_dir=layout.hybrid_index_dir,
        )
    ]
    return build_search_engine(
        sources=sources,
        search_config=search_config,
        embedding_device=embedding_device,
        embedding_batch_size=embedding_batch_size,
        embedding_local_files_only=embedding_local_files_only,
    )


def _format_segments(segments: list[dict]) -> str:
    if not segments:
        return ""
    parts = []
    for seg in segments[:3]:
        start = seg.get("start", 0)
        end = seg.get("end", start)
        score = seg.get("score")
        if score is not None:
            parts.append(f"{start:.1f}-{end:.1f}s ({score:.3f})")
        else:
            parts.append(f"{start:.1f}-{end:.1f}s")
    return " · ".join(parts)


def _normalize_clip_hit(item: dict) -> dict:
    segments = list(item.get("segments") or [])
    seg_line = _format_segments(segments)
    return {
        "video_id": str(item.get("video_id") or ""),
        "score": float(item.get("score") or 0.0),
        "description": seg_line,
        "display_line": seg_line or "Chinese-CLIP 整片检索",
        "tags": [],
        "segments": segments,
        "video_path": str(item.get("video_path") or ""),
        "sparse_rank": None,
        "dense_rank": None,
    }


def _normalize_hybrid_hit(item: dict) -> dict:
    tags = item.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    description = str(item.get("description") or item.get("caption") or "").strip()
    return {
        "video_id": str(item.get("video_id") or ""),
        "score": float(item.get("score") or 0.0),
        "description": description,
        "display_line": description,
        "tags": [str(t) for t in tags if str(t).strip()],
        "segments": list(item.get("segments") or []),
        "video_path": str(item.get("video_path") or ""),
        "sparse_rank": item.get("sparse_rank"),
        "dense_rank": item.get("dense_rank"),
    }


class ClipRetrieverHolder:
    """Lazy-loaded Chinese-CLIP retriever (model + Milvus)."""

    def __init__(
        self,
        layout: ProfileLayout,
        *,
        model_path: str | Path,
        device: str,
    ):
        self.layout = layout
        self.model_path = str(model_path)
        self.device = device
        self._metadata_store = ClipMetadataStore(layout.clip_metadata_db)
        self._retriever = None
        self._starting = False
        self._error: str | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._lock:
            if self._retriever is not None or self._starting:
                return
            self._starting = True
            self._error = None

        retriever = None
        error = None
        try:
            retriever = build_retriever_from_layout(
                self.layout,
                model_path=self.model_path,
                device=self.device,
            )
        except Exception as exc:
            error = str(exc)

        with self._lock:
            self._retriever = retriever
            self._error = error
            self._starting = False

    def start_background(self) -> None:
        threading.Thread(target=self._ensure_started, daemon=True).start()

    def status(self) -> dict:
        with self._lock:
            frame_count = 0
            if self._retriever is not None:
                frame_count = self._retriever.indexed_frame_count()
            return {
                "ready": self._retriever is not None,
                "starting": self._starting,
                "error": self._error,
                "profile": self.layout.clip_profile,
                "index_frames": frame_count,
            }

    def require_retriever(self):
        self._ensure_started()
        with self._lock:
            if self._retriever is not None:
                return self._retriever
            if self._starting:
                raise RuntimeError("CLIP 检索引擎加载中，请稍后重试。")
            raise RuntimeError(self._error or "CLIP 检索引擎不可用")

    def index_count(self) -> int:
        try:
            return self.require_retriever().indexed_frame_count()
        except RuntimeError:
            return 0

    def search(self, query: str, *, top_k: int) -> tuple[list[dict], float]:
        retriever = self.require_retriever()
        started = time.perf_counter()
        raw = retriever.search(query, top_k=top_k)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return [_normalize_clip_hit(item) for item in raw], elapsed_ms

    def resolve_video_path(self, video_id: str, *, portable_resolve) -> Path | None:
        """Resolve path from Milvus media_video_clip or local metadata.db."""
        raw_path: str | None = None
        with self._lock:
            retriever = self._retriever
        if retriever is not None:
            raw_path = retriever.resolve_video_path(video_id)
        if not raw_path:
            records = self._metadata_store.get_video_records([video_id])
            if records:
                raw_path = str(records[0].get("path") or "").strip() or None
        if not raw_path:
            return None
        try:
            path = portable_resolve(raw_path)
            return path if path.is_file() else None
        except OSError:
            return None

    def close(self) -> None:
        with self._lock:
            retriever = self._retriever
            self._retriever = None
        if retriever is not None:
            retriever.close()
        else:
            self._metadata_store.close()


class HybridEngineHolder:
    """Lazy-loaded hybrid search engine."""

    def __init__(
        self,
        layout: ProfileLayout,
        *,
        embedding_device: str,
        embedding_batch_size: int,
        embedding_local_files_only: bool,
        sparse_top_k: int,
        dense_top_k: int,
        rrf_k: int,
    ):
        self.layout = layout
        self.embedding_device = embedding_device
        self.embedding_batch_size = embedding_batch_size
        self.embedding_local_files_only = embedding_local_files_only
        self.sparse_top_k = sparse_top_k
        self.dense_top_k = dense_top_k
        self.rrf_k = rrf_k
        self._metadata_store = HybridMetadataStore(layout.hybrid_metadata_db)
        self._engine = None
        self._starting = False
        self._error: str | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._lock:
            if self._engine is not None or self._starting:
                return
            self._starting = True
            self._error = None

        engine = None
        error = None
        try:
            engine = build_hybrid_engine(
                self.layout,
                embedding_device=self.embedding_device,
                embedding_batch_size=self.embedding_batch_size,
                embedding_local_files_only=self.embedding_local_files_only,
                sparse_top_k=self.sparse_top_k,
                dense_top_k=self.dense_top_k,
                rrf_k=self.rrf_k,
            )
            engine.warmup()
        except Exception as exc:
            error = str(exc)

        with self._lock:
            self._engine = engine
            self._error = error
            self._starting = False

    def start_background(self) -> None:
        threading.Thread(target=self._ensure_started, daemon=True).start()

    def status(self) -> dict:
        with self._lock:
            return {
                "ready": self._engine is not None,
                "starting": self._starting,
                "error": self._error,
                "profile": self.layout.hybrid_profile,
            }

    def require_engine(self):
        self._ensure_started()
        with self._lock:
            if self._engine is not None:
                return self._engine
            if self._starting:
                raise RuntimeError("混合检索引擎加载中，请稍后重试。")
            raise RuntimeError(self._error or "混合检索引擎不可用")

    def index_count(self) -> int:
        try:
            engine = self.require_engine()
            return len(getattr(engine, "documents_by_id", {}) or {})
        except RuntimeError:
            return 0

    def search(
        self,
        query: str,
        *,
        top_k: int,
        sparse_top_k: int | None = None,
        dense_top_k: int | None = None,
        rrf_k: int | None = None,
    ) -> tuple[list[dict], float]:
        engine = self.require_engine()
        started = time.perf_counter()
        raw = engine.search(
            query,
            top_k=top_k,
            sparse_top_k=sparse_top_k or self.sparse_top_k,
            dense_top_k=dense_top_k or self.dense_top_k,
            rrf_k=rrf_k or self.rrf_k,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        results = []
        for item in raw:
            row = _normalize_hybrid_hit(item)
            raw_path = row.get("video_path") or ""
            if raw_path:
                row["video_path"] = str(resolve_portable_path(raw_path))
            results.append(row)
        return results, elapsed_ms

    def resolve_video_path(self, video_id: str) -> Path:
        """Resolve local file path from metadata only (does not require hybrid engine load)."""
        records = self._metadata_store.get_video_records([video_id])
        if not records:
            raise FileNotFoundError(f"video_id not found: {video_id}")
        raw_path = str(records[0].get("path") or "").strip()
        if not raw_path:
            raise FileNotFoundError(f"video path missing for {video_id}")
        path = resolve_portable_path(raw_path)
        if not path.is_file():
            raise FileNotFoundError(f"video file missing: {path}")
        return path

    def close(self) -> None:
        with self._lock:
            engine = self._engine
            self._engine = None
        if engine is not None:
            engine.close()
        self._metadata_store.close()


class VideoSearchService:
    """Unified video search across CLIP and hybrid pipelines."""

    def __init__(
        self,
        layout: ProfileLayout,
        *,
        model_path: str | Path | None = None,
        clip_device: str = "cuda",
        hybrid_device: str = "cuda",
        hybrid_batch_size: int = 16,
        hybrid_local_files_only: bool = False,
        sparse_top_k: int = DEFAULT_HYBRID_SPARSE_TOP_K,
        dense_top_k: int = DEFAULT_HYBRID_DENSE_TOP_K,
        rrf_k: int = DEFAULT_HYBRID_RRF_K,
        preload_hybrid: bool = True,
        preload_clip: bool = True,
    ):
        self.layout = layout
        self._resolve_portable_path = resolve_portable_path
        resolved_model = str(model_path or DEFAULT_MODEL_PATH)
        self._clip = ClipRetrieverHolder(
            layout,
            model_path=resolved_model,
            device=clip_device,
        )
        self._hybrid = HybridEngineHolder(
            layout,
            embedding_device=hybrid_device,
            embedding_batch_size=hybrid_batch_size,
            embedding_local_files_only=hybrid_local_files_only,
            sparse_top_k=sparse_top_k,
            dense_top_k=dense_top_k,
            rrf_k=rrf_k,
        )
        if preload_clip:
            self._clip.start_background()
        if preload_hybrid:
            self._hybrid.start_background()

    @classmethod
    def from_profiles(
        cls,
        profile: str | None = None,
        *,
        clip_profile: str | None = None,
        hybrid_profile: str | None = None,
        **kwargs,
    ) -> VideoSearchService:
        layout = resolve_profile_layout(
            profile,
            clip_profile=clip_profile,
            hybrid_profile=hybrid_profile,
        )
        return cls(layout, **kwargs)

    @staticmethod
    def _resolve_hit_path(
        row: dict,
        *,
        resolver,
        portable_resolve,
    ) -> Path | None:
        raw_path = row.get("video_path")
        if raw_path:
            candidate = Path(str(raw_path))
            if candidate.is_file():
                return candidate
            try:
                resolved = portable_resolve(str(raw_path))
                if resolved.is_file():
                    return resolved
            except OSError:
                return None
        return resolver(str(row.get("video_id") or ""))

    def _attach_media(self, results: list[dict], *, mode: str) -> list[dict]:
        resolver = self.resolve_clip_path if mode == "clip" else self.resolve_hybrid_path
        enriched = []
        for item in results:
            row = dict(item)
            vid = str(row.get("video_id") or "")
            path = self._resolve_hit_path(
                row,
                resolver=resolver,
                portable_resolve=self._resolve_portable_path,
            )
            row["video_available"] = path is not None
            if path is not None:
                row["resolved_path"] = str(path)
                row["video_url"] = f"/media/{mode}/{quote(vid, safe='')}"
            enriched.append(row)
        return enriched

    def search(
        self, request: SearchRequest | str, *, top_k: int | None = None
    ) -> dict | SearchResponse:
        if isinstance(request, str):
            req = SearchRequest(query=request, top_k=top_k or 10)
        else:
            req = request
            if top_k is not None:
                req = req.model_copy(update={"top_k": top_k})

        started = time.perf_counter()
        mode = req.mode
        clip_block: SearchResultBlock | None = None
        hybrid_block: SearchResultBlock | None = None

        if mode in ("clip", "both"):
            clip_error = None
            try:
                clip_raw, clip_ms = self._clip.search(req.query, top_k=req.top_k)
            except RuntimeError as exc:
                clip_raw, clip_ms = [], 0.0
                clip_error = str(exc)
            else:
                clip_error = self._clip.status().get("error")
            clip_status = self._clip.status()
            clip_block = SearchResultBlock(
                mode="clip",
                profile=self.layout.clip_profile,
                index_count=clip_status["index_frames"],
                elapsed_ms=round(clip_ms, 1),
                ready=clip_status["ready"],
                error=clip_error,
                results=self._attach_media(clip_raw, mode="clip"),
            )

        if mode in ("hybrid", "both"):
            hybrid_error = None
            try:
                hybrid_raw, hybrid_ms = self._hybrid.search(
                    req.query,
                    top_k=req.top_k,
                    sparse_top_k=req.sparse_top_k,
                    dense_top_k=req.dense_top_k,
                    rrf_k=req.rrf_k,
                )
            except RuntimeError as exc:
                hybrid_raw, hybrid_ms = [], 0.0
                hybrid_error = str(exc)
            else:
                hybrid_error = self._hybrid.status().get("error")

            hybrid_block = SearchResultBlock(
                mode="hybrid",
                profile=self.layout.hybrid_profile,
                index_count=self._hybrid.index_count(),
                elapsed_ms=round(hybrid_ms, 1),
                ready=self._hybrid.status()["ready"],
                error=hybrid_error,
                results=self._attach_media(hybrid_raw, mode="hybrid"),
            )

        response = SearchResponse(
            query=req.query,
            mode=mode,
            top_k=req.top_k,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
            clip=clip_block,
            hybrid=hybrid_block,
        )
        return response.model_dump()

    def resolve_clip_path(self, video_id: str) -> Path | None:
        return self._clip.resolve_video_path(
            video_id, portable_resolve=self._resolve_portable_path
        )

    def resolve_hybrid_path(self, video_id: str) -> Path | None:
        try:
            return self._hybrid.resolve_video_path(video_id)
        except (FileNotFoundError, RuntimeError):
            return None

    def health(self) -> dict:
        clip_status = self._clip.status()
        hybrid_status = self._hybrid.status()
        return {
            "profile": self.layout.profile,
            "clip_profile": self.layout.clip_profile,
            "hybrid_profile": self.layout.hybrid_profile,
            "unified_layout": self.layout.unified,
            "clip_ready": clip_status["ready"],
            "clip_starting": clip_status["starting"],
            "clip_error": clip_status.get("error"),
            "clip_index_frames": clip_status["index_frames"],
            "hybrid_index_videos": self._hybrid.index_count(),
            "hybrid_ready": hybrid_status["ready"],
            "hybrid_starting": hybrid_status["starting"],
            "hybrid_error": hybrid_status.get("error"),
            "clip_output_dir": str(self.layout.clip_output_dir),
            "hybrid_index_dir": str(self.layout.hybrid_index_dir),
        }

    def close(self) -> None:
        self._clip.close()
        self._hybrid.close()
