"""Background music listing, ducking mix, and final audio."""

from __future__ import annotations

import os
from pathlib import Path

from videoaudiotext.core.ffmpeg_util import run_ffmpeg
from videoaudiotext.tts.audio import get_audio_duration_seconds

_BGM_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def bgm_ducking_enabled() -> bool:
    from videoaudiotext.config.settings import BGM_DUCKING_ENABLED

    return os.environ.get("BGM_DUCKING", "1" if BGM_DUCKING_ENABLED else "0").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def list_bgm_files(bgm_dir: Path) -> list[Path]:
    if not bgm_dir.is_dir():
        return []
    return sorted(
        p.resolve()
        for p in bgm_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _BGM_EXTS
    )


def bgm_dropdown_choices(bgm_dir: Path | None = None) -> list[tuple[str, str]]:
    from videoaudiotext.config import BGM_DIR, DEFAULT_BGM_PATH

    root = bgm_dir or BGM_DIR
    choices: list[tuple[str, str]] = [("无背景音乐", "")]
    seen: set[str] = set()
    for path in list_bgm_files(root):
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        choices.append((path.name, key))
    if DEFAULT_BGM_PATH and DEFAULT_BGM_PATH.is_file():
        key = str(DEFAULT_BGM_PATH.resolve())
        if key not in seen:
            choices.append((DEFAULT_BGM_PATH.name, key))
    return choices


def default_bgm_dropdown_value() -> str:
    from videoaudiotext.config import DEFAULT_BGM_PATH

    if DEFAULT_BGM_PATH and DEFAULT_BGM_PATH.is_file():
        return str(DEFAULT_BGM_PATH.resolve())
    return ""


def resolve_bgm_path(bgm_path: str | Path | None = None) -> Path | None:
    from videoaudiotext.config import DEFAULT_BGM_PATH

    if bgm_path is not None:
        raw = str(bgm_path).strip()
        if not raw or raw.lower() in {"0", "false", "no", "off", "none"}:
            return None
        path = Path(raw)
        resolved = path.resolve()
        return resolved if resolved.is_file() else None
    if DEFAULT_BGM_PATH and DEFAULT_BGM_PATH.is_file():
        return DEFAULT_BGM_PATH.resolve()
    return None


def _ducking_filter(
    voice_vol: float,
    bgm_vol: float,
    target_dur: float,
) -> str:
    from videoaudiotext.config.settings import BGM_DUCKING_RATIO, BGM_DUCKING_THRESHOLD

    ratio = float(os.environ.get("BGM_DUCKING_RATIO", str(BGM_DUCKING_RATIO)))
    threshold = float(os.environ.get("BGM_DUCKING_THRESHOLD", str(BGM_DUCKING_THRESHOLD)))
    # sidechaincompress 会消耗 sidechain 输入，口播需 asplit 一路 duck、一路 amix
    return (
        f"[0:a]volume={voice_vol:.4f},aresample=44100,aformat=channel_layouts=stereo,asplit=2[sc][voice];"
        f"[1:a]volume={bgm_vol:.4f},atrim=0:{target_dur:.3f},"
        f"aresample=44100,aformat=channel_layouts=stereo[bgmraw];"
        f"[bgmraw][sc]sidechaincompress="
        f"threshold={threshold:.4f}:ratio={ratio:.1f}:attack=20:release=350:level_sc=1[bgmduck];"
        "[voice][bgmduck]amix=inputs=2:duration=first:dropout_transition=0[outa]"
    )


def build_final_audio(
    master_path: Path,
    out_path: Path,
    *,
    bgm_path: str | Path | None = None,
    voice_volume: float = 1.0,
    bgm_volume: float = 0.15,
    duration: float | None = None,
) -> Path:
    """配音 + 可选 BGM 混音（默认口播时 BGM ducking）。"""
    master_path = Path(master_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    voice_vol = max(0.0, float(voice_volume))
    bgm_vol = max(0.0, float(bgm_volume))
    effective_bgm = resolve_bgm_path(bgm_path)
    target_dur = duration
    if target_dur is None or target_dur <= 0:
        target_dur = get_audio_duration_seconds(master_path)

    if effective_bgm is None or bgm_vol <= 0.001:
        if abs(voice_vol - 1.0) < 0.001:
            if master_path.resolve() != out_path.resolve():
                out_path.write_bytes(master_path.read_bytes())
            return out_path
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(master_path),
            "-filter:a",
            f"volume={voice_vol:.4f}",
            "-c:a",
            "pcm_s16le",
            str(out_path),
        ]
        run_ffmpeg(cmd, label="voice-volume")
        return out_path

    if bgm_ducking_enabled():
        fc = _ducking_filter(voice_vol, bgm_vol, target_dur)
        label = "bgm-mix-duck"
    else:
        fc = (
            f"[0:a]volume={voice_vol:.4f}[v];"
            f"[1:a]volume={bgm_vol:.4f},atrim=0:{target_dur:.3f}[b];"
            "[v][b]amix=inputs=2:duration=first:dropout_transition=0[outa]"
        )
        label = "bgm-mix"

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(master_path),
        "-stream_loop",
        "-1",
        "-i",
        str(effective_bgm),
        "-filter_complex",
        fc,
        "-map",
        "[outa]",
        "-c:a",
        "pcm_s16le",
        "-t",
        f"{target_dur:.3f}",
        str(out_path),
    ]
    run_ffmpeg(cmd, label=label)
    return out_path
