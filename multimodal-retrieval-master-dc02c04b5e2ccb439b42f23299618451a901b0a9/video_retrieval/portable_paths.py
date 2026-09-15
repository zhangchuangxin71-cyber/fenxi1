"""Resolve portable paths stored in metadata across machines and legacy repo layouts."""

from __future__ import annotations

import re
from pathlib import Path

from .config import WORKSPACE_ROOT
from .path_compat import rewrite_legacy_suffix

DEFAULT_PORTABLE_ROOTS = (WORKSPACE_ROOT.resolve(),)
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PATH_SPLIT_RE = re.compile(r"[\\/]+")


def _normalized_roots(roots: tuple[Path, ...] | list[Path] | None = None) -> tuple[Path, ...]:
    values = roots or DEFAULT_PORTABLE_ROOTS
    result: list[Path] = []
    seen: set[str] = set()
    for root in values:
        resolved = Path(root).resolve()
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return tuple(result)


def _split_path_parts(value: str) -> tuple[str, ...]:
    return tuple(part for part in _PATH_SPLIT_RE.split(str(value).strip()) if part and part != ".")


def _looks_like_windows_absolute(value: str) -> bool:
    return bool(_WINDOWS_ABS_RE.match(str(value).strip()))


def _candidate_rebased_paths(raw_text: str, roots: tuple[Path, ...]) -> list[Path]:
    parts = _split_path_parts(raw_text)
    lowered = [part.lower() for part in parts]
    candidates: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        anchor = root.name.lower()
        matches = [index for index, part in enumerate(lowered) if part == anchor]
        for match_index in reversed(matches):
            suffix = rewrite_legacy_suffix(parts[match_index + 1 :])
            candidate = root.joinpath(*suffix)
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    return candidates


def _first_existing(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve() if candidates else None


def resolve_portable_path(
    value: str | Path,
    roots: tuple[Path, ...] | list[Path] | None = None,
) -> Path:
    """Map a stored path string to an existing local file when possible."""
    text = str(value or "").strip()
    if not text:
        return Path(".").resolve()

    normalized_roots = _normalized_roots(roots)
    if _looks_like_windows_absolute(text):
        rebased = _candidate_rebased_paths(text, normalized_roots)
        found = _first_existing(rebased)
        if found is not None:
            return found
        return Path(text.replace("\\", "/")).resolve()

    path = Path(text)
    if path.is_absolute():
        if path.exists():
            return path.resolve()
        rebased = _candidate_rebased_paths(text, normalized_roots)
        found = _first_existing(rebased)
        if found is not None:
            return found
        return path.resolve()

    parts = _split_path_parts(text)
    legacy_parts = rewrite_legacy_suffix(parts)
    if legacy_parts != parts:
        for root in normalized_roots:
            candidate = root.joinpath(*legacy_parts).resolve()
            if candidate.exists():
                return candidate
        if normalized_roots:
            return normalized_roots[0].joinpath(*legacy_parts).resolve()

    normalized_relative = Path(text.replace("\\", "/"))
    for root in normalized_roots:
        candidate = (root / normalized_relative).resolve()
        if candidate.exists():
            return candidate
    if normalized_roots:
        return (normalized_roots[0] / normalized_relative).resolve()
    return normalized_relative.resolve()


def make_portable_path(
    value: str | Path | None,
    roots: tuple[Path, ...] | list[Path] | None = None,
) -> str | None:
    """Store a path relative to the first matching project root."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""

    normalized_roots = _normalized_roots(roots)
    resolved = resolve_portable_path(text, normalized_roots)
    for root in normalized_roots:
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            continue
    return str(resolved)


def portable_path_text(
    value: str | Path | None,
    roots: tuple[Path, ...] | list[Path] | None = None,
) -> str | None:
    return make_portable_path(value, roots)


def resolved_path_text(
    value: str | Path | None,
    roots: tuple[Path, ...] | list[Path] | None = None,
) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""
    return str(resolve_portable_path(text, roots))
