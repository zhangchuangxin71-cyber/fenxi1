"""Adaptive crossfade duration from gap, retrieval score, and media continuity."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence

from videoaudiotext.config import CLIP_XFADE_SEC, XFADE_MAX_RATIO


def xfade_adaptive_enabled() -> bool:
    return os.environ.get("XFADE_ADAPTIVE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def xfade_score_hard_cut() -> float:
    explicit = os.environ.get("XFADE_SCORE_HARD_CUT", "").strip()
    if explicit:
        return max(0.0, float(explicit))
    # 与旧 CLIP 检索低分阈值对齐的默认硬切线
    return min(0.22, 0.28)


def xfade_clip_max_ratio() -> float:
    return max(0.05, min(0.5, float(os.environ.get("XFADE_CLIP_MAX_RATIO", "0.2"))))


def xfade_cross_media_sim() -> float:
    """不同素材文件且无 CLIP 相似度时的默认「可叠化」系数 (0–1)。"""
    return max(0.0, min(1.0, float(os.environ.get("XFADE_CROSS_MEDIA_SIM", "0.55"))))


@dataclass(frozen=True)
class XfadeBoundaryHint:
    """段 i 与 i+1 之间的叠化提示。"""

    min_score: float | None = None
    same_media_path: bool = False


def _base_xfade(gap_duration: float, *, at_boundary: bool) -> float:
    if not at_boundary or CLIP_XFADE_SEC <= 0.001:
        return 0.0
    if gap_duration <= 0.001:
        return 0.0
    return min(CLIP_XFADE_SEC, gap_duration * XFADE_MAX_RATIO)


def compute_adaptive_xfade_out(
    gap_duration: float,
    *,
    is_last: bool,
    at_boundary: bool = True,
    speech_duration: float = 0.0,
    hint: XfadeBoundaryHint | None = None,
) -> float:
    """
    自适应叠化时长：
    - 同文件续播（visual inherit）→ 硬切
    - 检索分过低 → 硬切
    - 不同素材 → 按 XFADE_CROSS_MEDIA_SIM 缩放
    - 不超过本段内容时长 × XFADE_CLIP_MAX_RATIO
    """
    if is_last:
        return 0.0
    base = _base_xfade(gap_duration, at_boundary=at_boundary)
    if base <= 0.001:
        return 0.0
    if not xfade_adaptive_enabled():
        content = max(0.0, speech_duration) + max(0.0, gap_duration)
        cap = content * xfade_clip_max_ratio()
        return min(base, cap) if content > 0.001 else base

    h = hint or XfadeBoundaryHint()
    if h.same_media_path:
        return 0.0

    if h.min_score is not None and h.min_score < xfade_score_hard_cut():
        return 0.0

    scaled = base * xfade_cross_media_sim()
    content = max(0.0, speech_duration) + max(0.0, gap_duration)
    if content > 0.001:
        scaled = min(scaled, content * xfade_clip_max_ratio())
    return max(0.0, scaled)


def _chosen_hit(seg: dict) -> dict | None:
    choice = (seg.get("choice") or "video").strip().lower()
    hit = seg.get(choice)
    if isinstance(hit, dict) and hit.get("path"):
        return hit
    rank_key = f"{choice}_rank"
    rank = int(seg.get(rank_key) or 0)
    cands = seg.get(f"{choice}_candidates") or []
    if cands and 0 <= rank < len(cands):
        return cands[rank]
    return None


def segment_score_and_path(seg: dict) -> tuple[float | None, str | None]:
    hit = _chosen_hit(seg)
    if not hit:
        return None, None
    score_raw = hit.get("score")
    score = float(score_raw) if score_raw is not None else None
    path = hit.get("path")
    return score, str(path) if path else None


def xfade_boundary_hints_for_segments(segment_count: int) -> List[XfadeBoundaryHint]:
    """从 media_choices.json 推断段间叠化提示；无数据时返回空提示。"""
    if segment_count <= 1:
        return []
    from videoaudiotext.media.media_choices import load_media_choices

    data = load_media_choices() or {}
    segments = data.get("segments") or []
    if len(segments) < segment_count:
        return [XfadeBoundaryHint()] * (segment_count - 1)

    meta: list[tuple[float | None, str | None]] = []
    for i in range(segment_count):
        seg = segments[i] if i < len(segments) else {}
        meta.append(segment_score_and_path(seg))

    hints: List[XfadeBoundaryHint] = []
    for i in range(segment_count - 1):
        score_a, path_a = meta[i]
        score_b, path_b = meta[i + 1]
        min_score = None
        if score_a is not None and score_b is not None:
            min_score = min(score_a, score_b)
        elif score_a is not None:
            min_score = score_a
        elif score_b is not None:
            min_score = score_b

        same_path = False
        if path_a and path_b:
            try:
                same_path = Path(path_a).resolve() == Path(path_b).resolve()
            except OSError:
                same_path = path_a == path_b

        hints.append(XfadeBoundaryHint(min_score=min_score, same_media_path=same_path))
    return hints


def summarize_adaptive_xfade(timeline: Sequence) -> str:
    """供日志：统计非零叠化段数与硬切段数。"""
    if len(timeline) <= 1:
        return ""
    xfs = [float(getattr(e, "xfade_out", 0.0) or 0.0) for e in timeline[:-1]]
    active = sum(1 for x in xfs if x > 0.001)
    hard = len(xfs) - active
    if not xfade_adaptive_enabled():
        return f"叠化 {active}/{len(xfs)} 处"
    return f"自适应叠化：{active} 处 fade，{hard} 处硬切"
