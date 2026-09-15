from __future__ import annotations

import unittest

PREFIX = "video_clip:video:"


def media_id_from_pk(pk: str) -> str:
    if pk.startswith(PREFIX):
        return pk[len(PREFIX) :]
    return pk


def video_clip_pk(media_id: str) -> str:
    return f"{PREFIX}{media_id}"


class VideoClipPkTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        media_id = "demo_video_001"
        pk = video_clip_pk(media_id)
        self.assertEqual(pk, "video_clip:video:demo_video_001")
        self.assertEqual(media_id_from_pk(pk), media_id)

    def test_plain_pk(self) -> None:
        self.assertEqual(media_id_from_pk("other"), "other")


if __name__ == "__main__":
    unittest.main()
