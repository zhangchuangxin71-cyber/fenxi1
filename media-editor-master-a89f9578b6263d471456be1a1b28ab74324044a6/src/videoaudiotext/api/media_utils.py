"""Shared media helpers (SRT parsing, paths, content-type)."""

from __future__ import annotations

import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Any


def ensure_dir(directory: str | Path) -> Path:
    """Ensure directory exists; return resolved path."""
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_temp_path(file_path: str | Path, suffix: str) -> Path:
    """Return a sibling temp path with an extra suffix before the extension."""
    path = Path(file_path)
    return path.with_name(f"{path.stem}_{suffix}{path.suffix}")


def parse_srt_to_json(srt_text: str) -> list[dict[str, Any]]:
    """Parse SRT text into structured JSON entries."""
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    results: list[dict[str, Any]] = []

    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        try:
            index = int(lines[0])
            time_range = lines[1]
            text = " ".join(lines[2:])
            results.append(
                {
                    "index": index,
                    "time_range": time_range,
                    "text": text.strip(),
                }
            )
        except (ValueError, IndexError):
            continue

    return results


def parse_time(time_str: str) -> float:
    """Convert ``00:00:00,150`` style timestamp to seconds."""
    time_obj = datetime.strptime(time_str.replace(",", "."), "%H:%M:%S.%f")
    return (
        time_obj.hour * 3600
        + time_obj.minute * 60
        + time_obj.second
        + time_obj.microsecond / 1e6
    )


def parse_srt_time(srt_time: str) -> tuple[float, float]:
    """Convert an SRT time range to start/end seconds."""

    def to_seconds(time_str: str) -> float:
        h, m, s = time_str.replace(",", ".").split(":")
        return float(h) * 3600 + float(m) * 60 + float(s)

    start_str, end_str = srt_time.split(" --> ")
    return to_seconds(start_str), to_seconds(end_str)


def get_extension_from_content_type(content_type: str | None) -> str | None:
    """Guess file extension from HTTP Content-Type."""
    if not content_type:
        return None
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
    return ext or ".bin"
