"""Visual-query helpers for LLM split (not CLIP retrieval)."""

from __future__ import annotations

import os

VIDEO_CONTEXT_TOPIC = os.environ.get("VIDEO_CONTEXT_TOPIC", "通用").strip()

COMMA_MULTI_MATERIAL = os.environ.get("COMMA_MULTI_MATERIAL", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}


def video_context_topic() -> str:
    """纯情绪句兜底主题（供 retrieval.queries 生成 fallback query）。"""
    return VIDEO_CONTEXT_TOPIC or "通用"


def comma_multi_material_enabled() -> bool:
    """逗号多素材切分；LLM 语义分句模式下默认关闭。"""
    if not COMMA_MULTI_MATERIAL:
        return False
    from videoaudiotext.config.subtitle import subtitle_match_pipeline_segments

    return not subtitle_match_pipeline_segments()
