"""Repository root and path constants anchor."""

from __future__ import annotations

import os
from pathlib import Path


def _resolve_project_root() -> Path:
    """Docker / pip install 场景可通过 PROJECT_ROOT 指向应用根（如 /app）。"""
    raw = os.environ.get("PROJECT_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    # src/videoaudiotext/config/paths.py -> repo root
    return Path(__file__).resolve().parents[3]


PROJECT_ROOT = _resolve_project_root()
ROOT = PROJECT_ROOT
