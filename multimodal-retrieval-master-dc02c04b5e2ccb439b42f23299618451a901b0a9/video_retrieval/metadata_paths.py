"""Shared SQLite path normalization for CLIP and hybrid metadata stores."""

from __future__ import annotations

from .portable_paths import portable_path_text, resolved_path_text

CLIP_PATH_FIELDS = frozenset(
    {
        "path",
        "video_path",
        "frame_path",
        "embedding_path",
        "embedding_metadata_path",
    }
)

HYBRID_PATH_FIELDS = frozenset({"path", "video_path"})


def normalize_record_paths(record: dict, path_fields: frozenset[str]) -> dict:
    for key in path_fields:
        value = record.get(key)
        if value is None:
            continue
        record[key] = resolved_path_text(value)
    return record


def portable_path_value(value: str | None) -> str | None:
    return portable_path_text(value)
