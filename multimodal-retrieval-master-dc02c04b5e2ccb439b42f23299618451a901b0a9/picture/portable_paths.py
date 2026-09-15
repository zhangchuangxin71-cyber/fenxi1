"""Resolve image paths for picture retrieval (simpler rules than video)."""

from __future__ import annotations

import re
from pathlib import Path

from .config import WORKSPACE_ROOT

DEFAULT_PORTABLE_ROOTS = (WORKSPACE_ROOT,)
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PATH_SPLIT_RE = re.compile(r"[\\/]+")


def _split_path_parts(value: str) -> tuple[str, ...]:
    return tuple(part for part in _PATH_SPLIT_RE.split(str(value).strip()) if part and part != ".")


def resolve_portable_path(value: str | Path, roots: tuple[Path, ...] | None = None) -> Path:
    text = str(value or "").strip()
    if not text:
        return Path(".").resolve()
    path = Path(text)
    if path.is_absolute() and path.exists():
        return path.resolve()
    roots = roots or DEFAULT_PORTABLE_ROOTS
    parts = _split_path_parts(text)
    lowered = [p.lower() for p in parts]
    for root in roots:
        anchor = root.name.lower()
        for idx in reversed([i for i, p in enumerate(lowered) if p == anchor]):
            candidate = root.joinpath(*parts[idx + 1 :])
            if candidate.exists():
                return candidate.resolve()
    candidate = (roots[0] / text).resolve()
    return candidate


def portable_path_text(value: str | Path | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        return text.replace("\\", "/")
    for root in DEFAULT_PORTABLE_ROOTS:
        try:
            rel = path.resolve().relative_to(root.resolve())
            return rel.as_posix()
        except ValueError:
            continue
    return text.replace("\\", "/")


def resolved_path_text(value: str | None) -> str | None:
    if value is None:
        return None
    return str(resolve_portable_path(value))
