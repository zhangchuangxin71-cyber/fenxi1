from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class SegmentRepresentation:
    segment_id: str
    video_id: str
    importance_score: float
    motion_score: float
    visual_diversity_score: float
    embedding_norm_score: float
    genericness_score: float = 0.0
    representative_rank: int = 0
    is_representative: bool = False


def compute_segment_motion_and_diversity(frame_embeddings: np.ndarray) -> tuple[float, float]:
    if frame_embeddings.shape[0] <= 1:
        return 0.0, 0.0

    normalized = frame_embeddings.astype(np.float32)
    normalized = normalized / np.clip(
        np.linalg.norm(normalized, axis=1, keepdims=True), a_min=1e-12, a_max=None
    )

    adjacent_sims = np.sum(normalized[1:] * normalized[:-1], axis=1)
    motion_score = float(np.mean(1.0 - np.clip(adjacent_sims, -1.0, 1.0)))

    similarity_matrix = normalized @ normalized.T
    upper = similarity_matrix[np.triu_indices(similarity_matrix.shape[0], k=1)]
    visual_diversity = float(np.mean(1.0 - np.clip(upper, -1.0, 1.0))) if upper.size else 0.0
    return motion_score, visual_diversity


def compute_frame_diff_motion_score(frame_paths: list[str]) -> float:
    if len(frame_paths) <= 1:
        return 0.0

    grayscale_frames: list[np.ndarray] = []
    target_size: tuple[int, int] | None = None
    for frame_path in frame_paths:
        image = cv2.imread(frame_path, cv2.IMREAD_GRAYSCALE)
        if image is None:
            continue
        if target_size is None:
            target_size = (image.shape[1], image.shape[0])
        elif (image.shape[1], image.shape[0]) != target_size:
            image = cv2.resize(image, target_size, interpolation=cv2.INTER_AREA)
        grayscale_frames.append(image.astype(np.float32) / 255.0)

    if len(grayscale_frames) <= 1:
        return 0.0

    frame_diffs = []
    for previous, current in zip(grayscale_frames[:-1], grayscale_frames[1:], strict=False):
        frame_diffs.append(float(np.mean(np.abs(current - previous))))
    return float(np.mean(frame_diffs)) if frame_diffs else 0.0


def compute_temporal_coverage_score(
    frame_timestamps: np.ndarray,
    segment_duration_seconds: float | None,
) -> float:
    if frame_timestamps.size <= 1:
        return 0.0
    sorted_timestamps = np.sort(frame_timestamps.astype(np.float32))
    observed_span = float(sorted_timestamps[-1] - sorted_timestamps[0])
    if observed_span <= 0:
        return 0.0
    if segment_duration_seconds is None or segment_duration_seconds <= 0:
        denominator = observed_span
    else:
        denominator = float(segment_duration_seconds)
    return float(np.clip(observed_span / max(denominator, 1e-6), 0.0, 1.0))


def compute_segment_importance(
    frame_embeddings: np.ndarray,
    frame_norms: np.ndarray,
    frame_timestamps: np.ndarray,
    segment_duration_seconds: float | None,
    frame_diff_motion_score: float,
    embedding_norm_weight: float,
    motion_score_weight: float,
    visual_diversity_weight: float,
) -> tuple[float, float, float, float]:
    embedding_norm_score = (
        float(np.mean(frame_norms.astype(np.float32))) if frame_norms.size else 0.0
    )
    _, visual_diversity_score = compute_segment_motion_and_diversity(frame_embeddings)
    temporal_coverage_score = compute_temporal_coverage_score(
        frame_timestamps=frame_timestamps,
        segment_duration_seconds=segment_duration_seconds,
    )
    motion_score = (0.8 * frame_diff_motion_score) + (0.2 * temporal_coverage_score)
    importance_score = (
        embedding_norm_weight * embedding_norm_score
        + motion_score_weight * motion_score
        + visual_diversity_weight * visual_diversity_score
    )
    return importance_score, motion_score, visual_diversity_score, embedding_norm_score


def compute_global_genericness(segment_embeddings: dict[str, np.ndarray]) -> dict[str, float]:
    if not segment_embeddings:
        return {}

    segment_ids = list(segment_embeddings.keys())
    matrix = np.vstack(
        [segment_embeddings[segment_id].astype(np.float32) for segment_id in segment_ids]
    ).astype(np.float32)
    matrix = matrix / np.clip(
        np.linalg.norm(matrix, axis=1, keepdims=True), a_min=1e-12, a_max=None
    )
    similarity = matrix @ matrix.T
    np.fill_diagonal(similarity, 0.0)
    genericness = similarity.mean(axis=1)
    return {segment_id: float(genericness[idx]) for idx, segment_id in enumerate(segment_ids)}


def compute_pairwise_segment_similarity(
    segment_embeddings: dict[str, np.ndarray],
) -> dict[str, dict[str, float]]:
    if not segment_embeddings:
        return {}

    segment_ids = list(segment_embeddings.keys())
    matrix = np.vstack(
        [segment_embeddings[segment_id].astype(np.float32) for segment_id in segment_ids]
    ).astype(np.float32)
    matrix = matrix / np.clip(
        np.linalg.norm(matrix, axis=1, keepdims=True), a_min=1e-12, a_max=None
    )
    similarity = matrix @ matrix.T
    return {
        segment_id: {
            other_segment_id: float(similarity[row_idx, col_idx])
            for col_idx, other_segment_id in enumerate(segment_ids)
            if other_segment_id != segment_id
        }
        for row_idx, segment_id in enumerate(segment_ids)
    }


def select_top_representative_segments(
    segments: list[SegmentRepresentation],
    top_n: int,
    pairwise_similarity: dict[str, dict[str, float]] | None = None,
    genericness_by_segment_id: dict[str, float] | None = None,
    genericness_weight: float = 0.0,
    diversity_penalty_weight: float = 0.0,
) -> list[SegmentRepresentation]:
    selected: list[SegmentRepresentation] = []
    candidates = list(segments)
    similarity_lookup = pairwise_similarity or {}
    genericness_lookup = genericness_by_segment_id or {}

    while candidates and len(selected) < max(1, top_n):
        best_item = None
        best_score = float("-inf")
        for item in candidates:
            item.genericness_score = float(
                genericness_lookup.get(item.segment_id, item.genericness_score)
            )
            base_score = item.importance_score - (genericness_weight * item.genericness_score)
            diversity_penalty = 0.0
            if selected:
                diversity_penalty = diversity_penalty_weight * max(
                    (
                        float(similarity_lookup.get(item.segment_id, {}).get(other.segment_id, 0.0))
                        for other in selected
                    ),
                    default=0.0,
                )
            final_score = base_score - diversity_penalty
            if final_score > best_score:
                best_score = final_score
                best_item = item

        if best_item is None:
            break
        item = best_item
        candidates = [
            candidate for candidate in candidates if candidate.segment_id != item.segment_id
        ]
        selected.append(item)

    for idx, item in enumerate(selected, start=1):
        item.representative_rank = idx
        item.is_representative = True
    return selected
