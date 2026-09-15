"""Segment Timeline Master — single source for subtitle + multi-clip video."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from videoaudiotext.config import (
    CLIP_PARTS_JSON,
    SEGMENT_TIMELINE_JSON,
    SOURCE_MEDIA_DIR,
    comma_multi_material_enabled,
)
from videoaudiotext.subtitle.text_timing import split_segment_into_steps

TimelineStore = dict[str, dict]


def _load_clip_parts(source_dir: Path) -> dict[str, dict]:
    path = source_dir / "clip_parts.json"
    if source_dir == SOURCE_MEDIA_DIR:
        path = CLIP_PARTS_JSON
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): v for k, v in data.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _steps_to_parts(steps: list[dict]) -> list[dict]:
    parts: list[dict] = []
    for step in steps:
        s0 = float(step.get("start_time") or step.get("speech_start") or 0.0)
        s1 = float(step.get("end_time") or step.get("speech_end") or s0)
        parts.append(
            {
                "part_index": int(step.get("part_index") or len(parts) + 1),
                "text": str(step.get("text") or ""),
                "comma_group": int(step.get("comma_group") or 0),
                "weight_phonetic": float(step.get("weight_phonetic") or 0.0),
                "speech_start": s0,
                "speech_end": s1,
                "duration": float(step.get("duration") or max(0.0, s1 - s0)),
                "start_time": s0,
                "end_time": s1,
            }
        )
    return parts


def build_segment_timeline(
    segment_id: int,
    text: str,
    speech_duration: float,
    *,
    video_duration: float | None = None,
    wav_path: Path | None = None,
) -> dict:
    speech = max(0.01, float(speech_duration))
    steps = split_segment_into_steps(text, speech, wav_path=wav_path)
    parts = _steps_to_parts(steps) if steps else []
    if not parts:
        parts = [
            {
                "part_index": 1,
                "text": text,
                "comma_group": 0,
                "weight_phonetic": 0.0,
                "speech_start": 0.0,
                "speech_end": speech,
                "duration": speech,
                "start_time": 0.0,
                "end_time": speech,
            }
        ]
    vid_dur = float(video_duration if video_duration is not None else speech)
    return {
        "segment_id": segment_id,
        "segment_index": segment_id,
        "full_text": text,
        "text": text,
        "total_audio_duration": speech,
        "speech_duration": speech,
        "video_duration": vid_dur,
        "parts": parts,
        "timeline_parts": list(parts),
        "video_parts": [
            {
                "video_group": 0,
                "text": text,
                "video_start": 0.0,
                "video_end": vid_dur,
                "video_duration": vid_dur,
            }
        ],
    }


def _attach_clip_parts(
    store: TimelineStore,
    source_dir: Path,
    *,
    video_durations: List[float] | None,
) -> None:
    clip_parts = _load_clip_parts(source_dir)
    if not comma_multi_material_enabled() or not clip_parts:
        return
    for key, entry in store.items():
        spec = clip_parts.get(key)
        if not spec:
            continue
        vparts = spec.get("video_parts") or spec.get("parts")
        if vparts:
            entry["video_parts"] = vparts


def build_all_timelines(
    segments: List[str],
    speech_durations: List[float],
    video_durations: List[float] | None = None,
    source_dir: Path = SOURCE_MEDIA_DIR,
    *,
    wav_paths: List[Path] | None = None,
) -> TimelineStore:
    if len(segments) != len(speech_durations):
        raise ValueError("segments and speech_durations length mismatch")
    store: TimelineStore = {}
    for i, (text, speech) in enumerate(zip(segments, speech_durations), start=1):
        wav = wav_paths[i - 1] if wav_paths and i - 1 < len(wav_paths) else None
        vid = None
        if video_durations and i - 1 < len(video_durations):
            vid = float(video_durations[i - 1])
        store[str(i)] = build_segment_timeline(
            i,
            text,
            float(speech),
            video_duration=vid,
            wav_path=wav,
        )
    _attach_clip_parts(store, source_dir, video_durations=video_durations)
    return store


def save_timelines(store: TimelineStore, path: Path | None = None) -> Path:
    dest = path or SEGMENT_TIMELINE_JSON
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dest


def load_timelines(
    path: Path | None = None,
    *,
    source_dir: Path | None = None,
) -> TimelineStore:
    if path is None:
        if source_dir is not None and source_dir != SOURCE_MEDIA_DIR:
            dest = source_dir / "segment_timeline.json"
        else:
            dest = SEGMENT_TIMELINE_JSON
    else:
        dest = path
    if not dest.is_file():
        return {}
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
        return {str(k): v for k, v in data.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _timeline_entry_text(entry: dict) -> str:
    return str(entry.get("text") or entry.get("full_text") or "").strip()


def timelines_matching_sentences(
    store: TimelineStore,
    sentences: List[str],
) -> TimelineStore:
    """仅保留与当前口播段文本一致的 timeline 条目。"""
    if not store or not sentences:
        return {}
    from videoaudiotext.text.llm_split import _collapse_ws

    matched: TimelineStore = {}
    for i, sentence in enumerate(sentences, start=1):
        entry = store.get(str(i))
        if not entry:
            continue
        stored = _timeline_entry_text(entry)
        current = str(sentence).strip()
        if stored and _collapse_ws(stored) != _collapse_ws(current):
            continue
        matched[str(i)] = entry
    return matched


def timeline_raw_cues(
    segment: dict,
    global_offset: float = 0.0,
    *,
    clip_duration: float | None = None,
    speech_duration: float | None = None,
) -> List[tuple[str, float, float]]:
    """从 Timeline 生成 (text, start, end) 字幕 raw 分步。"""
    parts = segment.get("timeline_parts") or segment.get("parts") or []
    scale = 1.0
    if clip_duration and speech_duration and speech_duration > 0:
        if clip_duration < speech_duration * 0.95:
            scale = clip_duration / speech_duration

    cues: List[tuple[str, float, float]] = []
    for part in parts:
        text = str(part.get("text") or "").strip()
        if not text:
            continue
        s = global_offset + float(
            part.get("speech_start") or part.get("start_time") or 0.0
        ) * scale
        e = global_offset + float(
            part.get("speech_end") or part.get("end_time") or 0.0
        ) * scale
        cues.append((text, s, e))
    return cues
