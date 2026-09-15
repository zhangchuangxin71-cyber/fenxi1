"""Helpers to serialize standalone API request bodies for job bootstrap."""

from __future__ import annotations

from typing import Any

from videoaudiotext.api.schemas import ComposeRunRequest


def standalone_compose_payload(body: ComposeRunRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "master_audio_url": body.master_audio_url,
        "subtitle_srt_url": body.subtitle_srt_url,
        "speed": body.speed,
        "segments": [
            {
                "index": s.index,
                "text": s.text,
                **(
                    {"duration_sec": s.duration_sec}
                    if s.duration_sec is not None
                    else {}
                ),
                **(
                    {"clip_duration_sec": s.clip_duration_sec}
                    if s.clip_duration_sec is not None
                    else {}
                ),
                "media": s.media.model_dump(),
            }
            for s in body.segments
        ],
    }
    if body.subtitle_ass_url:
        payload["subtitle_ass_url"] = body.subtitle_ass_url
    payload["voice_volume"] = body.voice_volume
    if body.segment_urls:
        payload["segment_urls"] = body.segment_urls
    if body.resolution:
        payload["resolution"] = body.resolution.model_dump(exclude_none=True)
    if body.subtitle_style:
        payload["subtitle_style"] = body.subtitle_style.model_dump()
    if body.bgm:
        payload["bgm"] = body.bgm.model_dump()
    return payload
