"""Per-segment visual retrieval metadata from LLM split."""

from __future__ import annotations

import re
from dataclasses import dataclass

_VISUAL_COMMENT_RE = re.compile(
    r"^//\s*(?:(?:\[\d+\]|\d+\.)\s*)?visual=",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SegmentVisualMeta:
    """LLM 对成片单元是否适合 CLIP 画面检索的判定。"""

    visual: bool
    search_query: str | None = None

    def normalized_query(self) -> str | None:
        q = (self.search_query or "").strip()
        if not q:
            return None
        return q[:48]


def format_visual_meta_line(meta: SegmentVisualMeta | None, *, index: int | None = None) -> str:
    """单行 visual 标定，用于 preview_split / 日志。"""
    if meta is None:
        body = "visual=— | search_query: —"
    else:
        flag = "true" if meta.visual else "false"
        query = meta.normalized_query() or "—"
        body = f"visual={flag} | search_query: {query}"
    if index is not None:
        return f"{index}. {body}"
    return body


def format_visual_meta_comment(meta: SegmentVisualMeta | None, *, index: int | None = None) -> str:
    """分句编辑区注释行（解析时会跳过）。"""
    line = format_visual_meta_line(meta, index=index)
    return f"// {line}"


def is_visual_meta_comment_line(line: str) -> bool:
    return bool(_VISUAL_COMMENT_RE.match((line or "").strip()))
