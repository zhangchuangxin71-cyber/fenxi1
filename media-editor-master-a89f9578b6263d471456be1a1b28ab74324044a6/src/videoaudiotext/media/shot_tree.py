"""Shot Tree — merge consecutive same-media segments into continuous super-shots."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Sequence

from videoaudiotext.config import (
    comma_multi_material_enabled,
    MEDIA_CHOICES_JSON,
    SHOT_TREE_ENABLED,
    SHOT_TREE_JSON,
    SOURCE_MEDIA_DIR,
)
from videoaudiotext.media.clip import IMAGE_EXTS, load_clip_offsets, load_clip_parts, media_for_sentences


def resolve_canonical_media_paths(
    segment_count: int,
    source_dir: Path = SOURCE_MEDIA_DIR,
    media_paths: List[Path] | None = None,
) -> List[Path]:
    """
    解析每段对应的原始素材路径（用于分镜树分组）。
    优先 media_choices.json 中的选用项，回退 source_media 编号文件。
    """
    if media_paths is None:
        media_paths = media_for_sentences(segment_count, source_dir)
    if not MEDIA_CHOICES_JSON.is_file():
        return list(media_paths)

    try:
        data = json.loads(MEDIA_CHOICES_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return list(media_paths)

    segments = data.get("segments") or []
    if len(segments) != segment_count:
        return list(media_paths)

    by_idx: dict[int, dict] = {}
    for seg in data.get("segments") or []:
        try:
            by_idx[int(seg["index"])] = seg
        except (KeyError, TypeError, ValueError):
            continue

    out: List[Path] = []
    for i in range(1, segment_count + 1):
        seg = by_idx.get(i)
        if seg:
            choice = str(seg.get("choice") or "video")
            item = seg.get(choice) or seg.get("video") or seg.get("image")
            if isinstance(item, dict) and item.get("path"):
                out.append(Path(str(item["path"])))
                continue
        out.append(media_paths[i - 1])
    return out


@dataclass
class SubSegment:
    segment_index: int
    text: str
    speech_duration: float
    video_duration: float
    gap_after: float
    rel_start: float = 0.0
    rel_end: float = 0.0
    wav_index: int = 0


@dataclass
class Shot:
    shot_id: int
    video_path: Path
    kind: str
    shot_start_offset: float
    shot_duration: float
    sub_segments: List[SubSegment] = field(default_factory=list)

    @property
    def segment_indices(self) -> List[int]:
        return [s.segment_index for s in self.sub_segments]


def _media_kind(path: Path) -> str:
    ext = path.suffix.lower()
    return "image" if ext in IMAGE_EXTS else "video"


def _segment_has_multi_parts(segment_index: int, clip_parts: dict) -> bool:
    if not comma_multi_material_enabled():
        return False
    spec = clip_parts.get(str(segment_index)) or {}
    parts = spec.get("parts") or []
    return len(parts) > 1


def _same_media(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _load_visual_inherit_flags(segment_count: int) -> dict[int, bool]:
    flags: dict[int, bool] = {}
    if not MEDIA_CHOICES_JSON.is_file():
        return flags
    try:
        data = json.loads(MEDIA_CHOICES_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return flags
    segments = data.get("segments") or []
    if len(segments) != segment_count:
        return flags
    for seg in data.get("segments") or []:
        try:
            idx = int(seg["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if idx <= segment_count:
            flags[idx] = bool(seg.get("visual_inherit"))
    return flags


def _continues_prior_shot(
    i: int,
    *,
    canonical_paths: Sequence[Path],
    media_paths: Sequence[Path],
    inherit_flags: dict[int, bool],
    clip_parts: dict,
) -> bool:
    """与上一段是否同一连续镜头（同素材或继承顺延）。"""
    seg_idx = i + 1
    if _segment_has_multi_parts(seg_idx, clip_parts) or _segment_has_multi_parts(
        seg_idx - 1, clip_parts
    ):
        return False
    if _same_media(canonical_paths[i], canonical_paths[i - 1]):
        return True
    if inherit_flags.get(seg_idx) and _same_media(
        canonical_paths[i], canonical_paths[i - 1]
    ):
        return True
    if inherit_flags.get(seg_idx) and _same_media(media_paths[i], media_paths[i - 1]):
        return True
    return False


def build_universal_shot_tree(
    segments: Sequence[str],
    segment_durations: Sequence[float],
    speech_durations: Sequence[float],
    gaps: Sequence[float],
    *,
    source_dir: Path = SOURCE_MEDIA_DIR,
    media_paths: List[Path] | None = None,
) -> List[Shot]:
    """
    将线性 N 段按连续相同素材压缩为大分镜（Super-Segment）。
    子句在大分镜内保留相对时间轴，供字幕/音频精准更替。
    """
    n = len(segments)
    if n == 0:
        return []

    if media_paths is None:
        media_paths = media_for_sentences(n, source_dir)
    canonical_paths = resolve_canonical_media_paths(n, source_dir, media_paths)
    inherit_flags = _load_visual_inherit_flags(n)
    offsets = load_clip_offsets(source_dir)
    clip_parts = load_clip_parts(source_dir)

    shot_tree: List[Shot] = []
    accumulated = 0.0

    def _start_shot(seg_idx: int, media: Path, display: Path) -> Shot:
        key = str(seg_idx)
        start = float(offsets.get(key, 0.0))
        return Shot(
            shot_id=len(shot_tree) + 1,
            video_path=display,
            kind=_media_kind(display),
            shot_start_offset=start,
            shot_duration=0.0,
            sub_segments=[],
        )

    current = _start_shot(1, canonical_paths[0], media_paths[0])

    for i in range(n):
        seg_idx = i + 1
        canonical = canonical_paths[i]
        display = media_paths[i]
        if i > 0 and not _continues_prior_shot(
            i,
            canonical_paths=canonical_paths,
            media_paths=media_paths,
            inherit_flags=inherit_flags,
            clip_parts=clip_parts,
        ):
            current.shot_duration = accumulated
            shot_tree.append(current)
            accumulated = 0.0
            current = _start_shot(seg_idx, canonical, display)

        vid_dur = float(segment_durations[i])
        gap = float(gaps[i]) if i < len(gaps) else 0.0
        sub = SubSegment(
            segment_index=seg_idx,
            text=str(segments[i]),
            speech_duration=float(speech_durations[i]),
            video_duration=vid_dur,
            gap_after=gap,
            rel_start=accumulated,
            rel_end=accumulated + vid_dur,
            wav_index=seg_idx,
        )
        current.sub_segments.append(sub)
        accumulated += vid_dur

    current.shot_duration = accumulated
    shot_tree.append(current)

    for shot in shot_tree:
        shot.shot_id = shot_tree.index(shot) + 1

    return shot_tree


def segment_video_durations_from_shots(shots: Sequence[Shot], segment_count: int) -> List[float]:
    out = [0.0] * segment_count
    for shot in shots:
        for sub in shot.sub_segments:
            out[sub.segment_index - 1] = sub.video_duration
    return out


def segment_audio_durations(
    speech_durations: Sequence[float],
    gaps: Sequence[float],
) -> List[float]:
    """字幕/时间轴用：语音 + 段后歇息（不含视频 尾缓冲）。"""
    return [float(s) + float(g) for s, g in zip(speech_durations, gaps)]


def segment_durations_from_measured_shots(
    shots: Sequence[Shot],
    clip_durations: Sequence[float],
    speech_durations: Sequence[float],
    gaps: Sequence[float],
    *,
    xfade_sec: float = 0.0,
) -> List[float]:
    """
    按实测大分镜 clip 时长，在 shot 内按 speech+gap 比例拆回各子句，
    并扣除 xfade 尾帧叠化占位，使字幕轴与最终 concat 成片对齐。
    """
    n = len(speech_durations)
    out = [0.0] * n
    if len(shots) != len(clip_durations):
        raise ValueError("shots and clip_durations length mismatch")

    for shot_idx, shot in enumerate(shots):
        clip_dur = float(clip_durations[shot_idx])
        is_last_shot = shot_idx >= len(shots) - 1
        effective = clip_dur
        if xfade_sec > 0.001 and not is_last_shot:
            effective = max(0.05, clip_dur - xfade_sec)

        weights: List[float] = []
        indices: List[int] = []
        for sub in shot.sub_segments:
            i = sub.segment_index - 1
            indices.append(i)
            weights.append(float(speech_durations[i]) + float(gaps[i]))
        total_w = sum(weights) or 1.0

        for i, w in zip(indices, weights):
            out[i] = effective * (w / total_w)
    return out


def shot_boundary_after_segment(shots: Sequence[Shot], segment_count: int) -> List[bool]:
    """segment i 与 i+1 之间是否为分镜边界（需 xfade）。"""
    seg_to_shot: dict[int, int] = {}
    for shot in shots:
        for sub in shot.sub_segments:
            seg_to_shot[sub.segment_index] = shot.shot_id

    out: List[bool] = []
    for i in range(1, segment_count):
        out.append(seg_to_shot.get(i) != seg_to_shot.get(i + 1))
    return out


def save_shot_tree(shots: Sequence[Shot], path: Path | None = None) -> Path:
    dest = path or SHOT_TREE_JSON

    def _serialize(shot: Shot) -> dict:
        d = asdict(shot)
        d["video_path"] = str(shot.video_path)
        d["segment_indices"] = shot.segment_indices
        return d

    payload = {"shots": [_serialize(s) for s in shots]}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dest


def shot_tree_enabled() -> bool:
    return SHOT_TREE_ENABLED


def format_shot_tree_summary(shots: Sequence[Shot], segment_count: int) -> str:
    merged = segment_count - len(shots)
    lines = [f"分镜树：{segment_count} 段 → {len(shots)} 大分镜（合并 {merged} 处）"]
    for shot in shots:
        idxs = shot.segment_indices
        if len(idxs) == 1:
            lines.append(
                f"  super_{shot.shot_id}: 段{idxs[0]} {shot.shot_duration:.2f}s ← {shot.video_path.name}"
            )
        else:
            span = f"{idxs[0]}-{idxs[-1]}"
            lines.append(
                f"  super_{shot.shot_id}: 段{span}（{len(idxs)}句）"
                f" {shot.shot_duration:.2f}s ← {shot.video_path.name}"
                f" (from {shot.shot_start_offset:.1f}s)"
            )
    return "\n".join(lines)
