"""FFmpeg subprocess helpers with actionable error messages."""

from __future__ import annotations

import shutil
import subprocess
from typing import Sequence


def check_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("未找到 ffmpeg，请先安装：apt install ffmpeg")
    if not shutil.which("ffprobe"):
        raise RuntimeError("未找到 ffprobe（通常随 ffmpeg 安装）")


def libx264_video_args(
    *,
    preset: str | None = None,
    crf: str | None = None,
) -> list[str]:
    """libx264 视频编码参数（preset/crf 默认走 CLIP 中间档）。"""
    from videoaudiotext.config.output import CLIP_X264_CRF, CLIP_X264_PRESET

    p = (preset or CLIP_X264_PRESET).strip() or CLIP_X264_PRESET
    c = (crf or CLIP_X264_CRF).strip() or CLIP_X264_CRF
    return ["-c:v", "libx264", "-preset", p, "-crf", str(c), "-pix_fmt", "yuv420p"]


def run_ffmpeg(
    cmd: Sequence[str],
    *,
    check: bool = True,
    label: str = "ffmpeg",
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        tail = detail[-2000:] if len(detail) > 2000 else detail
        raise RuntimeError(
            f"{label} 失败 (exit {proc.returncode}):\n{tail or '(无 stderr 输出)'}"
        )
    return proc
