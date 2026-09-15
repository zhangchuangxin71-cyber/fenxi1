"""Pipeline manifest — unified segment_timeline.json generation."""

from __future__ import annotations

from pathlib import Path
from typing import List

from videoaudiotext.config import SEGMENT_TIMELINE_JSON, SOURCE_MEDIA_DIR
from videoaudiotext.audio.timeline import TimelineStore, build_all_timelines, save_timelines


def generate_pipeline_manifest(
    segments: List[str],
    speech_durations: List[float],
    video_durations: List[float] | None = None,
    *,
    source_dir: Path = SOURCE_MEDIA_DIR,
    output_path: Path | None = None,
    wav_paths: List[Path] | None = None,
) -> TimelineStore:
    """
    TTS 探测完成后一次性编织整条片子的时间轴，写入 segment_timeline.json。
    画面裁切与字幕烧录共用此唯一事实源。
    """
    store = build_all_timelines(
        segments,
        speech_durations,
        video_durations,
        source_dir,
        wav_paths=wav_paths,
    )
    save_timelines(store, output_path or SEGMENT_TIMELINE_JSON)
    return store
