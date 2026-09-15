"""Stage 5: compose from locked workspace assets."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from videoaudiotext.api.errors import bad_request, process_failed
from videoaudiotext.api.workspace.context import workspace_paths
from videoaudiotext.api.workspace.store import WorkspaceStore
from videoaudiotext.audio.bgm import build_final_audio
from videoaudiotext.audio.gaps import (
    compute_absolute_timeline,
    compute_sentence_gaps,
    master_audio_duration,
    segment_video_durations,
    shot_level_xfade_transitions,
)
from videoaudiotext.audio.xfade import (
    summarize_adaptive_xfade,
    xfade_adaptive_enabled,
    xfade_boundary_hints_for_segments,
)
from videoaudiotext.core.compose import (
    concat_clips,
    concat_timeline_output_with_hard_subtitles,
    embed_subtitles,
    measure_clip_durations,
    predict_xfade_chain_duration,
    write_filelist,
)
from videoaudiotext.core.ffmpeg_util import check_ffmpeg
from videoaudiotext.core.source_media import prepare_existing_source_media
from videoaudiotext.subtitle.export import resolve_embed_subtitle_path
from videoaudiotext.media.clip import build_segment_clips_direct, build_super_clips_direct
from videoaudiotext.media.shot_tree import (
    build_universal_shot_tree,
    save_shot_tree,
    shot_boundary_after_segment,
    shot_tree_enabled,
)
from videoaudiotext.tts.audio import list_sentence_wavs, measure_wav_durations
from videoaudiotext.audio.gaps import xfade_at_boundary_from_shots

LogFn = Optional[Callable[[str], None]]


def _log(log: LogFn, msg: str) -> None:
    if log:
        log(msg)


def run_flow_b_compose(
    store: WorkspaceStore,
    scratch_dir: Path,
    *,
    subtitle_mode: str = "hard",
    reuse_intermediates: bool = True,
    log: LogFn = None,
) -> dict[str, Any]:
    check_ffmpeg()
    store.refresh_flags()
    if not store.flags.audio_confirmed or not store.flags.media_bound:
        raise bad_request(40003, "audio_not_confirmed or media_not_bound")

    plan = store.read_json(store.split_plan_path) or {}
    audio_cfg = store.read_json(store.audio_config_path) or {}
    render = store.read_json(store.render_style_path)

    plan_segments = plan.get("segments") or []
    segment_indices = [int(s["index"]) for s in plan_segments]
    segments = [str(s["text"]) for s in plan_segments]
    if not segments:
        raise bad_request(40004, "split_not_ready")

    gaps = audio_cfg.get("gaps_sec") or compute_sentence_gaps(
        segments,
        speed=float(audio_cfg.get("speed") or 1.0),
    )
    cfg_segments = audio_cfg.get("segments") or []
    wavs = sorted(
        store.audio_active_dir.glob("[0-9]*.wav"),
        key=lambda p: int(p.stem),
    )
    if not wavs:
        wavs = list_sentence_wavs(store.audio_active_dir)
    if len(wavs) == len(segments):
        durations = measure_wav_durations(wavs)
    elif len(cfg_segments) == len(segments):
        durations = [float(s.get("duration_sec") or 0) for s in cfg_segments]
        if any(d <= 0 for d in durations):
            raise bad_request(40004, "audio segment duration missing in config")
    else:
        raise bad_request(
            40004,
            "audio segment count mismatch",
            expected=len(segments),
            wav_count=len(wavs),
            config_count=len(cfg_segments),
        )
    segment_durations = segment_video_durations(durations, gaps)
    if len(cfg_segments) == len(segments):
        for i, seg in enumerate(cfg_segments):
            clip = seg.get("clip_duration_sec")
            if clip is not None and float(clip) > 0:
                segment_durations[i] = float(clip)

    resolution_mode = None
    custom_width = None
    custom_height = None
    subtitle_font_scale = None
    subtitle_font_name = None
    subtitle_y_offset_px = None
    if render:
        res = render.get("resolution") or {}
        rw = res.get("width")
        rh = res.get("height")
        if rw and rh:
            custom_width = int(rw)
            custom_height = int(rh)
            resolution_mode = "custom"
        sty = render.get("subtitle_style") or {}
        subtitle_font_scale = sty.get("font_scale")
        subtitle_font_name = sty.get("font_name")
        from videoaudiotext.api.subtitle_style import resolve_style_y_offset_to_pixels

        subtitle_y_offset_px = resolve_style_y_offset_to_pixels(sty.get("y_offset"))

    from videoaudiotext.config import (
        CLIP_XFADE_SEC,
        CONTENT_SAFE_SUBTITLES,
        MERGE_XFADE_SUBTITLE,
        set_active_output_dimensions,
        set_active_subtitle_font_name,
        set_active_subtitle_font_scale,
        set_active_subtitle_y_offset,
    )

    if render:
        res = render.get("resolution") or {}
        rw = res.get("width")
        rh = res.get("height")
        if rw and rh:
            set_active_output_dimensions(int(rw), int(rh))

    if subtitle_font_scale is not None:
        set_active_subtitle_font_scale(subtitle_font_scale)
    if subtitle_font_name is not None:
        set_active_subtitle_font_name(subtitle_font_name)
    if subtitle_y_offset_px is not None:
        set_active_subtitle_y_offset(subtitle_y_offset_px)

    scratch_dir.mkdir(parents=True, exist_ok=True)

    with workspace_paths(store, scratch_dir=scratch_dir):
        from videoaudiotext.config import CLIP_DIR, NO_SUB_MP4, OUTPUT_MP4, SUBTITLE_ASS

        prepare_existing_source_media(
            len(segments),
            source_dir=store.source_media_dir,
            segment_indices=segment_indices,
            log=log,
        )

        from videoaudiotext.media.clip import media_for_sentences

        media_paths = media_for_sentences(
            len(segments),
            store.source_media_dir,
            segment_indices=segment_indices,
        )

        shots = build_universal_shot_tree(
            segments,
            segment_durations,
            durations,
            gaps,
            media_paths=media_paths,
        )
        save_shot_tree(shots)
        shot_boundaries = shot_boundary_after_segment(shots, len(segments))
        use_shot_tree = shot_tree_enabled() and len(shots) < len(segments)
        if use_shot_tree:
            xfade_at = xfade_at_boundary_from_shots(shot_boundaries, len(segments))
        else:
            xfade_at = [True] * max(0, len(segments) - 1)

        xfade_hints = (
            xfade_boundary_hints_for_segments(len(segments))
            if xfade_adaptive_enabled()
            else None
        )
        timeline = compute_absolute_timeline(
            segments,
            durations,
            xfade_at_boundary=xfade_at,
            xfade_boundary_hints=xfade_hints,
        )
        if xfade_adaptive_enabled() and len(segments) > 1:
            _log(log, summarize_adaptive_xfade(timeline))

        if use_shot_tree:
            clips = build_super_clips_direct(
                shots,
                timeline,
                CLIP_DIR,
                resolution_mode=resolution_mode,
                custom_width=custom_width,
                custom_height=custom_height,
                reuse_existing=reuse_intermediates,
                log=log,
            )
            concat_xfade_secs = shot_level_xfade_transitions(timeline, shots)
        else:
            clips = build_segment_clips_direct(
                segment_durations,
                store.source_media_dir,
                timeline,
                CLIP_DIR,
                speech_durations=durations,
                resolution_mode=resolution_mode,
                custom_width=custom_width,
                custom_height=custom_height,
                reuse_existing=reuse_intermediates,
                log=log,
            )
            concat_xfade_secs = None

        master_audio_path = store.audio_active_dir / "master.wav"
        final_audio_path = scratch_dir / "master_final.wav"
        voice_volume = 1.0
        bgm_volume = 0.0
        bgm_path: Path | str | None = None
        if render:
            audio_cfg = render.get("audio") or {}
            voice_volume = float(audio_cfg.get("voice_volume") or 1.0)
            bgm_cfg = render.get("bgm") or {}
            if isinstance(bgm_cfg, dict) and bgm_cfg.get("local_path"):
                rel = str(bgm_cfg["local_path"]).strip()
                candidate = store.root / rel
                if candidate.is_file():
                    bgm_path = candidate
                    bgm_volume = float(bgm_cfg.get("bgm_volume") or 0.0)
                    _log(
                        log,
                        f"BGM 混音 voice={voice_volume:.2f} bgm={bgm_volume:.2f} "
                        f"ducking={'on' if bgm_volume > 0.001 else 'off'}",
                    )
            elif abs(voice_volume - 1.0) > 0.001:
                _log(log, f"口播音量倍率 voice={voice_volume:.2f}")
        build_final_audio(
            master_audio_path,
            final_audio_path,
            bgm_path=bgm_path,
            voice_volume=voice_volume,
            bgm_volume=bgm_volume,
            duration=master_audio_duration(timeline),
        )

        srt_path = store.subtitles_active_dir / "subtitle.srt"
        if not srt_path.is_file():
            raise process_failed("subtitle file missing for compose")

        from videoaudiotext.subtitle.export import build_display_ass_for_compose

        build_display_ass_for_compose(
            srt_path,
            SUBTITLE_ASS,
            segments=segments,
            durations=durations,
            gaps=gaps,
            wav_paths=wavs if len(wavs) == len(segments) else None,
            media_paths=media_paths,
        )
        sub_for_embed = SUBTITLE_ASS if SUBTITLE_ASS.is_file() else resolve_embed_subtitle_path(None)
        if not sub_for_embed.is_file():
            raise process_failed("subtitle ASS missing after compose rebake")

        if CLIP_XFADE_SEC > 0.001 and len(clips) > 1:
            label = "大分镜" if use_shot_tree else "段"
            _log(
                log,
                f"画面叠化 xfade（音轨硬切）{CLIP_XFADE_SEC:.2f}s（{len(clips)} {label}）",
            )

        use_merged_hard = (
            subtitle_mode == "hard"
            and MERGE_XFADE_SUBTITLE
            and clips
            and final_audio_path.is_file()
        )
        store.deliverables_dir.mkdir(parents=True, exist_ok=True)
        if use_merged_hard:
            concat_timeline_output_with_hard_subtitles(
                clips,
                final_audio_path,
                sub_for_embed,
                OUTPUT_MP4,
                xfade_secs=concat_xfade_secs,
                timeline=timeline,
                shots=shots if use_shot_tree else None,
            )
        else:
            write_filelist(clips)
            concat_clips(
                out=NO_SUB_MP4,
                clips=clips,
                master_audio=final_audio_path,
                timeline=timeline,
                xfade_secs=concat_xfade_secs,
                shots=shots if use_shot_tree else None,
            )
            embed_subtitles(NO_SUB_MP4, sub_for_embed, OUTPUT_MP4, mode=subtitle_mode)

    total = master_audio_duration(timeline)
    if store.output_mp4.is_file():
        from videoaudiotext.subtitle.export import _video_duration_seconds

        total = _video_duration_seconds(store.output_mp4) or total

    store.flags.compose_ready = True
    return {
        "total_seconds": total,
        "durations_sec": durations,
        "gaps_sec": gaps,
        "intermediates_cleaned": True,
    }
