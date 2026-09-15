from __future__ import annotations

import numpy as np
from sklearn.cluster import KMeans


def select_diverse_frame_indices(
    frame_embeddings: np.ndarray,
    *,
    top_k: int,
    random_state: int = 0,
) -> np.ndarray:
    """
    K-means diversity selection: pick up to top_k frames, one per cluster centroid.

    Embeddings are L2-normalized before clustering (cosine-friendly space).
    """
    if frame_embeddings.size == 0:
        return np.array([], dtype=np.int64)

    n = int(frame_embeddings.shape[0])
    k = min(max(1, int(top_k)), n)
    if k >= n:
        return np.arange(n, dtype=np.int64)

    normalized = frame_embeddings.astype(np.float32)
    norms = np.linalg.norm(normalized, axis=1, keepdims=True)
    normalized = normalized / np.clip(norms, 1e-12, None)

    kmeans = KMeans(n_clusters=k, n_init=10, random_state=random_state)
    labels = kmeans.fit_predict(normalized)
    centers = kmeans.cluster_centers_

    selected: list[int] = []
    for cluster_id in range(k):
        indices = np.where(labels == cluster_id)[0]
        if indices.size == 0:
            continue
        cluster_vecs = normalized[indices]
        dists = np.linalg.norm(cluster_vecs - centers[cluster_id], axis=1)
        selected.append(int(indices[int(np.argmin(dists))]))

    if not selected:
        return np.arange(min(k, n), dtype=np.int64)
    return np.sort(np.unique(np.asarray(selected, dtype=np.int64)))
