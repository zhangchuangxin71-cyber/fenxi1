"""Optional ASR force-alignment for subtitle cues (Whisper when available)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from videoaudiotext.subtitle.build import Cue

Cue = tuple[str, float, float]


def asr_align_enabled() -> bool:
    return os.environ.get("SUBTITLE_ASR_ALIGN", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def align_cues_with_audio(
    cues: List[Cue],
    wav_paths: List[Path],
    *,
    reference_texts: List[str] | None = None,
) -> List[Cue]:
    """
    使用轻量 ASR 对每段 wav 做强制对齐，修正 SRT 起止时间。
    未安装 whisper 或未开启 SUBTITLE_ASR_ALIGN 时原样返回。
    """
    if not asr_align_enabled() or not cues:
        return cues
    if len(wav_paths) != len(cues):
        return cues

    try:
        import whisper  # type: ignore
    except ImportError:
        return cues

    model_name = os.environ.get("SUBTITLE_ASR_MODEL", "tiny")
    try:
        model = whisper.load_model(model_name)
    except Exception:
        return cues

    aligned: List[Cue] = []
    offset = 0.0
    for i, (text, _start, _end) in enumerate(cues):
        wav = wav_paths[i]
        if not wav.is_file():
            aligned.append((text, offset, offset + max(_end - _start, 0.3)))
            offset += max(_end - _start, 0.3)
            continue
        try:
            result = model.transcribe(
                str(wav),
                language="zh",
                word_timestamps=True,
                initial_prompt=(reference_texts[i] if reference_texts else text),
            )
            segs = result.get("segments") or []
            if segs:
                seg_start = float(segs[0].get("start", 0))
                seg_end = float(segs[-1].get("end", seg_start + 0.3))
                dur = max(0.3, seg_end - seg_start)
                aligned.append((text, offset, offset + dur))
                offset += dur
                continue
        except Exception:
            pass
        dur = max(0.3, _end - _start)
        aligned.append((text, offset, offset + dur))
        offset += dur
    return aligned if aligned else cues
