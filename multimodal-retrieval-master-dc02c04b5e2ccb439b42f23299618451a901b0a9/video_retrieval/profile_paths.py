"""Resolve profile directory layout for CLIP and hybrid indexes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import (
    DEFAULT_CLIP_PROFILE,
    DEFAULT_HYBRID_PROFILE,
    DEFAULT_PROFILES_DIR,
    LEGACY_CLIP_PROFILES_ROOT,
    LEGACY_HYBRID_PROFILES_ROOT,
)


def normalize_profile_name(profile: str | None) -> str | None:
    if profile is None:
        return None
    text = profile.strip()
    return text or None


def unified_profile_root(profile: str) -> Path:
    return DEFAULT_PROFILES_DIR / profile


def legacy_clip_profile_root(profile: str) -> Path:
    return LEGACY_CLIP_PROFILES_ROOT / profile


def legacy_hybrid_profile_root(profile: str) -> Path:
    return LEGACY_HYBRID_PROFILES_ROOT / profile


def _pick_path(preferred: Path, legacy: Path, *, file_name: str | None = None) -> Path:
    """Prefer unified layout; fall back to legacy if unified artifact missing."""
    if file_name:
        unified_file = preferred / file_name
        legacy_file = legacy / file_name
        if unified_file.exists():
            return preferred
        if legacy_file.exists():
            return legacy
    if preferred.exists() or not legacy.exists():
        return preferred
    return legacy


@dataclass(frozen=True)
class ProfileLayout:
    """Resolved storage paths for one logical profile (CLIP + hybrid may use different names)."""

    profile: str
    clip_profile: str
    hybrid_profile: str
    unified: bool

    clip_root: Path
    clip_output_dir: Path
    clip_metadata_db: Path
    clip_logs_dir: Path

    hybrid_root: Path
    hybrid_metadata_db: Path
    hybrid_index_dir: Path
    hybrid_captions_jsonl: Path
    hybrid_logs_dir: Path
    hybrid_output_dir: Path


@dataclass(frozen=True)
class SearchSource:
    name: str
    metadata_db_path: Path
    index_dir: Path


def resolve_profile_layout(
    profile: str | None = None,
    *,
    clip_profile: str | None = None,
    hybrid_profile: str | None = None,
    clip_output_dir: Path | None = None,
    clip_metadata_db: Path | None = None,
    hybrid_metadata_db: Path | None = None,
    hybrid_index_dir: Path | None = None,
    hybrid_captions_jsonl: Path | None = None,
) -> ProfileLayout:
    base = (
        normalize_profile_name(profile)
        or normalize_profile_name(clip_profile)
        or normalize_profile_name(hybrid_profile)
    )
    if not base and not (clip_output_dir or clip_metadata_db or hybrid_metadata_db):
        raise ValueError("profile name or explicit paths are required")

    clip_name = normalize_profile_name(clip_profile) or base or DEFAULT_CLIP_PROFILE
    hybrid_name = normalize_profile_name(hybrid_profile) or base or DEFAULT_HYBRID_PROFILE

    unified_clip = unified_profile_root(clip_name) / "clip"
    legacy_clip = legacy_clip_profile_root(clip_name)
    clip_root = _pick_path(unified_clip, legacy_clip)
    unified_clip_layout = clip_root == unified_clip

    unified_hybrid = unified_profile_root(hybrid_name) / "hybrid"
    legacy_hybrid = legacy_hybrid_profile_root(hybrid_name)
    hybrid_root = _pick_path(unified_hybrid, legacy_hybrid)
    unified_hybrid_layout = hybrid_root == unified_hybrid

    if clip_output_dir is not None:
        out = Path(clip_output_dir)
    elif clip_root in (unified_clip, legacy_clip):
        out = clip_root / "output"
    else:
        out = resolve_profile_layout(clip_profile=DEFAULT_CLIP_PROFILE).clip_output_dir

    if clip_metadata_db is not None:
        clip_db = Path(clip_metadata_db)
    elif clip_root in (unified_clip, legacy_clip):
        clip_db = clip_root / "metadata.db"
    else:
        clip_db = resolve_profile_layout(clip_profile=DEFAULT_CLIP_PROFILE).clip_metadata_db

    if hybrid_metadata_db is not None:
        hybrid_db = Path(hybrid_metadata_db)
    elif hybrid_root in (unified_hybrid, legacy_hybrid):
        hybrid_db = hybrid_root / "metadata.db"
    else:
        hybrid_db = resolve_profile_layout(hybrid_profile=DEFAULT_HYBRID_PROFILE).hybrid_metadata_db

    if hybrid_index_dir is not None:
        index_dir = Path(hybrid_index_dir)
    elif hybrid_root in (unified_hybrid, legacy_hybrid):
        index_dir = hybrid_root / "hybrid_index"
    else:
        index_dir = resolve_profile_layout(hybrid_profile=DEFAULT_HYBRID_PROFILE).hybrid_index_dir

    if hybrid_captions_jsonl is not None:
        captions = Path(hybrid_captions_jsonl)
    elif hybrid_root == unified_hybrid:
        captions = hybrid_root / "captions.jsonl"
    elif hybrid_root == legacy_hybrid:
        candidates = (
            legacy_hybrid / "captions.jsonl",
            legacy_hybrid / "doubao_video_captions.jsonl",
        )
        captions = next((p for p in candidates if p.exists()), candidates[0])
    else:
        captions = resolve_profile_layout(hybrid_profile=DEFAULT_HYBRID_PROFILE).hybrid_captions_jsonl

    return ProfileLayout(
        profile=base or clip_name,
        clip_profile=clip_name,
        hybrid_profile=hybrid_name,
        unified=unified_clip_layout and unified_hybrid_layout,
        clip_root=clip_root,
        clip_output_dir=out,
        clip_metadata_db=clip_db,
        clip_logs_dir=out / "logs",
        hybrid_root=hybrid_root,
        hybrid_metadata_db=hybrid_db,
        hybrid_index_dir=index_dir,
        hybrid_captions_jsonl=captions,
        hybrid_logs_dir=hybrid_root / "logs",
        hybrid_output_dir=hybrid_root / "output",
    )


def default_clip_output_dir(profile: str | None) -> Path:
    name = normalize_profile_name(profile) or DEFAULT_CLIP_PROFILE
    return resolve_profile_layout(clip_profile=name).clip_output_dir


def default_clip_metadata_db_path(profile: str | None) -> Path:
    name = normalize_profile_name(profile) or DEFAULT_CLIP_PROFILE
    return resolve_profile_layout(clip_profile=name).clip_metadata_db


def clip_profile_root(profile: str | None) -> Path | None:
    name = normalize_profile_name(profile)
    if name is None:
        return None
    return resolve_profile_layout(clip_profile=name).clip_root


def default_hybrid_output_dir(profile: str | None) -> Path:
    name = normalize_profile_name(profile) or DEFAULT_HYBRID_PROFILE
    return resolve_profile_layout(hybrid_profile=name).hybrid_output_dir


def default_hybrid_metadata_db_path(profile: str | None) -> Path:
    name = normalize_profile_name(profile) or DEFAULT_HYBRID_PROFILE
    return resolve_profile_layout(hybrid_profile=name).hybrid_metadata_db


def default_hybrid_index_dir(profile: str | None) -> Path:
    name = normalize_profile_name(profile) or DEFAULT_HYBRID_PROFILE
    return resolve_profile_layout(hybrid_profile=name).hybrid_index_dir


def default_hybrid_captions_jsonl(profile: str | None) -> Path:
    name = normalize_profile_name(profile) or DEFAULT_HYBRID_PROFILE
    return resolve_profile_layout(hybrid_profile=name).hybrid_captions_jsonl


def hybrid_profile_root(profile: str | None) -> Path | None:
    name = normalize_profile_name(profile)
    if name is None:
        return None
    return resolve_profile_layout(hybrid_profile=name).hybrid_root


def ensure_unified_profile_dirs(layout: ProfileLayout) -> None:
    """Create unified profile tree (does not remove legacy paths)."""
    unified_clip = unified_profile_root(layout.clip_profile) / "clip"
    unified_hybrid = unified_profile_root(layout.hybrid_profile) / "hybrid"
    for path in (
        unified_clip / "output",
        unified_clip / "output" / "logs",
        unified_hybrid,
        unified_hybrid / "hybrid_index",
        unified_hybrid / "logs",
        unified_hybrid / "output",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (unified_clip / "metadata.db").touch(exist_ok=True)
    (unified_hybrid / "metadata.db").touch(exist_ok=True)


def resolve_path(value: str | Path | None, default: Path) -> Path:
    return Path(value) if value else default
