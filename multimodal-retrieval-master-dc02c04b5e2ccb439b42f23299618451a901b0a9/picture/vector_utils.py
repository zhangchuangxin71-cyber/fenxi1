from __future__ import annotations

import numpy as np

from embed_core.vector_utils import l2_normalize

__all__ = ["l2_normalize", "average_pool_vectors"]


def average_pool_vectors(vectors: np.ndarray) -> np.ndarray:
    if vectors.size == 0:
        raise ValueError("empty vectors")
    if vectors.ndim == 1:
        return l2_normalize(vectors)
    return l2_normalize(vectors.mean(axis=0))
