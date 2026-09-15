"""Timeline filmstrip (sprite strip) helpers."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from backend.config import UPLOAD_DIR
from backend.core.cutter import FFmpegError, _resolve_ffmpeg, probe_duration
from backend.utils.proc import run_text


DEFAULT_COUNT = 24
# 硬上限：约每 3 秒一格时覆盖 ~6 分钟；更长的片自动变疏，避免一次抽数百帧卡死
MAX_COUNT = 120
MIN_COUNT = 8
# 约每 N 秒一帧；张数按时长计算，再受 MAX_COUNT 约束
SECONDS_PER_THUMB = 3.0
FRAME_W = 120
FRAME_H = 68
# 缓存文件名版本；改采样规则后递增
CACHE_VER = "v9"
# 限制同时跑的胶片 ffmpeg，避免拖垮整机/API
_FILMSTRIP_SEM = threading.Semaphore(1)
_FILMSTRIP_TIMEOUT_SEC = 180


def suggest_filmstrip_count(span_seconds: float) -> int:
    """按时间跨度建议缩略图张数：约每 3 秒一格，最长 MAX_COUNT。"""
    span = max(float(span_seconds), 0.1)
    raw = int(round(span / SECONDS_PER_THUMB))
    return max(MIN_COUNT, min(MAX_COUNT, raw))



def filmstrip_path(
    video_id: str,
    count: int,
    *,
    start: float = 0.0,
    end: float | None = None,
    duration: float | None = None,
) -> Path:
    count = int(count)
    start = max(0.0, float(start))
    # 覆盖整段时用稳定文件名，便于与列表缩略图共享缓存
    if end is None or (
        duration is not None and start <= 0.01 and float(end) >= float(duration) - 0.05
    ):
        return UPLOAD_DIR / f"{video_id}_filmstrip_{CACHE_VER}_{count}.jpg"
    s = int(round(start * 10))
    e = int(round(float(end) * 10))
    return UPLOAD_DIR / f"{video_id}_filmstrip_{CACHE_VER}_{count}_{s}_{e}.jpg"


def generate_filmstrip(
    video_path: Path,
    out_path: Path,
    *,
    duration: float,
    count: int = DEFAULT_COUNT,
    start: float = 0.0,
    end: float | None = None,
    frame_w: int = FRAME_W,
    frame_h: int = FRAME_H,
) -> Path:
    """Build a horizontal N-tile JPEG filmstrip via ffmpeg.

    When start/end are set, tiles cover that window only (for zoomed timelines).
    Samples at each tile's center time with accurate (decode) seek so frames
    align with the player playhead.
    """
    count = max(MIN_COUNT, min(int(count), MAX_COUNT))
    duration = max(float(duration), 0.1)
    # 以视频流真实时长为准，避免容器时长偏长导致片尾抽到黑帧
    try:
        stream_dur = probe_duration(video_path)
        if stream_dur > 0.05:
            duration = min(duration, stream_dur)
    except FFmpegError:
        pass
    # 末格略提前，避开最后一帧之后的空白
    duration = max(0.1, duration - 0.04)

    start = max(0.0, min(float(start), duration))
    end_t = duration if end is None else max(float(end), start + 0.05)
    end_t = min(end_t, duration)
    span = max(end_t - start, 0.05)

    # 在每格中心采样：第 i 格对应 start + (i+0.5)*span/count
    half = span / (2.0 * count)
    sample_start = start + half
    fps = count / span
    # 读到最后一格中心再略多一点，保证凑满 count 帧
    read_dur = max(span - half + half / count, span / count * (count - 0.5) + 0.05)

    vf = (
        f"fps={fps:.8f},"
        f"select='lt(n\\,{count})',"
        f"scale={frame_w}:{frame_h}:force_original_aspect_ratio=decrease,"
        f"pad={frame_w}:{frame_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"tile={count}x1"
    )

    # 只用输入前 -ss。输入后 -ss + 低 fps/select/tile 在短片上常得到 0 帧
    #（ffmpeg 报 Output file is empty），横竖屏都会中招。
    ffmpeg = _resolve_ffmpeg()
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-an",
        "-sn",
        "-ss",
        f"{sample_start:.3f}",
        "-i",
        str(video_path),
        "-t",
        f"{read_dur:.3f}",
        "-vf",
        vf,
        "-frames:v",
        "1",
        "-q:v",
        "6",
        str(out_path),
    ]
    try:
        with _FILMSTRIP_SEM:
            # 生成过程中若已有其它请求写好缓存，直接复用
            if out_path.exists() and out_path.stat().st_size >= 32:
                return out_path
            run_text(
                cmd,
                check=True,
                timeout=_FILMSTRIP_TIMEOUT_SEC,
            )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError("生成时间轴缩略图超时，请稍后重试或缩小缩放范围") from exc
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc)).strip()
        raise FFmpegError(f"生成时间轴缩略图失败: {err[:400]}") from exc
    if not out_path.exists() or out_path.stat().st_size < 32:
        raise FFmpegError("生成时间轴缩略图失败: 输出为空")
    return out_path


def delete_filmstrips(video_id: str) -> None:
    for path in UPLOAD_DIR.glob(f"{video_id}_filmstrip_*.jpg"):
        path.unlink(missing_ok=True)
