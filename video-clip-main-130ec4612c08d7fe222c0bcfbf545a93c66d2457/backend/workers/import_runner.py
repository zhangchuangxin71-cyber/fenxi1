"""Celery 侧源片导入执行逻辑（避免经 services 包触发循环导入）。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from backend.core.cutter import FFmpegError, get_video_info
from backend.core.waveform import load_or_build_waveform
from backend.storage.database import db
from backend.storage.file_manager import is_allowed_extension
from backend.config import (
    OSS_DOWNLOAD_RETRIES,
    OSS_DOWNLOAD_RETRY_BASE_SLEEP,
    OSS_INGEST_PREFIX,
    OSS_PROCESS_SIGN_EXPIRES,
)
from backend.storage import oss_client
from backend.storage.http_download import filename_from_url, iter_http_bytes
from backend.storage.workspace import (
    cleanup_video_local,
)

logger = logging.getLogger(__name__)

STATUS_IMPORTING = "importing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


def run_import(video_id: str) -> None:
    """OSS HEAD/URL 流式转存 → 远程 probe/波形 → ready。"""
    video = db.get_video(video_id)
    if not video:
        logger.error("import task missing video_id=%s", video_id)
        return
    status = (video.get("status") or "").lower()
    if status == STATUS_READY:
        logger.info("idempotent skip import already ready video_id=%s", video_id)
        return
    if status == STATUS_FAILED:
        # 允许失败后重投递重试：继续执行
        pass

    source_url = (video.get("source_url") or "").strip() or None
    oss_key = (video.get("oss_key") or "").strip() or None
    if not source_url and not oss_key:
        db.update_video_import(
            video_id,
            status=STATUS_FAILED,
            progress=0,
            message="缺少 source_url / oss_key，无法导入",
        )
        return

    try:
        managed_source = False
        if source_url:
            db.update_video_import(
                video_id,
                status=STATUS_IMPORTING,
                progress=5,
                message="正在将公网源片流式转存到 OSS...",
            )
            filename = filename_from_url(source_url) or "source.mp4"
            suffix = Path(filename).suffix.lower()
            if not is_allowed_extension(filename):
                suffix = ".mp4"
                filename = f"source{suffix}"
            oss_key = f"{OSS_INGEST_PREFIX}{video_id}/source{suffix}"
            attempts = max(1, OSS_DOWNLOAD_RETRIES + 1)
            for attempt in range(attempts):
                try:
                    oss_client.multipart_upload_chunks(
                        oss_key,
                        iter_http_bytes(source_url),
                        content_type="video/mp4",
                    )
                    break
                except Exception:
                    if attempt >= attempts - 1:
                        raise
                    time.sleep(OSS_DOWNLOAD_RETRY_BASE_SLEEP * (2**attempt))
            managed_source = True
        else:
            assert oss_key is not None
            db.update_video_import(
                video_id,
                status=STATUS_IMPORTING,
                progress=5,
                message="正在校验 OSS 源片...",
            )
            oss_client.head_object_size(oss_key)
            filename = Path(oss_key).name or "source.mp4"

        if not is_allowed_extension(filename):
            filename = f"source{Path(oss_key or '').suffix or '.mp4'}"

        assert oss_key is not None
        media_ref = oss_client.sign_url(
            oss_key, expires=OSS_PROCESS_SIGN_EXPIRES, prefer_cdn=False
        )

        db.update_video_import(
            video_id,
            progress=55,
            message="正在解析视频元信息...",
            path="",
            filename=filename,
            oss_key=oss_key,
            managed_source=managed_source,
        )
        try:
            info = get_video_info(media_ref)
            info["size_bytes"] = oss_client.head_object_size(oss_key)
        except FFmpegError as exc:
            raise RuntimeError(f"无法解析视频: {exc}") from exc

        db.update_video_import(
            video_id,
            progress=75,
            message="正在生成波形...",
            path="",
            info=info,
            filename=filename,
        )
        try:
            load_or_build_waveform(
                video_id,
                media_ref,
                bins=64,
                duration=float(info.get("duration") or 0),
            )
        except Exception:
            logger.exception("waveform build failed video_id=%s", video_id)

        db.update_video_import(
            video_id,
            status=STATUS_READY,
            progress=100,
            message="导入完成",
            path="",
            info=info,
            filename=filename,
        )
    except Exception as exc:
        logger.exception("import failed video_id=%s", video_id)
        cleanup_video_local(video_id)
        current = db.get_video(video_id)
        if current and current.get("managed_source") and current.get("oss_key"):
            try:
                oss_client.delete_object(current["oss_key"])
            except Exception:
                logger.warning("failed to remove managed import source", exc_info=True)
        db.update_video_import(
            video_id,
            status=STATUS_FAILED,
            progress=0,
            message=str(exc) or "导入失败",
        )
