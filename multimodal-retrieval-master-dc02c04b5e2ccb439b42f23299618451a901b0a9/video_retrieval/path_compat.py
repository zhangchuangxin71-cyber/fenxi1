"""Rewrite legacy repo paths stored in metadata (project/, doubao_pipeline/, workspace/)."""

from __future__ import annotations

from .config import DEFAULT_CLIP_PROFILE, DEFAULT_HYBRID_PROFILE

# Suffix segments after repo root (chinese_clip): old -> new
LEGACY_SUFFIX_REWRITES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("project", "profiles"), ("video_retrieval", "legacy", "clip_profiles")),
    (
        ("project", "output"),
        ("video_retrieval", "legacy", "clip_profiles", DEFAULT_CLIP_PROFILE, "output"),
    ),
    (
        ("project", "metadata"),
        ("video_retrieval", "legacy", "clip_profiles", DEFAULT_CLIP_PROFILE),
    ),
    (
        ("video_retrieval", "workspace", "output"),
        ("video_retrieval", "legacy", "clip_profiles", DEFAULT_CLIP_PROFILE, "output"),
    ),
    (
        ("video_retrieval", "workspace", "metadata"),
        ("video_retrieval", "legacy", "clip_profiles", DEFAULT_CLIP_PROFILE),
    ),
    (("project", "models"), ("video_retrieval", "models")),
    (("project", "videos"), ("video_retrieval", "videos")),
    (("doubao_pipeline", "profiles"), ("video_retrieval", "legacy", "hybrid_profiles")),
    (
        ("doubao_pipeline", "artifacts"),
        ("video_retrieval", "legacy", "hybrid_profiles", DEFAULT_HYBRID_PROFILE),
    ),
    (
        ("video_retrieval", "artifacts"),
        ("video_retrieval", "legacy", "hybrid_profiles", DEFAULT_HYBRID_PROFILE),
    ),
    (("doubao_pipeline", "output"), ("video_retrieval", "hybrid", "output")),
)


def rewrite_legacy_suffix(parts: tuple[str, ...]) -> tuple[str, ...]:
    lowered = tuple(part.lower() for part in parts)
    for old_prefix, new_prefix in LEGACY_SUFFIX_REWRITES:
        old_len = len(old_prefix)
        if len(parts) >= old_len and lowered[:old_len] == old_prefix:
            return new_prefix + parts[old_len:]
    return parts
