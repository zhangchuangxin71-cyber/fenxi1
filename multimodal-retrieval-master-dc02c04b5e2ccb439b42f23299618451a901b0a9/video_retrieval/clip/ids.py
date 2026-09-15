from __future__ import annotations

VIDEO_CLIP_PK_PREFIX = "video_clip:video:"


def media_id_from_pk(pk: str) -> str:
    if pk.startswith(VIDEO_CLIP_PK_PREFIX):
        return pk[len(VIDEO_CLIP_PK_PREFIX) :]
    return pk


def video_clip_pk(media_id: str) -> str:
    return f"{VIDEO_CLIP_PK_PREFIX}{media_id}"
