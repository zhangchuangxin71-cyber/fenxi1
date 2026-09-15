from __future__ import annotations

import os
from pathlib import Path

from .config import PACKAGE_ROOT, WORKSPACE_ROOT

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def parse_simple_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_default_dotenv_files() -> None:
    for dotenv_path in (
        WORKSPACE_ROOT / ".env",
        PACKAGE_ROOT / ".env",
    ):
        if not dotenv_path.exists():
            continue
        if load_dotenv is not None:
            load_dotenv(dotenv_path=dotenv_path, override=False)
            continue
        for key, value in parse_simple_dotenv(dotenv_path).items():
            os.environ.setdefault(key, value)


def env_first(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None
