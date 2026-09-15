"""Shared config helpers."""

from __future__ import annotations

import os


def env_bool(name: str, default: str = "1") -> bool:
    raw = os.environ.get(name, default).strip().lower()
    return raw in {"1", "true", "yes", "on"}
