from __future__ import annotations

import unittest

from embedding_service.media_types import suffix_for_content_type


class MediaTypesTests(unittest.TestCase):
    def test_known_image_suffix(self) -> None:
        self.assertEqual(suffix_for_content_type("image/jpeg"), ".jpg")

    def test_known_video_suffix(self) -> None:
        self.assertEqual(suffix_for_content_type("video/mp4"), ".mp4")

    def test_unknown_mime_fallback(self) -> None:
        self.assertEqual(suffix_for_content_type("application/octet-stream"), ".octet-stream")

    def test_case_insensitive(self) -> None:
        self.assertEqual(suffix_for_content_type("IMAGE/PNG"), ".png")


if __name__ == "__main__":
    unittest.main()
