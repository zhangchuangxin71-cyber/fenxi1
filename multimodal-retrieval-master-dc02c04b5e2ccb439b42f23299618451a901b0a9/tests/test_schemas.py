from __future__ import annotations

import unittest

from pydantic import ValidationError

from embedding_service.schemas import ImageClipEmbedRequest, VideoClipConfiguration


class SchemaTests(unittest.TestCase):
    def test_image_clip_request(self) -> None:
        req = ImageClipEmbedRequest(
            object_key="images/a.jpg",
            content_type="image/jpeg",
            media_id="img-001",
        )
        self.assertEqual(req.object_key, "images/a.jpg")
        self.assertTrue(req.return_vector)

    def test_video_configuration_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            VideoClipConfiguration(sample_fps=0.05)

    def test_video_configuration_defaults(self) -> None:
        cfg = VideoClipConfiguration()
        self.assertEqual(cfg.sample_fps, 1.0)
        self.assertEqual(cfg.max_frames, 16)
        self.assertEqual(cfg.top_k_frames, 4)
        self.assertEqual(cfg.segment_seconds, 5)


if __name__ == "__main__":
    unittest.main()
