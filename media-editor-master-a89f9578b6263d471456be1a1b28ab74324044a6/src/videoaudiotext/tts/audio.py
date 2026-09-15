"""WAV I/O helpers for Flow B (duration probe, concat, list)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import List

from videoaudiotext.config import AUDIO_DIR

SEGMENTS_DIGEST_FILE = "segments_digest.txt"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def list_sentence_wavs(audio_dir: Path = AUDIO_DIR) -> List[Path]:
    wavs = [p for p in audio_dir.glob("*.wav") if p.stem.isdigit()]
    return sorted(wavs, key=lambda p: int(p.stem))


def get_audio_duration_seconds(wav_path: Path) -> float:
    """Read duration via ffprobe (fallback: ffmpeg stderr parse)."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(wav_path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        data = json.loads(out)
        return float(data["format"]["duration"])
    except (subprocess.CalledProcessError, KeyError, ValueError, json.JSONDecodeError):
        cmd2 = ["ffmpeg", "-i", str(wav_path), "-f", "null", "-"]
        proc = subprocess.run(cmd2, capture_output=True, text=True)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
        raise RuntimeError(f"Cannot read duration for {wav_path}")


def measure_wav_durations(wav_paths: List[Path]) -> List[float]:
    return [get_audio_duration_seconds(w) for w in wav_paths]


def pad_audio_with_gap(wav: Path, gap_sec: float, out: Path) -> None:
    _ensure_dir(out.parent)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(wav),
        "-af",
        f"apad=pad_dur={gap_sec}",
        "-c:a",
        "pcm_s16le",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def concat_wav_files(inputs: List[Path], out: Path) -> None:
    """顺序拼接多段 WAV/音频为一条音轨。"""
    _ensure_dir(out.parent)
    if not inputs:
        raise ValueError("concat_wav_files: no inputs")
    if len(inputs) == 1:
        import shutil

        shutil.copy2(inputs[0], out)
        return
    cmd = ["ffmpeg", "-y"]
    for inp in inputs:
        cmd.extend(["-i", str(inp)])
    n = len(inputs)
    fc = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[outa]"
    cmd.extend(
        [
            "-filter_complex",
            fc,
            "-map",
            "[outa]",
            "-c:a",
            "pcm_s16le",
            str(out),
        ]
    )
    subprocess.run(cmd, check=True, capture_output=True)
