"""Audio waveform peak helpers for timeline visualization."""

from __future__ import annotations

import json
import math
import struct
import subprocess
from pathlib import Path

from backend.config import UPLOAD_DIR
from backend.core.cutter import FFmpegError, _resolve_ffmpeg, media_has_audio

DEFAULT_BINS = 1200
MAX_BINS = 4000
MIN_BINS = 64
CACHE_VER = "v1"
# 下采样采样率：足够画波形，体积可控
WAVE_SAMPLE_RATE = 8000


def waveform_path(video_id: str, bins: int) -> Path:
    return UPLOAD_DIR / f"{video_id}_waveform_{CACHE_VER}_{int(bins)}.json"


def suggest_waveform_bins(duration: float) -> int:
    """按时长建议峰值点数：约每 0.1s 一个峰，夹在 MIN/MAX。"""
    span = max(float(duration), 0.1)
    raw = int(round(span / 0.1))
    return max(MIN_BINS, min(MAX_BINS, raw))


def _pcm_to_peaks(pcm: bytes, bins: int) -> list[float]:
    if not pcm:
        return [0.0] * bins
    # float32 little-endian mono
    n = len(pcm) // 4
    if n <= 0:
        return [0.0] * bins
    samples = struct.unpack(f"<{n}f", pcm[: n * 4])
    peaks = [0.0] * bins
    for i, sample in enumerate(samples):
        idx = min(bins - 1, int(i * bins / n))
        v = abs(float(sample))
        if v > peaks[idx]:
            peaks[idx] = v
    peak_max = max(peaks) if peaks else 0.0
    if peak_max <= 1e-8:
        return [0.0] * bins
    # 轻柔压缩，避免只有个别尖峰
    return [min(1.0, math.sqrt(p / peak_max)) for p in peaks]


def generate_waveform_peaks(
    video_path: Path,
    *,
    bins: int = DEFAULT_BINS,
) -> list[float]:
    """用 ffmpeg 抽 mono PCM，再聚合成 bins 个峰值（0~1）。"""
    bins = max(MIN_BINS, min(int(bins), MAX_BINS))
    if not media_has_audio(video_path):
        return [0.0] * bins

    ffmpeg = _resolve_ffmpeg()
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(WAVE_SAMPLE_RATE),
        "-f",
        "f32le",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", errors="ignore").strip()
        raise FFmpegError(f"生成音频波形失败: {err[:400]}") from exc

    return _pcm_to_peaks(result.stdout, bins)


def load_or_build_waveform(
    video_id: str,
    video_path: Path,
    *,
    bins: int | None = None,
    duration: float | None = None,
) -> dict:
    resolved = (
        int(bins)
        if bins is not None
        else suggest_waveform_bins(duration or 60.0)
    )
    resolved = max(MIN_BINS, min(resolved, MAX_BINS))
    cache = waveform_path(video_id, resolved)
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            peaks = data.get("peaks")
            if isinstance(peaks, list) and peaks:
                return {
                    "bins": len(peaks),
                    "peaks": [float(x) for x in peaks],
                    "has_audio": bool(data.get("has_audio", True)),
                }
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            cache.unlink(missing_ok=True)

    has_audio = media_has_audio(video_path)
    peaks = (
        generate_waveform_peaks(video_path, bins=resolved)
        if has_audio
        else [0.0] * resolved
    )
    payload = {
        "bins": len(peaks),
        "peaks": peaks,
        "has_audio": has_audio,
        "version": CACHE_VER,
    }
    try:
        cache.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass
    return payload


def delete_waveforms(video_id: str) -> None:
    for path in UPLOAD_DIR.glob(f"{video_id}_waveform_*.json"):
        path.unlink(missing_ok=True)
