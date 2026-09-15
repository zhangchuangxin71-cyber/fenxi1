"""Digest helpers for workspace config validation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence


def md5_hex(blob: str) -> str:
    return hashlib.md5(blob.encode("utf-8"), usedforsecurity=False).hexdigest()


def text_digest_from_segments(segments: Sequence[str]) -> str:
    joined = "\n".join(s.strip() for s in segments)
    return md5_hex(joined)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def audio_preview_digest(
    segments: Sequence[dict[str, Any]],
    gaps_sec: Sequence[float],
    *,
    speed: float = 1.0,
) -> str:
    payload = {
        "segments": segments,
        "gaps_sec": list(gaps_sec),
        "speed": speed,
    }
    return md5_hex(canonical_json(payload))


def media_binding_digest(segments: Sequence[dict[str, Any]]) -> str:
    return md5_hex(canonical_json({"segments": segments}))


def render_style_digest(payload: dict[str, Any]) -> str:
    return md5_hex(canonical_json(payload))
