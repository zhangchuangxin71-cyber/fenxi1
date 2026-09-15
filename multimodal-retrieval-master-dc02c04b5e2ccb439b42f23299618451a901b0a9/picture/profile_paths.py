from __future__ import annotations

from pathlib import Path

from .config import PICTURE_ROOT


def normalize_profile_name(profile: str | None) -> str | None:
    if profile is None:
        return None
    text = profile.strip()
    return text or None


def profile_root(profile: str | None) -> Path | None:
    name = normalize_profile_name(profile)
    if name is None:
        return None
    return PICTURE_ROOT / "profiles" / name


def default_output_dir(profile: str | None) -> Path:
    root = profile_root(profile)
    if root is None:
        return PICTURE_ROOT / "output"
    return root / "output"


def default_metadata_db_path(profile: str | None) -> Path:
    root = profile_root(profile)
    if root is None:
        return PICTURE_ROOT / "metadata" / "metadata.db"
    return root / "metadata.db"


def default_caption_metadata_db_path(profile: str | None) -> Path:
    root = profile_root(profile)
    if root is None:
        return PICTURE_ROOT / "metadata" / "caption_metadata.db"
    return root / "caption_metadata.db"


def resolve_path(value: str | Path | None, default: Path) -> Path:
    return Path(value) if value else default
