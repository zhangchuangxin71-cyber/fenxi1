"""Detect pauses at comma boundaries from TTS wav (production timeline split)."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

from videoaudiotext.subtitle.text_timing import get_phonetic_weight


def estimate_boundary_time(
    segment_text: str,
    prefix_text: str,
    speech_duration: float,
) -> float:
    """按拟音权重估算 prefix 结束点在段内的秒数。"""
    w_full = max(0.1, get_phonetic_weight(segment_text))
    w_pre = max(0.1, get_phonetic_weight(prefix_text))
    return max(0.0, min(float(speech_duration), speech_duration * (w_pre / w_full)))


def _read_wav_samples(path: Path) -> tuple[list[float], int]:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        width = wf.getsampwidth()
        ch = wf.getnchannels()
        raw = wf.readframes(n)
    if width == 2:
        fmt = f"<{n * ch}h"
        samples = struct.unpack(fmt, raw)
        data = [s / 32768.0 for s in samples]
    else:
        data = [b / 128.0 - 1.0 for b in raw]
    if ch > 1:
        data = [sum(data[i : i + ch]) / ch for i in range(0, len(data), ch)]
    return data, sr


def _rms(values: list[float]) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum(v * v for v in values) / len(values))


def silence_duration_in_window(
    path: Path,
    center_sec: float,
    *,
    window_before: float = 0.06,
    window_after: float = 0.18,
    amp_threshold: float = 0.018,
    min_run_ms: int = 40,
) -> float:
    """
    在 center 附近窗口内检测最长连续低能量段（视为换气静音）。
    返回静音时长（秒），无有效 wav 时返回 0。
    """
    if not path.is_file():
        return 0.0
    try:
        samples, sr = _read_wav_samples(path)
    except (wave.Error, struct.error, OSError, ValueError):
        return 0.0
    if sr <= 0 or not samples:
        return 0.0

    t0 = max(0.0, center_sec - window_before)
    t1 = min(len(samples) / sr, center_sec + window_after)
    i0 = int(t0 * sr)
    i1 = max(i0 + 1, int(t1 * sr))

    frame = max(1, int(sr * 0.01))
    min_run = max(1, int(sr * min_run_ms / 1000.0))

    longest = 0
    run = 0
    for i in range(i0, min(i1, len(samples)), frame):
        chunk = samples[i : min(i + frame, len(samples))]
        if _rms(chunk) < amp_threshold:
            run += frame
            longest = max(longest, run)
        else:
            run = 0

    if longest < min_run:
        return 0.0
    return longest / sr


def pause_at_text_boundary(
    wav_path: Path | str | None,
    segment_text: str,
    prefix_text: str,
    speech_duration: float,
    *,
    min_pause: float = 0.15,
) -> float:
    """逗号边界处测得的最长静音（秒）。"""
    if not wav_path:
        return 0.0
    path = Path(wav_path)
    center = estimate_boundary_time(segment_text, prefix_text, speech_duration)
    return silence_duration_in_window(path, center)


def comma_has_audio_pause(
    wav_path: Path | str | None,
    segment_text: str,
    prefix_text: str,
    speech_duration: float,
    *,
    min_pause: float = 0.15,
) -> bool:
    return pause_at_text_boundary(
        wav_path,
        segment_text,
        prefix_text,
        speech_duration,
        min_pause=min_pause,
    ) >= min_pause
