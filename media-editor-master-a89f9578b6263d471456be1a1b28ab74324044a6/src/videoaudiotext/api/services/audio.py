"""Stage 2: fetch per-segment audio URLs, master wav, subtitles (revisioned)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from videoaudiotext.api.errors import bad_request, conflict, process_failed
from videoaudiotext.api.services import audio_revisions as rev
from videoaudiotext.api.url_fetch import fetch_url_to_path
from videoaudiotext.api.workspace.digests import (
    audio_preview_digest,
    text_digest_from_segments,
)
from videoaudiotext.api.workspace.context import workspace_paths
from videoaudiotext.api.workspace.store import WorkspaceStore
from videoaudiotext.audio.gaps import compute_sentence_gaps, segment_video_durations
from videoaudiotext.core.compose import build_master_audio
from videoaudiotext.subtitle.export import build_segment_subtitles
from videoaudiotext.tts.audio import get_audio_duration_seconds, measure_wav_durations


def _normalize_audio(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-ar",
        "44100",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise process_failed(
            "audio normalize failed",
            stderr=(proc.stderr or "")[-500:],
        )


def _clear_dir_files(dir_path: Path, pattern: str = "*") -> None:
    if not dir_path.is_dir():
        return
    for p in dir_path.glob(pattern):
        if p.is_file():
            p.unlink()


def _audio_process_scratch(store: WorkspaceStore) -> Path:
    return store.root / "intermediate" / "audio_process" / "_scratch"


def _cleanup_audio_process_scratch(scratch: Path) -> None:
    if scratch.is_dir():
        shutil.rmtree(scratch, ignore_errors=True)


def _clear_audio_raw_uploads(store: WorkspaceStore) -> None:
    uploads = store.root / "uploads"
    if not uploads.is_dir():
        return
    for p in uploads.glob("audio_raw_*"):
        p.unlink(missing_ok=True)


def cleanup_audio_process_ephemeral(store: WorkspaceStore) -> tuple[bool, int]:
    """Remove in-flight audio/process scratch and raw upload temp files."""
    scratch = _audio_process_scratch(store)
    cleared = False
    if scratch.is_dir():
        _cleanup_audio_process_scratch(scratch)
        cleared = True
    uploads = store.root / "uploads"
    raw_removed = 0
    if uploads.is_dir():
        for p in uploads.glob("audio_raw_*"):
            p.unlink(missing_ok=True)
            raw_removed += 1
    return cleared, raw_removed


def _commit_to_revision_dirs(scratch: Path, audio_dir: Path, subtitles_dir: Path) -> None:
    scratch_audio = scratch / "audio"
    scratch_subtitles = scratch / "subtitles"
    audio_dir.mkdir(parents=True, exist_ok=True)
    subtitles_dir.mkdir(parents=True, exist_ok=True)
    _clear_dir_files(audio_dir, "*.wav")
    work = audio_dir / "_work"
    if work.is_dir():
        shutil.rmtree(work, ignore_errors=True)
    _clear_dir_files(subtitles_dir)
    for wav in sorted(scratch_audio.glob("*.wav")):
        shutil.move(str(wav), audio_dir / wav.name)
    scratch_work = scratch_audio / "_work"
    if scratch_work.is_dir():
        shutil.rmtree(scratch_work, ignore_errors=True)
    for sub in sorted(scratch_subtitles.iterdir()):
        if sub.is_file():
            shutil.move(str(sub), subtitles_dir / sub.name)


def _revision_has_master(store: WorkspaceStore, revision_id: str) -> bool:
    return (rev.revision_audio_dir(store, revision_id) / "master.wav").is_file()


def _cached_revision_response(
    store: WorkspaceStore,
    entry: dict[str, Any],
) -> dict[str, Any]:
    rev_id = str(entry["revision_id"])
    meta = rev.load_revision_audio_config(store, rev_id)
    return _assemble_revision_response(
        store,
        rev_id,
        segments=meta.get("segments") or [],
        gaps=meta.get("gaps_sec") or [],
        digest=str(entry.get("audio_config_digest") or meta.get("audio_config_digest") or ""),
        warnings=[],
        cached=True,
    )


def run_audio_process(
    store: WorkspaceStore,
    segments_input: Sequence[dict[str, Any]],
    *,
    force_refresh: bool = False,
    speed: float = 1.0,
    cancel_check: Callable[[], None] | None = None,
    on_progress: Callable[[str, int, str], None] | None = None,
) -> dict[str, Any]:
    plan = store.read_json(store.split_plan_path)
    if not plan:
        raise bad_request(40004, "split_not_ready")

    expected = len(plan.get("segments") or [])
    if len(segments_input) != expected:
        raise bad_request(
            40001,
            "segments count mismatch with split_plan",
            expected_segment_count=expected,
            received=len(segments_input),
        )

    if speed <= 0:
        raise bad_request(40001, "speed must be positive")

    texts: list[str] = []
    for item in segments_input:
        idx = int(item["index"])
        text = str(item["text"]).strip()
        audio = item.get("audio") or {}
        url = str(audio.get("url") or "").strip()
        if not url:
            raise bad_request(40001, f"missing audio.url for index {idx}")
        texts.append(text)

    digest_payload = [
        {
            "index": int(s["index"]),
            "text": str(s["text"]).strip(),
            "audio_url": str((s.get("audio") or {}).get("url") or "").strip(),
        }
        for s in segments_input
    ]
    preview_digest = audio_preview_digest(digest_payload, [], speed=speed)

    if not force_refresh:
        existing = rev.find_revision_by_digest(store, preview_digest)
        if existing and _revision_has_master(store, str(existing["revision_id"])):
            return _cached_revision_response(store, existing)

    store.ensure_tree()
    revision_id = rev.new_revision_id()
    dest_audio = rev.revision_audio_dir(store, revision_id)
    dest_subtitles = rev.revision_subtitles_dir(store, revision_id)

    scratch = _audio_process_scratch(store)
    _cleanup_audio_process_scratch(scratch)
    scratch_audio = scratch / "audio"
    scratch_subtitles = scratch / "subtitles"
    scratch_audio.mkdir(parents=True)
    scratch_subtitles.mkdir(parents=True)

    warnings: list[str] = []
    try:
        with workspace_paths(store):
            wavs: list[Path] = []
            total = len(segments_input)
            for seg_i, item in enumerate(segments_input, start=1):
                if cancel_check:
                    cancel_check()
                idx = int(item["index"])
                url = str((item.get("audio") or {}).get("url") or "").strip()
                if on_progress:
                    pct = min(85, 10 + int(70 * seg_i / max(total, 1)))
                    on_progress(
                        "fetch",
                        pct,
                        f"处理配音 {seg_i}/{total}（index={idx}）…",
                    )
                raw = store.root / "uploads" / f"audio_raw_{idx}"
                fetch_url_to_path(url, raw)
                wav = scratch_audio / f"{idx}.wav"
                _normalize_audio(raw, wav)
                dur = get_audio_duration_seconds(wav)
                if dur < 0.05:
                    raise bad_request(40007, "audio_too_short", index=idx, duration_sec=dur)
                wavs.append(wav)

            if cancel_check:
                cancel_check()
            if on_progress:
                on_progress("merge", 88, "拼接 master.wav…")
            durations = measure_wav_durations(wavs)
            gaps = compute_sentence_gaps(texts, speed=speed)
            master = scratch_audio / "master.wav"
            build_master_audio(wavs, gaps, master, work_dir=scratch_audio / "_work")

            if cancel_check:
                cancel_check()
            if on_progress:
                on_progress("subtitle", 94, "生成字幕…")
            srt = scratch_subtitles / "subtitle.srt"
            build_segment_subtitles(
                texts,
                durations,
                srt,
                gaps=gaps,
                speed=speed,
            )

        _commit_to_revision_dirs(scratch, dest_audio, dest_subtitles)
    except Exception:
        _cleanup_audio_process_scratch(scratch)
        _clear_audio_raw_uploads(store)
        rev._delete_revision_tree(store, revision_id)
        raise
    finally:
        _cleanup_audio_process_scratch(scratch)

    new_text_digest = text_digest_from_segments(texts)
    plan_segments = plan.get("segments") or []
    for i, item in enumerate(segments_input):
        if i < len(plan_segments):
            plan_segments[i]["text"] = str(item["text"]).strip()
    plan["segments"] = plan_segments
    plan["text_digest"] = new_text_digest
    store.write_json(store.split_plan_path, plan)

    clip_durations = segment_video_durations(durations, gaps)
    seg_results = []
    for i, item in enumerate(segments_input):
        idx = int(item["index"])
        seg_results.append(
            {
                "index": idx,
                "text": str(item["text"]).strip(),
                "duration_sec": durations[i],
                "clip_duration_sec": clip_durations[i],
            }
        )

    preview_digest = audio_preview_digest(digest_payload, gaps, speed=speed)
    speech = sum(float(s.get("duration_sec") or 0) for s in seg_results)
    gap_total = sum(float(g) for g in gaps)
    meta = {
        "revision_id": revision_id,
        "label": "",
        "status": "draft",
        "audio_config_digest": preview_digest,
        "segments": seg_results,
        "gaps_sec": gaps,
        "speed": speed,
        "texts": texts,
        "segment_count": len(seg_results),
        "total_speech_sec": speech,
        "total_with_gaps_sec": speech + gap_total,
    }
    rev.revision_meta_path(store, revision_id).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    evicted = rev.register_revision(
        store,
        revision_id=revision_id,
        label="",
        audio_config_digest=preview_digest,
        segment_count=len(seg_results),
        total_speech_sec=speech,
        total_with_gaps_sec=speech + gap_total,
    )

    result = _assemble_revision_response(
        store,
        revision_id,
        segments=seg_results,
        gaps=gaps,
        digest=preview_digest,
        warnings=warnings,
        cached=False,
    )
    if evicted:
        result["evicted_revision_ids"] = evicted
    return result


def _assemble_revision_response(
    store: WorkspaceStore,
    revision_id: str,
    *,
    segments: list[dict[str, Any]],
    gaps: list[float],
    digest: str,
    warnings: list[str],
    cached: bool,
) -> dict[str, Any]:
    speech = sum(float(s.get("duration_sec") or 0) for s in segments)
    gap_total = sum(float(g) for g in gaps)
    return {
        "revision_id": revision_id,
        "status": "draft",
        "audio_config_digest": digest,
        "cached": cached,
        "segment_count": len(segments),
        "segments": segments,
        "gaps_sec": gaps,
        "total_speech_sec": speech,
        "total_with_gaps_sec": speech + gap_total,
        "warnings": warnings,
    }


def list_audio_revisions(store: WorkspaceStore) -> dict[str, Any]:
    return rev.list_audio_revisions(store)


def confirm_audio(
    store: WorkspaceStore,
    *,
    revision_id: str | None = None,
    audio_config_digest: str | None = None,
) -> dict[str, Any]:
    rid = rev.resolve_revision_id(
        store,
        revision_id=revision_id,
        audio_config_digest=audio_config_digest,
    )
    return rev.promote_revision_to_active(store, rid, confirmed=True, invalidate_media=True)


def ensure_audio_confirmed_for_bind(
    store: WorkspaceStore,
    revision_id: str,
    *,
    audio_config_digest: str | None = None,
) -> dict[str, Any]:
    cfg = store.read_json(store.audio_config_path) or {}
    if not cfg.get("confirmed"):
        raise bad_request(40003, "audio_not_confirmed")
    if str(cfg.get("revision_id") or "") != revision_id:
        raise conflict(40901, "revision_id mismatch")
    digest = rev.resolve_audio_config_digest(cfg)
    if audio_config_digest and digest != audio_config_digest:
        raise conflict(40901, "audio_config_digest mismatch")
    if not (store.audio_active_dir / "master.wav").is_file():
        raise bad_request(40004, "audio_not_ready")
    return cfg


def activate_revision_for_bind(
    store: WorkspaceStore,
    revision_id: str,
    *,
    audio_config_digest: str | None = None,
) -> dict[str, Any]:
    rid = rev.resolve_revision_id(
        store,
        revision_id=revision_id,
        audio_config_digest=audio_config_digest,
    )
    index = rev.load_revisions_index(store)
    prev_active = index.get("active_revision_id")
    invalidate = prev_active is not None and prev_active != rid
    return rev.promote_revision_to_active(
        store,
        rid,
        confirmed=True,
        invalidate_media=invalidate,
    )
