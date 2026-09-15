"""片段媒体：预览、封面、单文件下载。"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from backend.api.errors import (
    CODE_INTERNAL,
    CODE_SEGMENT_NOT_FOUND,
    CODE_SERVICE_UNAVAILABLE,
    raise_api_error,
)
from backend.storage import oss_client
from backend.storage.database import db
from backend.utils.naming import content_disposition_attachment
from backend.services.job_service import _segment_download_name


def _parse_byte_range(range_header: str | None, size: int) -> tuple[int, int] | None:
    if not range_header or size <= 0:
        return None
    text = range_header.strip()
    if not text.lower().startswith("bytes="):
        return None
    spec = text.split("=", 1)[1].strip()
    if "," in spec:
        spec = spec.split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    start_s, end_s = spec.split("-", 1)
    try:
        if start_s == "":
            length = int(end_s)
            if length <= 0:
                return None
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    end = min(end, size - 1)
    return start, end


def _oss_preview_response(key: str, request: Request, filename: str) -> Response:
    try:
        size = oss_client.head_object_size(key)
    except oss_client.OssError as exc:
        raise_api_error(404, CODE_SEGMENT_NOT_FOUND, str(exc))

    byte_range = _parse_byte_range(request.headers.get("range"), size)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=60",
        "Content-Disposition": f'inline; filename="{filename}"',
    }

    if byte_range is None:
        headers["Content-Length"] = str(size)

        def full_iter():
            yield from oss_client.iter_object_bytes(key)

        return StreamingResponse(
            full_iter(),
            media_type="video/mp4",
            headers=headers,
            status_code=200,
        )

    start, end = byte_range
    length = end - start + 1
    headers["Content-Length"] = str(length)
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    def ranged_iter():
        yield from oss_client.iter_object_bytes(key, start=start, end=end)

    return StreamingResponse(
        ranged_iter(),
        media_type="video/mp4",
        headers=headers,
        status_code=206,
    )


def _oss_redirect(segment: dict) -> RedirectResponse | None:
    key = segment.get("oss_key")
    if not key or not oss_client.is_enabled():
        return None
    try:
        url = oss_client.preview_url(key)
    except oss_client.OssError as exc:
        raise_api_error(500, CODE_INTERNAL, str(exc))
    return RedirectResponse(url=url, status_code=302)


async def preview(segment_id: str, request: Request) -> Response:
    segment = db.get_segment(segment_id)
    if not segment:
        raise_api_error(404, CODE_SEGMENT_NOT_FOUND, "片段不存在")
    path = segment.get("output_path")
    if path and Path(path).exists():
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'inline; filename="{Path(path).name}"',
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, max-age=60",
            },
        )
    key = segment.get("oss_key") or segment.get("staging_oss_key")
    if key and oss_client.is_enabled():
        return RedirectResponse(url=oss_client.preview_url(key), status_code=302)
    raise_api_error(404, CODE_SEGMENT_NOT_FOUND, "片段文件不存在")


async def thumb(segment_id: str) -> Response:
    from backend.config import OUTPUT_DIR
    from backend.core.cutter import FFmpegError, extract_poster, segment_poster_path

    segment = db.get_segment(segment_id)
    if not segment:
        raise_api_error(
            404,
            CODE_SEGMENT_NOT_FOUND,
            "片段不存在",
        )

    path = segment.get("output_path")
    cache_poster = OUTPUT_DIR / "_thumbs" / f"{segment_id}.jpg"
    cache_poster.parent.mkdir(parents=True, exist_ok=True)
    if cache_poster.exists() and cache_poster.stat().st_size >= 32:
        return FileResponse(
            cache_poster,
            media_type="image/jpeg",
            headers={
                "Content-Disposition": f'inline; filename="{cache_poster.name}"',
                "Cache-Control": "public, max-age=3600",
            },
        )

    if path and Path(path).exists():
        video = Path(path)
        poster = segment_poster_path(video)
        if not poster.exists() or poster.stat().st_size < 32:
            try:
                poster = await asyncio.to_thread(extract_poster, video, poster)
            except FFmpegError as exc:
                raise_api_error(500, CODE_INTERNAL, str(exc))
        if poster != cache_poster:
            shutil.copy2(poster, cache_poster)
        return FileResponse(
            cache_poster if cache_poster.exists() else poster,
            media_type="image/jpeg",
            headers={
                "Content-Disposition": f'inline; filename="{segment_id}.jpg"',
                "Cache-Control": "public, max-age=3600",
            },
        )

    media_key = segment.get("oss_key") or segment.get("staging_oss_key")
    if media_key and oss_client.is_enabled():
        poster_key = segment.get("thumb_oss_key") or str(Path(media_key).with_suffix(".jpg"))
        try:
            if oss_client.object_exists(poster_key):
                return RedirectResponse(
                    url=oss_client.preview_url(poster_key),
                    status_code=302,
                )
        except oss_client.OssError:
            pass

        raise_api_error(
            503,
            CODE_SERVICE_UNAVAILABLE,
            "片段封面未就绪；服务不会为封面重新下载完整 OSS 视频",
        )

    raise_api_error(503, CODE_SERVICE_UNAVAILABLE, "片段文件未就绪")


async def download(segment_id: str) -> Response:
    """切割完成且有 OSS 对象后可下载。"""
    from backend.api.errors import CODE_JOB_NOT_READY

    segment = db.get_segment(segment_id)
    if not segment:
        raise_api_error(404, CODE_SEGMENT_NOT_FOUND, "片段不存在")
    if not segment.get("oss_key"):
        raise_api_error(
            400,
            CODE_JOB_NOT_READY,
            "片段尚未生成 OSS 对象；请先等待切割任务完成",
        )
    download_name = _segment_download_name(segment)
    redirect = _oss_redirect(segment)
    if redirect is not None:
        return redirect
    # 发布后本地仍可能残留：允许直出，文件名与 download_filename 一致
    path = segment.get("output_path")
    if path and Path(path).exists():
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={
                "Content-Disposition": content_disposition_attachment(download_name),
            },
        )
    raise_api_error(404, CODE_SEGMENT_NOT_FOUND, "片段文件不存在")
