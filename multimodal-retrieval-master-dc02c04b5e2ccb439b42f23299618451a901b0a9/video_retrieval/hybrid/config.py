"""Hybrid retrieval default paths (from video_retrieval.profile_paths)."""

from pathlib import Path

from ..config import DEFAULT_VIDEO_DIR, PACKAGE_ROOT, WORKSPACE_ROOT
from ..profile_paths import (
    default_hybrid_captions_jsonl,
    default_hybrid_index_dir,
    default_hybrid_metadata_db_path,
)

VIDEO_RETRIEVAL_ROOT = PACKAGE_ROOT
PROJECT_ROOT = PACKAGE_ROOT

DEFAULT_CAPTIONS_JSONL = default_hybrid_captions_jsonl(None)
DEFAULT_METADATA_DB = default_hybrid_metadata_db_path(None)
DEFAULT_INDEX_DIR = default_hybrid_index_dir(None)
HYBRID_ROOT = Path(__file__).resolve().parent

__all__ = [
    "DEFAULT_CAPTIONS_JSONL",
    "DEFAULT_HYBRID_CAPTIONS_JSONL",
    "DEFAULT_HYBRID_INDEX_DIR",
    "DEFAULT_HYBRID_METADATA_DB",
    "DEFAULT_INDEX_DIR",
    "DEFAULT_METADATA_DB",
    "DEFAULT_VIDEO_DIR",
    "HYBRID_ROOT",
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
    "VIDEO_RETRIEVAL_ROOT",
    "WORKSPACE_ROOT",
    "default_hybrid_captions_jsonl",
    "default_hybrid_index_dir",
    "default_hybrid_metadata_db_path",
]

DEFAULT_HYBRID_CAPTIONS_JSONL = DEFAULT_CAPTIONS_JSONL
DEFAULT_HYBRID_METADATA_DB = DEFAULT_METADATA_DB
DEFAULT_HYBRID_INDEX_DIR = DEFAULT_INDEX_DIR
