from __future__ import annotations

from typing import Literal

ImageContentType = Literal[
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/bmp",
]

VideoContentType = Literal[
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
    "video/x-matroska",
]

TextContentType = Literal["text/plain"]

_CONTENT_TYPE_SUFFIX: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
}


def suffix_for_content_type(content_type: str) -> str:
    """Map MIME type to a safe temp-file suffix for decoders."""
    normalized = content_type.strip().lower()
    if normalized in _CONTENT_TYPE_SUFFIX:
        return _CONTENT_TYPE_SUFFIX[normalized]
    if "/" in normalized:
        subtype = normalized.split("/", 1)[1]
        return f".{subtype.replace('x-', '').replace('.', '_')}"
    return ".bin"
