"""绝对 URL 拼装（媒体 / action links）。"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import Request

from backend.config import API_PUBLIC_BASE, OSS_CDN_BASE


def public_base_url(request: Request | None = None) -> str:
    if API_PUBLIC_BASE:
        return API_PUBLIC_BASE
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


def absolutize(path: str | None, base: str) -> str | None:
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    root = (base or "").rstrip("/")
    if not root:
        return path if path.startswith("/") else f"/{path}"
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{root}{path}"


def oss_cdn_url(oss_key: str | None) -> str | None:
    """将 oss_key 拼成 CDN 公网 URL；未配置 CDN 或无 key 时返回 None。"""
    key = (oss_key or "").strip().lstrip("/")
    base = (OSS_CDN_BASE or "").rstrip("/")
    if not key or not base:
        return None
    encoded = "/".join(quote(seg, safe="") for seg in key.split("/") if seg != "")
    if not encoded:
        return None
    return f"{base}/{encoded}"


def video_media_url(video_id: str, *, base: str = "") -> str:
    return absolutize(f"/api/v1/videos/{video_id}/media", base) or ""


def video_preview_url(
    video_id: str,
    *,
    base: str = "",
    oss_key: str | None = None,
    source_url: str | None = None,
) -> str:
    """源片预览地址。

    - 导入传 url：直接返回该 url（source_url）
    - 导入传 oss_key：拼 CDN → {OSS_CDN_BASE}/{oss_key}
    - 否则回退本机 /media
    """
    if oss_key:
        return video_media_url(video_id, base=base)
    url = (source_url or "").strip()
    if url.startswith(("http://", "https://")):
        return url
    return video_media_url(video_id, base=base)


def video_filmstrip_url(video_id: str, *, base: str = "") -> str:
    return absolutize(f"/api/v1/videos/{video_id}/filmstrip", base) or ""


def segment_media_url(segment_id: str, kind: str, *, base: str = "") -> str | None:
    return absolutize(f"/api/v1/segments/{segment_id}/{kind}", base)


def segment_preview_url(
    segment_id: str,
    *,
    base: str = "",
    oss_key: str | None = None,
) -> str | None:
    """片段预览：已发布且有 CDN 时走 CDN，否则本机 /preview。"""
    return segment_media_url(segment_id, "preview", base=base)
