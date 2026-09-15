from __future__ import annotations

import unittest


def embedding_dim(payload: dict) -> int:
    embedding = payload.get("embedding")
    if isinstance(embedding, list):
        return len(embedding)
    video_level = payload.get("video_level")
    if isinstance(video_level, dict) and isinstance(video_level.get("embedding"), list):
        return len(video_level["embedding"])
    return int(payload.get("vector_dim") or 0)


class VideoClipPayloadTests(unittest.TestCase):
    def test_new_shape(self) -> None:
        payload = {"embedding": [0.1] * 1024, "vector_dim": 1024}
        self.assertEqual(embedding_dim(payload), 1024)

    def test_legacy_video_level(self) -> None:
        payload = {"video_level": {"embedding": [0.2] * 512}}
        self.assertEqual(embedding_dim(payload), 512)


if __name__ == "__main__":
    unittest.main()
