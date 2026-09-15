"""Locked segment plan — preview 与成片共用同一分句结果。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import List, Sequence

from videoaudiotext.config import SOURCE_MEDIA_DIR
from videoaudiotext.text.segment_meta import SegmentVisualMeta

PIPELINE_PLAN_JSON = SOURCE_MEDIA_DIR / "pipeline_plan.json"


def pipeline_plan_lock_enabled() -> bool:
    return os.environ.get("PIPELINE_PLAN_LOCK", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _text_digest(text: str) -> str:
    return hashlib.md5((text or "").strip().encode(), usedforsecurity=False).hexdigest()


def _voice_digest(voice: str, rate: str) -> str:
    payload = f"{voice.strip()}|{rate.strip()}"
    return hashlib.md5(payload.encode(), usedforsecurity=False).hexdigest()


def _meta_to_json(meta: SegmentVisualMeta | None) -> dict | None:
    if meta is None:
        return None
    return {
        "visual": bool(meta.visual),
        "search_query": meta.search_query,
    }


def _meta_from_json(raw: dict | None) -> SegmentVisualMeta | None:
    if not raw:
        return None
    return SegmentVisualMeta(
        visual=bool(raw.get("visual")),
        search_query=raw.get("search_query"),
    )


def save_pipeline_plan(
    text: str,
    segments: List[str],
    *,
    groups: List[int] | None = None,
    visual_meta: Sequence[SegmentVisualMeta | None] | None = None,
    voice: str = "",
    rate: str = "+0%",
    path: Path | None = None,
) -> Path:
    dest = path or PIPELINE_PLAN_JSON
    dest.parent.mkdir(parents=True, exist_ok=True)
    segs = [str(s).strip() for s in segments if str(s).strip()]
    grp = list(groups) if groups is not None else list(range(len(segs)))
    meta_list: list[dict | None] = []
    if visual_meta is not None:
        meta_list = [_meta_to_json(m) for m in visual_meta]
    payload = {
        "text_digest": _text_digest(text),
        "voice_digest": _voice_digest(voice, rate),
        "segments": segs,
        "groups": grp,
        "visual_meta": meta_list,
    }
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dest


def load_pipeline_plan(
    text: str,
    *,
    voice: str = "",
    rate: str = "+0%",
    path: Path | None = None,
) -> tuple[List[str], List[int], List[SegmentVisualMeta | None] | None] | None:
    dest = path or PIPELINE_PLAN_JSON
    if not dest.is_file():
        return None
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if data.get("text_digest") != _text_digest(text):
        return None
    if data.get("voice_digest") != _voice_digest(voice, rate):
        return None
    segments = [str(s) for s in data.get("segments") or [] if str(s).strip()]
    if not segments:
        return None
    groups_raw = data.get("groups") or list(range(len(segments)))
    groups = [int(g) for g in groups_raw]
    if len(groups) != len(segments):
        groups = list(range(len(segments)))
    meta_raw = data.get("visual_meta")
    visual_meta: List[SegmentVisualMeta | None] | None = None
    if isinstance(meta_raw, list) and len(meta_raw) == len(segments):
        visual_meta = [_meta_from_json(m if isinstance(m, dict) else None) for m in meta_raw]
    return segments, groups, visual_meta


def clear_pipeline_plan(path: Path | None = None) -> None:
    dest = path or PIPELINE_PLAN_JSON
    dest.unlink(missing_ok=True)
