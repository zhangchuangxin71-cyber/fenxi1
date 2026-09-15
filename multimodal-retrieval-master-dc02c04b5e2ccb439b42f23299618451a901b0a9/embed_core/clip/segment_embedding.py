from __future__ import annotations

import numpy as np


def softmax_attention_pooling(
    embeddings: np.ndarray, scores: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Weighted softmax pooling; output is L2-normalized."""
    if embeddings.size == 0:
        raise ValueError("embeddings must not be empty")
    if scores.size == 0:
        raise ValueError("scores must not be empty")
    if embeddings.shape[0] != scores.shape[0]:
        raise ValueError("embeddings and scores must have the same length")
    scores = scores.astype(np.float32)
    scores = scores - np.max(scores)
    exp_scores = np.exp(scores)
    weights = exp_scores / np.clip(np.sum(exp_scores), a_min=1e-12, a_max=None)
    pooled = np.sum(embeddings * weights[:, None], axis=0)
    norm = np.linalg.norm(pooled)
    if norm > 0:
        pooled = pooled / norm
    return pooled.astype(np.float32), weights.astype(np.float32)
