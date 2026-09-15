"""Chinese-CLIP retrieval configuration and tunable recall/rerank defaults."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import PACKAGE_ROOT, WORKSPACE_ROOT

# Backward-compatible aliases
CLIP_ROOT = PACKAGE_ROOT / "clip"
VIDEO_RETRIEVAL_ROOT = PACKAGE_ROOT
PROJECT_ROOT = PACKAGE_ROOT


@dataclass
class RetrievalConfig:
    video_recall_top_k: int = 64
    segment_recall_top_k: int = 64
    video_recall_candidate_pool_size: int = 128
    segment_recall_candidate_pool_size: int = 128
    rerank_top_k_average: int = 3
    video_genericness_penalty_weight: float = 0.1
    clip_score_weight: float = 0.85
    motion_score_weight: float = 0.1
    merge_gap_seconds: float = 2.0
    temporal_consistency_threshold: float = 0.9
    clip_score_top_k: int = 3
    clip_avg_score_weight: float = 0.7
    clip_temporal_consistency_weight: float = 0.3
    clip_smoothmax_beta: float = 12.0
    rerank_smoothmax_beta: float = 10.0
    rerank_support_floor: float = 0.18
    rerank_support_bonus_weight: float = 0.08
    rerank_spike_penalty_weight: float = 0.06
    rerank_segment_support_weight: float = 0.2
    rerank_genericness_penalty_weight: float = 0.05
    rerank_segment_support_top_k: int = 3
    max_segments_per_video: int = 3
    result_videos: int = 5
