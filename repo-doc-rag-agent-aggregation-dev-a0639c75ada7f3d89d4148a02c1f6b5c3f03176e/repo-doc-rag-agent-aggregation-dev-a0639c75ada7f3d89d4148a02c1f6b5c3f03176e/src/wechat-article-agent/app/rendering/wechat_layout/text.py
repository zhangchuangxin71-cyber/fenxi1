from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")


def normalize_visible_text(value: str) -> str:
    """Normalize layout-only whitespace without changing visible characters."""

    return _WHITESPACE.sub(" ", value).strip()
