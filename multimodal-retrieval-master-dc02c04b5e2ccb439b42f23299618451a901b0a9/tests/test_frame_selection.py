from __future__ import annotations

import unittest

import numpy as np

from embed_core.clip.frame_selection import select_diverse_frame_indices
from embed_core.clip.segment_embedding import softmax_attention_pooling


class FrameSelectionTests(unittest.TestCase):
    def test_select_all_when_fewer_than_top_k(self) -> None:
        embeddings = np.eye(3, dtype=np.float32)
        idx = select_diverse_frame_indices(embeddings, top_k=4)
        self.assertEqual(idx.tolist(), [0, 1, 2])

    def test_select_one_per_cluster(self) -> None:
        embeddings = np.vstack(
            [
                np.array([1.0, 0.0, 0.0], dtype=np.float32),
                np.array([0.99, 0.01, 0.0], dtype=np.float32),
                np.array([0.0, 1.0, 0.0], dtype=np.float32),
                np.array([0.0, 0.0, 1.0], dtype=np.float32),
            ]
        )
        idx = select_diverse_frame_indices(embeddings, top_k=2, random_state=0)
        self.assertEqual(idx.size, 2)
        self.assertEqual(len(set(idx.tolist())), 2)

    def test_segment_pooling_uses_l2_norm_weights(self) -> None:
        embeddings = np.array(
            [
                [1.0, 0.0],
                [0.0, 1.0],
            ],
            dtype=np.float32,
        )
        norms = np.linalg.norm(embeddings, axis=1)
        pooled, weights = softmax_attention_pooling(embeddings, norms)
        self.assertAlmostEqual(float(np.linalg.norm(pooled)), 1.0, places=5)
        self.assertGreater(float(weights[0]), float(weights[1]))


if __name__ == "__main__":
    unittest.main()
