from __future__ import annotations

import os

from .config import PICTURE_ROOT, WORKSPACE_ROOT

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def load_default_dotenv_files() -> None:
    for path in (WORKSPACE_ROOT / ".env", PICTURE_ROOT / ".env"):
        if not path.exists():
            continue
        if load_dotenv:
            load_dotenv(dotenv_path=path, override=False)


def env_first(*keys: str) -> str | None:
    for key in keys:
        v = os.environ.get(key)
        if v and v.strip():
            return v.strip()
    return None
