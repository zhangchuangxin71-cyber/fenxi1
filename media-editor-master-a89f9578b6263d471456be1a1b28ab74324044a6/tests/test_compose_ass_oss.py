from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from videoaudiotext.api.errors import ApiError
from videoaudiotext.api.jobs import _upload_compose_deliverables
from videoaudiotext.api.workspace.store import WorkspaceStore
from videoaudiotext.storage.oss import (
    ASS_CONTENT_TYPE,
    build_compose_object_keys,
    upload_file_oss,
)


class ComposeAssOssTests(unittest.TestCase):
    def _workspace(self, root: Path) -> tuple[WorkspaceStore, Path, Path]:
        store = WorkspaceStore(workspace_id="compose-test", root=root / "job")
        store.ensure_tree()
        store.output_mp4.write_bytes(b"video")

        scratch = store.root / "intermediate" / "compose" / "task-id"
        scratch.mkdir(parents=True)
        (scratch / "master_final.wav").write_bytes(b"audio")

        subtitle_ass = store.subtitles_dir / "active" / "subtitle.ass"
        subtitle_ass.parent.mkdir(parents=True)
        subtitle_ass.write_text("[Script Info]\n", encoding="utf-8")

        log_path = root / "compose.log"
        log_path.write_text("compose ok\n", encoding="utf-8")
        return store, scratch, log_path

    def test_compose_object_keys_include_subtitle_ass(self) -> None:
        with patch.dict(
            "os.environ",
            {"ALIYUN_OSS_PREFIX": "prod/ImagesVideosText2Video"},
        ):
            keys = build_compose_object_keys(
                code="demo",
                user_id="user-1",
                job_id="task-id",
                day=date(2026, 8, 10),
            )

        self.assertEqual(
            keys["subtitle_ass"],
            "prod/ImagesVideosText2Video/demo-user-1/2026-08-10/task-id_subtitle.ass",
        )

    def test_upload_compose_deliverables_uploads_and_returns_ass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, scratch, log_path = self._workspace(Path(tmp))
            uploaded: list[tuple[Path, str]] = []

            def fake_upload(local_path: str | Path, object_key: str) -> bool:
                uploaded.append((Path(local_path), object_key))
                return True

            with (
                patch("videoaudiotext.api.jobs.oss_configured", return_value=True),
                patch("videoaudiotext.api.jobs.upload_file_oss", side_effect=fake_upload),
                patch(
                    "videoaudiotext.api.jobs.oss_public_url",
                    side_effect=lambda key: f"https://bucket.example/{key}",
                ),
            ):
                result = _upload_compose_deliverables(
                    store=store,
                    scratch=scratch,
                    job_id="task-id",
                    code="demo",
                    user_id="user-1",
                    log_path=log_path,
                    task_logger=logging.getLogger("compose-ass-test"),
                )

        uploaded_keys = [key for _, key in uploaded]
        self.assertEqual(len(uploaded_keys), 4)
        self.assertIn(result["output_subtitle_ass_object_key"], uploaded_keys)
        self.assertTrue(
            result["output_subtitle_ass_object_key"].endswith("_subtitle.ass")
        )
        self.assertEqual(
            result["output_subtitle_ass_url"],
            f"https://bucket.example/{result['output_subtitle_ass_object_key']}",
        )

    def test_missing_ass_fails_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, scratch, log_path = self._workspace(Path(tmp))
            (store.subtitles_dir / "active" / "subtitle.ass").unlink()

            with (
                patch("videoaudiotext.api.jobs.oss_configured", return_value=True),
                patch("videoaudiotext.api.jobs.upload_file_oss") as upload,
                self.assertRaisesRegex(ApiError, "subtitle ASS missing"),
            ):
                _upload_compose_deliverables(
                    store=store,
                    scratch=scratch,
                    job_id="task-id",
                    code="demo",
                    user_id="user-1",
                    log_path=log_path,
                    task_logger=logging.getLogger("compose-ass-test"),
                )

            upload.assert_not_called()

    def test_ass_upload_failure_fails_compose_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, scratch, log_path = self._workspace(Path(tmp))

            def fail_ass(_local_path: str | Path, object_key: str) -> bool:
                return not object_key.endswith("_subtitle.ass")

            with (
                patch("videoaudiotext.api.jobs.oss_configured", return_value=True),
                patch("videoaudiotext.api.jobs.upload_file_oss", side_effect=fail_ass),
                self.assertRaisesRegex(ApiError, "upload subtitle_ass to OSS failed"),
            ):
                _upload_compose_deliverables(
                    store=store,
                    scratch=scratch,
                    job_id="task-id",
                    code="demo",
                    user_id="user-1",
                    log_path=log_path,
                    task_logger=logging.getLogger("compose-ass-test"),
                )

    def test_ass_upload_sets_text_content_type(self) -> None:
        class FakeBucket:
            bucket_name = "test-bucket"

            def __init__(self) -> None:
                self.calls: list[tuple[str, str, dict[str, str] | None]] = []

            def put_object_from_file(
                self,
                key: str,
                path: str,
                headers: dict[str, str] | None = None,
            ) -> None:
                self.calls.append((key, path, headers))

        with tempfile.TemporaryDirectory() as tmp:
            subtitle_ass = Path(tmp) / "subtitle.ass"
            subtitle_ass.write_text("[Script Info]\n", encoding="utf-8")
            bucket = FakeBucket()

            with (
                patch("videoaudiotext.storage.oss.oss_configured", return_value=True),
                patch("videoaudiotext.storage.oss._get_oss_bucket", return_value=bucket),
            ):
                uploaded = upload_file_oss(
                    subtitle_ass,
                    "deliverables/task_subtitle.ass",
                )

        self.assertTrue(uploaded)
        self.assertEqual(len(bucket.calls), 1)
        self.assertEqual(
            bucket.calls[0][2],
            {"Content-Type": ASS_CONTENT_TYPE},
        )


if __name__ == "__main__":
    unittest.main()
