"""视频业务：异步导入、查询、媒体、删除。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from backend.api.errors import (
    CODE_CONFLICT,
    CODE_IMPORT_FAILED,
    CODE_INTERNAL,
    CODE_OSS_DISABLED,
    CODE_QUEUE_UNAVAILABLE,
    CODE_VIDEO_NOT_FOUND,
    raise_api_error,
)
from backend.api.v1.schemas import ImportSubmitted, Video, VideoStatus, WaveformData
from backend.api.v1.urls import video_filmstrip_url, video_preview_url
from backend.core.cutter import FFmpegError, media_has_audio
from backend.core.filmstrip import (
    FRAME_H,
    FRAME_W,
    filmstrip_path,
    generate_filmstrip,
    suggest_filmstrip_count,
)
from backend.core.waveform import load_or_build_waveform
from backend.config import OSS_STAGING_PREFIX
from backend.storage import oss_client
from backend.storage.database import CapacityError, db
from backend.storage.file_manager import (
    delete_task_outputs,
    delete_video_file,
    generate_id,
    is_allowed_extension,
)
from backend.storage.http_download import HttpDownloadError, filename_from_url
from backend.storage.workspace import (
    ensure_source_local,
    resolve_import_http_url,
    resolve_import_oss_key,
    source_media_ref,
)
from backend.workers.enqueue import QueueUnavailableError, enqueue_import_video

logger = logging.getLogger(__name__)


def require_local_source(video_id: str) -> Path:
    path = ensure_source_local(video_id)
    if not path or not path.exists():
        raise_api_error(404, CODE_VIDEO_NOT_FOUND, "视频文件不存在")
    return path


async def require_local_source_async(video_id: str) -> Path:
    path = await asyncio.to_thread(ensure_source_local, video_id)
    if not path or not path.exists():
        raise_api_error(404, CODE_VIDEO_NOT_FOUND, "视频文件不存在")
    return path


def require_ready(video_id: str) -> dict:
    """建 Job / 拉媒体前：视频须存在且 status=ready。"""
    video = db.get_video(video_id)
    if not video:
        raise_api_error(404, CODE_VIDEO_NOT_FOUND, "视频不存在")
    status = (video.get("status") or VideoStatus.READY.value).lower()
    if status == VideoStatus.IMPORTING.value:
        raise_api_error(
            409,
            CODE_CONFLICT,
            "视频仍在导入中，请轮询 GET /videos/{video_id} 至 status=ready",
        )
    if status == VideoStatus.FAILED.value:
        raise_api_error(
            400,
            CODE_IMPORT_FAILED,
            video.get("message") or "视频导入失败，请重新 POST /videos/import",
        )
    if status != VideoStatus.READY.value:
        raise_api_error(409, CODE_CONFLICT, f"视频状态不可用: {status}")
    return video


def _parse_status(raw: str | None) -> VideoStatus:
    try:
        return VideoStatus((raw or VideoStatus.READY.value).lower())
    except ValueError:
        return VideoStatus.READY


async def submit_import(
    *,
    oss_key: str | None,
    oss_url: str | None,
) -> ImportSubmitted:
    """校验引用、写占位行、入队；立刻返回。url 走 HTTP；oss_key 走配置桶。"""
    if not oss_client.is_enabled():
        raise_api_error(
            503,
            CODE_OSS_DISABLED,
            "OSS-native 模式需要配置 OSS_ACCESS_KEY_ID / "
            "OSS_ACCESS_KEY_SECRET / OSS_ENDPOINT / OSS_BUCKET",
        )
    video_id = generate_id()
    object_key: str | None = None
    source_url: str | None = None
    source = "oss"

    if oss_url:
        try:
            source_url = resolve_import_http_url(oss_url)
        except HttpDownloadError as exc:
            raise_api_error(400, CODE_IMPORT_FAILED, str(exc))
        source = "http"
        filename = filename_from_url(source_url) or f"{video_id}.mp4"
    else:
        if not oss_client.is_enabled():
            raise_api_error(
                503,
                CODE_OSS_DISABLED,
                "OSS 未启用：请配置 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / "
                "OSS_ENDPOINT / OSS_BUCKET",
            )
        try:
            object_key = resolve_import_oss_key(oss_key=oss_key, oss_url=None)
        except oss_client.OssError as exc:
            raise_api_error(400, CODE_IMPORT_FAILED, str(exc))
        filename = Path(object_key).name or f"{video_id}.mp4"

    if not is_allowed_extension(filename):
        filename = f"{video_id}.mp4"

    try:
        db.create_video_importing(
            video_id,
            filename=filename,
            oss_key=object_key,
            source_url=source_url,
            source=source,
            message="已入队，等待后台导入...",
        )
    except CapacityError as exc:
        raise_api_error(503, CODE_QUEUE_UNAVAILABLE, str(exc))
    try:
        enqueue_import_video(video_id)
    except QueueUnavailableError as exc:
        db.update_video_import(
            video_id,
            status=VideoStatus.FAILED.value,
            progress=0,
            message=str(exc),
        )
        raise_api_error(503, CODE_QUEUE_UNAVAILABLE, str(exc))

    row = db.get_video(video_id)
    return ImportSubmitted(
        video_id=video_id,
        status=_parse_status(row.get("status") if row else None),
        oss_key=object_key,
        source_url=source_url,
    )


def run_import(video_id: str) -> None:
    """兼容入口；实际逻辑在 workers.import_runner。"""
    from backend.workers.import_runner import run_import as _run

    _run(video_id)


async def _build_waveform(video_id: str, duration: float) -> WaveformData | None:
    path = await asyncio.to_thread(source_media_ref, video_id)
    if path is None:
        return None
    try:
        data = await asyncio.to_thread(
            load_or_build_waveform,
            video_id,
            path,
            bins=64,
            duration=duration,
        )
    except FFmpegError:
        logger.exception("waveform build failed video_id=%s", video_id)
        return None
    except Exception:
        logger.exception("waveform build unexpected error video_id=%s", video_id)
        return None
    return WaveformData(
        bins=int(data["bins"]),
        peaks=list(data["peaks"]),
        has_audio=bool(data["has_audio"]),
    )


async def build_video(video_id: str, *, base: str = "") -> Video:
    video = db.get_video(video_id)
    if not video:
        raise_api_error(404, CODE_VIDEO_NOT_FOUND, "视频不存在")

    status = _parse_status(video.get("status"))
    progress = float(video.get("progress") if video.get("progress") is not None else 0)
    message = video.get("message") or ""
    filename = video.get("filename") or ""
    oss_key = video.get("oss_key")
    source_url = video.get("source_url")

    if status != VideoStatus.READY:
        return Video(
            video_id=video["id"],
            status=status,
            progress=progress,
            message=message,
            filename=filename,
            duration=float(video["duration"]) if video.get("duration") is not None else None,
            width=int(video["width"]) if video.get("width") is not None else None,
            height=int(video["height"]) if video.get("height") is not None else None,
            fps=float(video["fps"]) if video.get("fps") is not None else None,
            codec=video.get("codec"),
            size_bytes=int(video["size_bytes"]) if video.get("size_bytes") is not None else None,
            preview_url=None,
            filmstrip_url=None,
            waveform=None,
            has_audio=False,
            oss_key=oss_key,
            source_url=source_url,
        )

    path = await asyncio.to_thread(source_media_ref, video_id)
    has_audio = bool(video.get("has_audio", False))
    if path and "has_audio" not in video:
        has_audio = await asyncio.to_thread(media_has_audio, path)
    duration = float(video["duration"] or 0)
    waveform = await _build_waveform(video_id, duration)
    return Video(
        video_id=video["id"],
        status=status,
        progress=progress if progress else 100.0,
        message=message or "导入完成",
        filename=filename,
        duration=duration,
        width=int(video["width"] or 0),
        height=int(video["height"] or 0),
        fps=float(video["fps"] or 0),
        codec=video.get("codec") or "",
        size_bytes=int(video["size_bytes"] or 0),
        preview_url=video_preview_url(
            video_id,
            base=base,
            oss_key=oss_key,
            source_url=source_url,
        ),
        filmstrip_url=video_filmstrip_url(video_id, base=base),
        waveform=waveform,
        has_audio=waveform.has_audio if waveform is not None else has_audio,
        oss_key=oss_key,
        source_url=source_url,
    )


async def stream_media(video_id: str) -> Response:
    require_ready(video_id)
    video = db.get_video(video_id)
    if video and video.get("oss_key") and oss_client.is_enabled():
        return RedirectResponse(
            url=oss_client.preview_url(video["oss_key"]), status_code=302
        )
    path = await require_local_source_async(video_id)
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


async def get_filmstrip_jpeg(video_id: str) -> Response:
    require_ready(video_id)
    video = db.get_video(video_id)
    if not video:
        raise_api_error(404, CODE_VIDEO_NOT_FOUND, "视频不存在")
    duration = float(video["duration"] or 1)
    resolved_count = suggest_filmstrip_count(duration)
    existing_key = video.get("filmstrip_oss_key")
    if existing_key and oss_client.is_enabled() and oss_client.object_exists(existing_key):
        size = oss_client.head_object_size(existing_key)
        return StreamingResponse(
            oss_client.iter_object_bytes(existing_key),
            media_type="image/jpeg",
            headers={
                "Content-Length": str(size),
                "Cache-Control": "public, max-age=3600",
                "X-Filmstrip-Count": str(resolved_count),
            },
        )
    path = await asyncio.to_thread(source_media_ref, video_id)
    if not path:
        raise_api_error(404, CODE_VIDEO_NOT_FOUND, "源片不可访问")
    out = filmstrip_path(video_id, resolved_count, start=0.0, end=None, duration=duration)
    if not out.exists():
        try:
            await asyncio.to_thread(
                generate_filmstrip,
                path,
                out,
                duration=duration,
                count=resolved_count,
                start=0.0,
                end=None,
            )
        except FFmpegError as exc:
            raise_api_error(500, CODE_INTERNAL, str(exc))
    if oss_client.is_enabled():
        from backend.config import OSS_DERIVED_PREFIX

        key = f"{OSS_DERIVED_PREFIX}videos/{video_id}/{out.name}"
        if not oss_client.object_exists(key):
            await asyncio.to_thread(oss_client.upload_file, out, key)
        db.update_video_artifacts(video_id, filmstrip_oss_key=key)
        out.unlink(missing_ok=True)
        size = oss_client.head_object_size(key)
        return StreamingResponse(
            oss_client.iter_object_bytes(key),
            media_type="image/jpeg",
            headers={
                "Content-Length": str(size),
                "Cache-Control": "public, max-age=3600",
                "X-Filmstrip-Count": str(resolved_count),
            },
        )
    return FileResponse(out, media_type="image/jpeg")


async def delete_video(video_id: str) -> None:
    video = db.get_video(video_id)
    if not video:
        raise_api_error(404, CODE_VIDEO_NOT_FOUND, "视频不存在")
    if oss_client.is_enabled():
        retained_final_keys: list[str] = []
        for task in db.get_tasks_by_video(video_id):
            for seg in db.get_segments(task["id"]):
                if seg.get("oss_key"):
                    retained_final_keys.append(str(seg["oss_key"]))
                staging_key = seg.get("staging_oss_key")
                staging_thumb = seg.get("thumb_oss_key") if staging_key else None
                for seg_key in (staging_key, staging_thumb):
                    if not str(seg_key or "").startswith(OSS_STAGING_PREFIX):
                        continue
                    if not seg_key:
                        continue
                    try:
                        oss_client.delete_object(seg_key)
                    except Exception:
                        pass
            delete_task_outputs(task["id"])
        for key in (video.get("filmstrip_oss_key"),):
            if key:
                oss_client.delete_object(key)
        if video.get("managed_source") and video.get("oss_key"):
            oss_client.delete_object(video["oss_key"])
        if retained_final_keys:
            logger.warning(
                "video deleted while retaining published OSS objects video_id=%s count=%s prefixes=%s",
                video_id,
                len(retained_final_keys),
                sorted({str(Path(key).parent) for key in retained_final_keys}),
            )
    else:
        for task in db.get_tasks_by_video(video_id):
            delete_task_outputs(task["id"])
    db.delete_video(video_id)
    delete_video_file(video_id)
