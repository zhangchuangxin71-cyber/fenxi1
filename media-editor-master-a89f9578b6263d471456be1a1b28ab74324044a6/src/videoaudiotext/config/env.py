"""Load project .env into os.environ (does not override existing vars)."""

from __future__ import annotations

import os
from pathlib import Path

from videoaudiotext.config.paths import PROJECT_ROOT

ENV_FILE = PROJECT_ROOT / ".env"


def load_dotenv() -> None:
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
