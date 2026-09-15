from __future__ import annotations

import numpy as np


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float32)
    if arr.ndim == 1:
        norm = float(np.linalg.norm(arr))
        if norm <= 1e-12:
            return arr
        return (arr / norm).astype(np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-12)
    return (arr / norms).astype(np.float32)


def average_pool_vectors(vectors: np.ndarray) -> np.ndarray:
    if vectors.size == 0:
        raise ValueError("empty vectors")
    if vectors.ndim == 1:
        return l2_normalize(vectors)
    return l2_normalize(vectors.mean(axis=0))
