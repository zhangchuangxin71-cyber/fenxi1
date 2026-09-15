"""Patch global config paths to a workspace (and optional compose scratch)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from videoaudiotext.api.workspace.store import WorkspaceStore

_PATH_ATTRS = (
    "AUDIO_DIR",
    "SOURCE_MEDIA_DIR",
    "CLIP_DIR",
    "TEMP_DIR",
    "SUBTITLE_SRT",
    "SUBTITLE_ASS",
    "NO_SUB_MP4",
    "OUTPUT_MP4",
    "COVER_JPG",
    "FILELIST",
    "CLIP_OFFSETS_JSON",
    "CLIP_PARTS_JSON",
    "SEGMENT_TIMELINE_JSON",
    "SHOT_TREE_JSON",
    "MEDIA_CHOICES_JSON",
    "PIPELINE_PLAN_JSON",
)


@contextmanager
def workspace_paths(
    store: WorkspaceStore,
    *,
    scratch_dir: Path | None = None,
) -> Iterator[None]:
    import videoaudiotext.config as cfg

    scratch = scratch_dir or (store.root / "intermediate" / "_scratch")
    scratch.mkdir(parents=True, exist_ok=True)
    clip_dir = scratch / "clip"
    clip_dir.mkdir(parents=True, exist_ok=True)

    new_values = {
        "AUDIO_DIR": store.audio_active_dir,
        "SOURCE_MEDIA_DIR": store.source_media_dir,
        "CLIP_DIR": clip_dir,
        "TEMP_DIR": scratch / "staging",
        "SUBTITLE_SRT": store.subtitles_active_dir / "subtitle.srt",
        "SUBTITLE_ASS": store.subtitles_active_dir / "subtitle.ass",
        "NO_SUB_MP4": scratch / "no_sub.mp4",
        "OUTPUT_MP4": store.output_mp4,
        "COVER_JPG": scratch / "cover.jpg",
        "FILELIST": scratch / "filelist.txt",
        "CLIP_OFFSETS_JSON": store.source_media_dir / "clip_offsets.json",
        "CLIP_PARTS_JSON": store.source_media_dir / "clip_parts.json",
        "SEGMENT_TIMELINE_JSON": store.source_media_dir / "segment_timeline.json",
        "SHOT_TREE_JSON": store.source_media_dir / "shot_tree.json",
        "MEDIA_CHOICES_JSON": store.source_media_dir / "media_choices.json",
        "PIPELINE_PLAN_JSON": store.config_dir / "split_plan.json",
    }
    old = {name: getattr(cfg, name) for name in _PATH_ATTRS}
    try:
        for name, value in new_values.items():
            setattr(cfg, name, value)
        (cfg.TEMP_DIR).mkdir(parents=True, exist_ok=True)
        yield
    finally:
        for name, val in old.items():
            setattr(cfg, name, val)
