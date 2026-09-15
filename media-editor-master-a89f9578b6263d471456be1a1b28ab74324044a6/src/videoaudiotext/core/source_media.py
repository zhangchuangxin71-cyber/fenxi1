"""Numbered source_media validation for Flow B compose."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional, Sequence

from videoaudiotext.config import SOURCE_MEDIA_DIR
from videoaudiotext.media.clip import (
    create_numbered_placeholders,
    list_source_media,
    media_for_sentences,
    pad_missing_numbered_media,
)

LogFn = Optional[Callable[[str], None]]


def _log(fn: LogFn, msg: str) -> None:
    if fn:
        fn(msg)


def ensure_source_media(
    sentence_count: int,
    *,
    source_dir: Path = SOURCE_MEDIA_DIR,
    segment_indices: Sequence[int] | None = None,
    log: LogFn = None,
) -> List[Path]:
    """Ensure numbered media exist under source_dir (pad or placeholders)."""
    source_dir.mkdir(parents=True, exist_ok=True)
    indices = (
        [int(i) for i in segment_indices]
        if segment_indices is not None
        else list(range(1, sentence_count + 1))
    )
    pool = list_source_media(source_dir)
    by_idx = {
        int(p.stem): p for p in pool if p.stem.isdigit()
    }
    if all(i in by_idx for i in indices):
        assigned = media_for_sentences(
            sentence_count,
            source_dir,
            segment_indices=indices,
        )
        for i, p in zip(indices, assigned):
            _log(log, f"  素材 index={i} ← {p.name}")
        return assigned

    if len(pool) >= sentence_count and segment_indices is None:
        assigned = media_for_sentences(sentence_count, source_dir)
        for i, p in enumerate(assigned, 1):
            _log(log, f"  素材 {i} ← {p.name}")
        return assigned

    padded = pad_missing_numbered_media(sentence_count, source_dir)
    if padded >= sentence_count and segment_indices is None:
        _log(
            log,
            f"  素材不足 {sentence_count} 个，已复制末条补齐至 {sentence_count} 个编号文件",
        )
        assigned = media_for_sentences(sentence_count, source_dir)
        for i, p in enumerate(assigned, 1):
            _log(log, f"  素材 {i} ← {p.name}")
        return assigned

    if len(pool) == 0:
        _log(log, f"未找到素材，为 {sentence_count} 句话生成占位图 1.jpg …")
        create_numbered_placeholders(sentence_count, source_dir)
        return media_for_sentences(sentence_count, source_dir)

    raise ValueError(
        f"素材 {len(pool)} 个，分句 {sentence_count} 句：每句话需要 1 个素材，"
        f"请补足到 {sentence_count} 个（建议命名为 1.mp4、2.jpg …）"
    )


def prepare_existing_source_media(
    sentence_count: int,
    *,
    source_dir: Path = SOURCE_MEDIA_DIR,
    segment_indices: Sequence[int] | None = None,
    log: LogFn = None,
) -> List[Path]:
    """Flow B: media/bind 已写入 source_media，仅校验/补齐编号文件。"""
    return ensure_source_media(
        sentence_count,
        source_dir=source_dir,
        segment_indices=segment_indices,
        log=log,
    )
