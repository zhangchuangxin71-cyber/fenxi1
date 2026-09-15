"""Stage 1: split + optional AI prompts."""

from __future__ import annotations

from typing import Any, Callable

from videoaudiotext.api.errors import bad_request, conflict, process_failed
from videoaudiotext.api.services.audio_revisions import clear_all_audio_revisions
from videoaudiotext.api.workspace.digests import text_digest_from_segments
from videoaudiotext.api.workspace.store import WorkspaceStore
from videoaudiotext.text.llm_prompts import (
    generate_image_video_prompts_parallel,
    pick_default_global_style,
)
from videoaudiotext.text.segment_meta import SegmentVisualMeta
from videoaudiotext.text.segments import estimate_duration
from videoaudiotext.text.split import normalize_script_text, split_sentence_with_outcome


def _segment_dict(
    index: int,
    text: str,
    meta: SegmentVisualMeta | None,
    ai_prompts: dict | None = None,
) -> dict[str, Any]:
    sq = (meta.search_query if meta else "") or ""
    item: dict[str, Any] = {
        "index": index,
        "text": text,
        "search_query": sq,
        "visual": True,
    }
    if ai_prompts:
        item["ai_prompts"] = ai_prompts
    return item


def _invalidate_downstream(store: WorkspaceStore, *, clear_split_adjacent: bool = True) -> None:
    if clear_split_adjacent:
        for path in (
            store.media_binding_path,
            store.render_style_path,
        ):
            if path.is_file():
                path.unlink()
        clear_all_audio_revisions(store)
        if store.source_media_dir.is_dir():
            for p in store.source_media_dir.glob("*"):
                if p.is_file():
                    p.unlink()
        if store.previews_dir.is_dir():
            for p in store.previews_dir.glob("*"):
                if p.is_file():
                    p.unlink()
    store.flags.audio_ready = False
    store.flags.audio_confirmed = False
    store.flags.media_bound = False
    store.flags.visual_preview_ready = False
    store.flags.compose_ready = False
    store.audio_preview_digest = None
    store.audio_confirmed_digest = None


def _split_mode_used_label(outcome) -> str:
    if outcome.mode == "llm" and not outcome.fallback:
        return "llm"
    if outcome.fallback and outcome.llm_requested:
        return "rule_fallback"
    return "rule"


def run_split(
    store: WorkspaceStore,
    text: str,
    *,
    include_ai_prompts: bool = False,
    global_style: str | None = None,
    cancel_check: Callable[[], None] | None = None,
    on_progress: Callable[[str, int, str], None] | None = None,
) -> dict[str, Any]:
    text = normalize_script_text(text or "")
    if not text:
        raise bad_request(40001, "text is required")

    if cancel_check:
        cancel_check()
    if on_progress:
        on_progress("split", 10, "LLM 分句中…")

    outcome = split_sentence_with_outcome(text)

    if cancel_check:
        cancel_check()
    segments = outcome.segments
    visual_meta = outcome.segment_visual_meta

    warnings: list[str] = []
    if outcome.fallback and outcome.reason:
        warnings.append(f"split fallback: {outcome.reason}")

    ai_global = None
    seg_payloads: list[dict[str, Any]] = []
    split_mode_used = _split_mode_used_label(outcome)
    if include_ai_prompts:
        if on_progress:
            on_progress("prompts", 40, "生成 AI 提示词…")
        if cancel_check:
            cancel_check()
        try:
            global_style = (global_style or "").strip() or pick_default_global_style()
            search_queries = [
                (
                    visual_meta[i].search_query
                    if visual_meta and i < len(visual_meta) and visual_meta[i]
                    else None
                )
                for i in range(len(segments))
            ]
            durations = [estimate_duration(s) for s in segments]
            batch, video_batch = generate_image_video_prompts_parallel(
                segments,
                search_queries=search_queries,
                global_style=global_style,
                durations_sec=durations,
            )
            ai_global = {
                "global_style": global_style,
                "video_global_negative_prompt": batch.global_negative_prompt,
                "video_film_look": batch.film_look,
            }
            for i, seg_text in enumerate(segments, start=1):
                ip = batch.prompts[i - 1]
                vp = video_batch.prompts[i - 1]
                prompts = {
                    "image": {
                        "positive_prompt": ip.positive_prompt,
                        "negative_prompt": ip.negative_prompt,
                    },
                    "video": {
                        "positive_prompt": vp.positive_prompt,
                        "negative_prompt": vp.negative_prompt,
                    },
                }
                meta = (
                    visual_meta[i - 1]
                    if visual_meta and i - 1 < len(visual_meta)
                    else None
                )
                seg_payloads.append(_segment_dict(i, seg_text, meta, prompts))
        except Exception as exc:
            raise process_failed(
                "ai_prompt_generation_failed",
                reason=str(exc)[:500],
            ) from exc
    else:
        for i, seg_text in enumerate(segments, start=1):
            meta = visual_meta[i - 1] if visual_meta and i - 1 < len(visual_meta) else None
            seg_payloads.append(_segment_dict(i, seg_text, meta))

    text_digest = text_digest_from_segments(segments)
    plan = {
        "text_digest": text_digest,
        "segments": [
            {
                "index": s["index"],
                "text": s["text"],
                "search_query": s["search_query"],
                "visual": True,
            }
            for s in seg_payloads
        ],
        "split_mode_used": split_mode_used,
    }
    if cancel_check:
        cancel_check()
    _invalidate_downstream(store)
    store.write_json(store.split_plan_path, plan)
    store.flags.split_ready = True

    data: dict[str, Any] = {
        "text_digest": text_digest,
        "split_mode_used": split_mode_used,
        "segment_count": len(seg_payloads),
        "segments": seg_payloads,
        "warnings": warnings,
    }
    if ai_global:
        data["ai_prompts_global"] = ai_global
    return data

