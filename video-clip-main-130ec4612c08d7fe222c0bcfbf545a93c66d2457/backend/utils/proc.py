"""Safe subprocess helpers — FFmpeg stderr may contain non-UTF-8 bytes."""

from __future__ import annotations

import subprocess
from typing import Sequence


def run_text(
    cmd: Sequence[str],
    *,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command and decode stdout/stderr as UTF-8 with replacement.

    Avoids ``UnicodeDecodeError`` when tools emit GBK/latin-1 bytes
    (common with Chinese paths or metadata in FFmpeg logs).
    """
    return subprocess.run(
        list(cmd),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=check,
        timeout=timeout,
    )
