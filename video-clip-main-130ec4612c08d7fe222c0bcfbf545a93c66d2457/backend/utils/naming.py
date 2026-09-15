"""统一下载/ZIP 展示文件名，避免 clip_/segment_ 分裂与跨任务撞名。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

_INVALID = re.compile(r'[\\/:*?"<>|\s]+')


def sanitize_stem(filename: str | None, *, max_len: int = 40) -> str:
    raw = Path(filename or "video").stem.strip() or "video"
    cleaned = _INVALID.sub("_", raw).strip("._") or "video"
    return cleaned[:max_len]


def segment_download_filename(
    source_filename: str | None,
    task_id: str,
    index: int,
    start_time: float,
    end_time: float,
) -> str:
    """
    {源片名}__{task短ID}__seg{序号}__{入点ms}-{出点ms}.mp4
    """
    stem = sanitize_stem(source_filename)
    tid = (task_id or "task")[:12]
    start_ms = max(0, int(round(float(start_time) * 1000)))
    end_ms = max(start_ms, int(round(float(end_time) * 1000)))
    return f"{stem}__{tid}__seg{int(index):03d}__{start_ms}-{end_ms}.mp4"


def task_zip_filename(
    source_filename: str | None,
    task_id: str,
    *,
    selected: bool = False,
) -> str:
    stem = sanitize_stem(source_filename)
    tid = (task_id or "task")[:12]
    suffix = "clips_selected" if selected else "clips"
    return f"{stem}__{tid}__{suffix}.zip"


def content_disposition_attachment(filename: str) -> str:
    """支持中文文件名的 Content-Disposition。"""
    name = Path(filename).name
    ascii_name = name.encode("ascii", "ignore").decode("ascii").strip("._") or "download.bin"
    if ascii_name != name:
        return (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(name)}"
        )
    return f'attachment; filename="{name}"'
