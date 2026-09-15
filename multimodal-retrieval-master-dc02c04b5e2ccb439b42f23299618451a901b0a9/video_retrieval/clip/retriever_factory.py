from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from embed_core.milvus_store import milvus_enabled

from ..config import DEFAULT_MODEL_PATH
from ..env_loader import load_default_dotenv_files
from ..profile_paths import ProfileLayout
from .config import RetrievalConfig
from .embedding import ChineseClipEncoder
from .metadata_store import MetadataStore
from .milvus_index import load_media_video_clip_index
from .retrieval import VideoRetriever


def build_retriever(
    *,
    output_dir: str | Path,
    metadata_db_path: str | Path,
    model_path: str | None = None,
    profile: str | None = None,
    device: str = "cuda",
    batch_size: int = 16,
    video_recall_top_k: int | None = None,
    segment_recall_top_k: int | None = None,
    video_recall_candidate_pool_size: int | None = None,
    segment_recall_candidate_pool_size: int | None = None,
    rerank_top_k_average: int | None = None,
    rerank_smoothmax_beta: float | None = None,
    clip_score_weight: float | None = None,
    motion_score_weight: float | None = None,
    rerank_segment_support_weight: float | None = None,
    rerank_genericness_penalty_weight: float | None = None,
) -> VideoRetriever:
    del output_dir, profile, segment_recall_top_k, segment_recall_candidate_pool_size
    del rerank_top_k_average, rerank_smoothmax_beta, clip_score_weight, motion_score_weight
    del rerank_segment_support_weight, rerank_genericness_penalty_weight

    load_default_dotenv_files()
    if not milvus_enabled():
        raise RuntimeError(
            "Video CLIP retrieval requires Milvus (VECTOR_STORE=milvus). "
            "Set VECTOR_STORE=none only when not using vector search."
        )

    index = load_media_video_clip_index()

    overrides: dict[str, int] = {}
    if video_recall_top_k is not None:
        overrides["video_recall_top_k"] = video_recall_top_k
    if video_recall_candidate_pool_size is not None:
        overrides["video_recall_candidate_pool_size"] = video_recall_candidate_pool_size
    retrieval_config = replace(RetrievalConfig(), **overrides)

    encoder = ChineseClipEncoder(
        model_path=model_path or str(DEFAULT_MODEL_PATH), device=device, batch_size=batch_size
    )
    metadata_db = Path(metadata_db_path)
    metadata_store = MetadataStore(metadata_db) if metadata_db.is_file() else None
    return VideoRetriever(
        encoder=encoder,
        index=index,
        retrieval_config=retrieval_config,
        metadata_store=metadata_store,
    )


def build_retriever_from_layout(
    layout: ProfileLayout,
    *,
    model_path: str | Path | None = None,
    device: str = "cuda",
    batch_size: int = 16,
    **kwargs,
) -> VideoRetriever:
    """Build a retriever from unified profile layout (preferred entry for services)."""
    return build_retriever(
        output_dir=layout.clip_output_dir,
        metadata_db_path=layout.clip_metadata_db,
        model_path=str(model_path or DEFAULT_MODEL_PATH),
        profile=layout.clip_profile,
        device=device,
        batch_size=batch_size,
        **kwargs,
    )
