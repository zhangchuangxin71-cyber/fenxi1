from __future__ import annotations

import contextlib
from pathlib import Path

from .portable_paths import resolve_portable_path


def resolve_image_file(
    image_id: str,
    path_text: str | None,
    search_roots: list[Path],
) -> Path | None:
    """Try multiple roots + portable path until an image file exists."""
    candidates: list[Path] = []
    raw = (path_text or "").strip()

    if raw:
        p = Path(raw)
        if p.is_absolute():
            candidates.append(p)
        else:
            for root in search_roots:
                if root:
                    candidates.append(root / raw)

    stem = image_id.strip()
    if stem:
        for root in search_roots:
            if not root:
                continue
            for name in (f"{stem}.jpg", f"{stem}.jpeg", f"{stem}.png", stem):
                candidates.append(root / name)

    if raw:
        with contextlib.suppress(Exception):
            candidates.append(resolve_portable_path(raw))

    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path.resolve()
    return None
