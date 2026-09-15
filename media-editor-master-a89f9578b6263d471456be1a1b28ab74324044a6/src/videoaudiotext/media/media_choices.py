"""Read media_choices.json (optional; used by xfade hints and shot tree)."""

from __future__ import annotations

import json

from videoaudiotext.config import MEDIA_CHOICES_JSON


def load_media_choices() -> dict | None:
    if not MEDIA_CHOICES_JSON.is_file():
        return None
    try:
        return json.loads(MEDIA_CHOICES_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return None
