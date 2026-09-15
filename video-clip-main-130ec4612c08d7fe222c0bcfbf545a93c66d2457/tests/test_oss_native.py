from __future__ import annotations

import io
import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.core.cutter import _build_cut_command, _cut_segment_to_oss_job
from backend.storage import oss_client
from backend.storage.database import MemoryStore
from backend.models.schemas import TaskMode, TaskStatus
from backend.services import job_service


class FakeBucket:
    def __init__(self, *, fail_part: bool = False):
        self.fail_part = fail_part
        self.parts: dict[int, bytes] = {}
        self.objects: dict[str, bytes] = {}
        self.aborted = False

    def init_multipart_upload(self, key, headers=None):
        self.key = key
        self.headers = headers
        return SimpleNamespace(upload_id="upload-1")

    def upload_part(self, key, upload_id, part_number, data):
        if self.fail_part:
            raise RuntimeError("upload failed")
        self.parts[part_number] = bytes(data)
        return SimpleNamespace(etag=f"etag-{part_number}")

    def complete_multipart_upload(self, key, upload_id, parts):
        self.objects[key] = b"".join(self.parts[n] for n in sorted(self.parts))

    def abort_multipart_upload(self, key, upload_id):
        self.aborted = True


class OssNativeTests(unittest.TestCase):
    def test_preview_url_falls_back_to_signed_oss_when_cdn_fails(self):
        class FailedResponse:
            status = 404
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        with (
            patch("backend.storage.oss_client.cdn_url", return_value="https://cdn/x.mp4"),
            patch("backend.storage.oss_client.urlopen", return_value=FailedResponse()),
            patch.object(oss_client, "sign_url", return_value="https://oss/signed") as signed,
        ):
            self.assertEqual(oss_client.preview_url("x.mp4"), "https://oss/signed")
            signed.assert_called_once_with("x.mp4", prefer_cdn=False)

    def test_fragmented_mp4_command_targets_stdout(self):
        cmd = _build_cut_command(
            "https://example.invalid/source.mp4",
            "pipe:1",
            0,
            2,
            True,
            fps=25,
            start_frame=0,
            end_frame=50,
            fragmented=True,
        )
        self.assertEqual(cmd[-3:], ["-f", "mp4", "pipe:1"])
        self.assertIn("+frag_keyframe+empty_moov+default_base_moof", cmd)

    def test_multipart_upload_streams_and_completes(self):
        bucket = FakeBucket()
        with patch.object(oss_client, "_get_bucket", return_value=bucket):
            oss_client.multipart_upload_fileobj(
                "stage/a.mp4",
                io.BytesIO(b"abcdefgh"),
                part_size=3,
                content_type="video/mp4",
            )
        self.assertEqual(bucket.objects["stage/a.mp4"], b"abcdefgh")
        self.assertEqual(bucket.headers, {"Content-Type": "video/mp4"})

    def test_multipart_failure_aborts(self):
        bucket = FakeBucket(fail_part=True)
        with patch.object(oss_client, "_get_bucket", return_value=bucket):
            with self.assertRaises(oss_client.OssError):
                oss_client.multipart_upload_fileobj(
                    "stage/a.mp4", io.BytesIO(b"abc"), part_size=3
                )
        self.assertTrue(bucket.aborted)

    def test_segment_tracks_staging_separately_from_published_key(self):
        store = MemoryStore()
        store.create_task("job", "video", TaskMode.MANUAL)
        store.create_segments(
            "job",
            [{
                "id": "seg",
                "index": 1,
                "start_time": 0,
                "end_time": 1,
                "source": "manual",
                "staging_oss_key": "_staging/seg.mp4",
                "thumb_oss_key": "_staging/seg.jpg",
            }],
        )
        segment = store.get_segment("seg")
        self.assertEqual(segment["staging_oss_key"], "_staging/seg.mp4")
        self.assertIsNone(segment["oss_key"])

    def test_real_ffmpeg_stdout_is_validated_without_media_files_in_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            staged = root / "staged.mp4"
            generated = subprocess.run(
                [
                    "/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25",
                    "-t", "1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(source),
                ],
                capture_output=True,
            )
            if generated.returncode != 0:
                self.skipTest("system FFmpeg has no libx264")

            def consume(_key, stream, **_kwargs):
                with staged.open("wb") as out:
                    while chunk := stream.read(64 * 1024):
                        out.write(chunk)

            with (
                patch.object(oss_client, "delete_object"),
                patch.object(oss_client, "multipart_upload_fileobj", side_effect=consume),
                patch.object(oss_client, "sign_url", return_value=str(staged)),
                patch.object(oss_client, "upload_file"),
            ):
                index, ok, error, key, _thumb = _cut_segment_to_oss_job(
                    str(source), "job", 1, 0, 1, False, 25, 0, 25
                )
            self.assertEqual(index, 1)
            self.assertTrue(ok, error)
            self.assertTrue(key and key.endswith("segment_001.mp4"))
            self.assertGreater(staged.stat().st_size, 0)

    def test_publish_returns_all_and_selected_without_copying(self):
        store = MemoryStore()
        store.create_video(
            "video", "source.mp4", "", {
                "duration": 2, "width": 160, "height": 90, "fps": 25,
                "codec": "h264", "size_bytes": 100,
            }, oss_key="source/source.mp4",
        )
        store.create_task("job", "video", TaskMode.MANUAL)
        store.update_task("job", status=TaskStatus.DONE)
        store.create_segments("job", [{
            "id": "seg", "index": 1, "start_time": 0, "end_time": 1,
            "source": "manual", "status": "done",
            "staging_oss_key": "stage/job/segment.mp4",
            "thumb_oss_key": "stage/job/segment.jpg",
        }])
        copied: list[tuple[str, str]] = []
        with (
            patch.object(job_service, "db", store),
            patch.object(oss_client, "is_enabled", return_value=True),
            patch.object(oss_client, "object_exists", return_value=True),
            patch.object(oss_client, "copy_object", side_effect=lambda a, b: copied.append((a, b)) or b),
            patch.object(oss_client, "delete_object"),
            patch.object(oss_client, "preview_url", side_effect=lambda key: f"https://cdn/{key}"),
        ):
            result = asyncio.run(job_service.publish("job", segment_ids=["seg"], oss_key="final/"))
        self.assertEqual(len(result.all), 1)
        self.assertEqual(len(result.selected), 1)
        self.assertEqual(copied, [])
        segment = store.get_segment("seg")
        self.assertIsNone(segment["oss_key"])
        self.assertEqual(result.selected[0].oss_key, "stage/job/segment.mp4")

    def test_clips_object_key_uses_source_parent(self):
        self.assertEqual(
            oss_client.clips_object_key("a/b/c/1.mp4", "segment_001.mp4"),
            "a/b/c/clips/segment_001.mp4",
        )
        self.assertEqual(
            oss_client.clips_object_key("1.mp4", "segment_001.mp4"),
            "clips/segment_001.mp4",
        )


if __name__ == "__main__":
    unittest.main()
