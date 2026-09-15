from __future__ import annotations

import unittest

import numpy as np

from embed_core.vector_utils import average_pool_vectors, l2_normalize


class VectorUtilsTests(unittest.TestCase):
    def test_l2_normalize_unit_vector(self) -> None:
        vec = np.array([3.0, 4.0], dtype=np.float32)
        out = l2_normalize(vec)
        self.assertAlmostEqual(float(np.linalg.norm(out)), 1.0, places=5)
        np.testing.assert_allclose(out, [0.6, 0.8], rtol=1e-5)

    def test_l2_normalize_batch(self) -> None:
        batch = np.array([[3.0, 4.0], [0.0, 5.0]], dtype=np.float32)
        out = l2_normalize(batch)
        norms = np.linalg.norm(out, axis=1)
        np.testing.assert_allclose(norms, [1.0, 1.0], rtol=1e-5)

    def test_average_pool_vectors(self) -> None:
        vectors = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        out = average_pool_vectors(vectors)
        self.assertAlmostEqual(float(np.linalg.norm(out)), 1.0, places=5)

    def test_average_pool_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            average_pool_vectors(np.array([], dtype=np.float32))


if __name__ == "__main__":
    unittest.main()
