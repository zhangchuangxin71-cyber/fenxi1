"""Step 1: Outer split — rule punctuation or LLM semantic split."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import List, Literal

from videoaudiotext.text.segment_meta import SegmentVisualMeta

# 外层分片：仅 。！？； 切段（及英文 .!?;）。逗号、顿号不切，留给字幕内折行。
OUTER_SPLIT = "。！？；.!?;"
_CLAUSE_END = "，、；："


def _merge_soft_wraps(paragraph: str) -> str:
    """段内单个换行视为排版折行，直接去掉。"""
    return re.sub(r"[\r\n]+", "", paragraph).strip()


def _join_paragraphs(left: str, right: str) -> str:
    """空行分段后的拼接：必要时补句末标点，避免跨段粘连。"""
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    last = left[-1]
    if last in OUTER_SPLIT:
        return left + right
    if last in _CLAUSE_END:
        return left[:-1].rstrip() + "。" + right
    return left + "。" + right


def normalize_script_text(text: str) -> str:
    """口播输入规范化：段内换行合并；空行分段并在必要时补句号。"""
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = re.split(r"\n{2,}", normalized)
    merged = _merge_soft_wraps(paragraphs[0])
    for paragraph in paragraphs[1:]:
        part = _merge_soft_wraps(paragraph)
        if part:
            merged = _join_paragraphs(merged, part)
    return merged.strip()

# 同进程内 LLM 分句缓存，避免 preview / 检索 / TTS 多次调用结果不一致
# Key: (原文, prompt_hash, model, api_base) — Prompt/模型变更后自动失效
_LLM_SPLIT_CACHE: dict[
    tuple[str, str, str, str],
    tuple[List[str], List[int], List[SegmentVisualMeta | None]],
] = {}

_LAST_SPLIT_OUTCOME: "SplitOutcome | None" = None


@dataclass(frozen=True)
class SplitOutcome:
    segments: list[str]
    mode: Literal["llm", "rule"]
    llm_requested: bool
    fallback: bool
    reason: str | None = None
    segment_groups: list[int] | None = None
    segment_visual_meta: list[SegmentVisualMeta | None] | None = None


def get_last_split_outcome() -> SplitOutcome | None:
    return _LAST_SPLIT_OUTCOME


def _truncate_reason(reason: str, *, max_len: int = 160) -> str:
    text = " ".join((reason or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def split_outcome_label(outcome: SplitOutcome | None) -> str:
    from videoaudiotext.config import LLM_MODEL, text_split_mode_label

    if outcome is None:
        return text_split_mode_label()
    if outcome.mode == "llm" and not outcome.fallback:
        return f"LLM 语义分句（{LLM_MODEL}）"
    if outcome.fallback and outcome.llm_requested:
        return "规则分句（。！？；）· LLM 已回退"
    return text_split_mode_label()


def split_outcome_notice(outcome: SplitOutcome | None, *, markdown: bool = True) -> str:
    """LLM 请求失败回退规则分句时的用户可见说明。"""
    if outcome is None or not outcome.fallback:
        return ""
    reason = _truncate_reason(outcome.reason or "未知原因")
    if markdown:
        return (
            "⚠️ **已回退规则分句**：本应按 LLM 语义拆分，但因调用/校验失败，"
            "已改用 **仅在 。！？； 处切开** 的标点分句。"
            f" 原因：{reason}"
        )
    return (
        "⚠️ 已回退规则分句：LLM 语义拆分失败，已改用仅在 。！？； 处切开的标点分句。"
        f" 原因：{reason}"
    )


def clear_split_cache() -> None:
    _LLM_SPLIT_CACHE.clear()


def _split_cache_key(text: str) -> tuple[str, str, str, str]:
    from videoaudiotext.config import LLM_API_BASE, LLM_MODEL
    from videoaudiotext.text.llm_split import split_prompt_version_hash

    return (text, split_prompt_version_hash(), LLM_MODEL, LLM_API_BASE)


def split_sentence_rule(text: str) -> List[str]:
    """
    规则口播外层分片：
    - 仅在 。！？； 处切开，标点保留在本段末尾
    - ，、 不切，避免 TTS/画面/字幕条目碎片化
    """
    text = text.strip()
    if not text:
        return []

    segments: List[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in OUTER_SPLIT:
            s = buf.strip()
            if s:
                segments.append(s)
            buf = ""
    if buf.strip():
        segments.append(buf.strip())
    return segments


def split_sentence_with_outcome(text: str, *, use_cache: bool = True) -> SplitOutcome:
    """成片单元外层分句，并记录本次实际使用的分句模式（含 LLM 回退）。"""
    global _LAST_SPLIT_OUTCOME

    text = normalize_script_text(text)
    if not text:
        outcome = SplitOutcome(
            [], "rule", False, False, segment_groups=[], segment_visual_meta=[]
        )
        _LAST_SPLIT_OUTCOME = outcome
        return outcome

    from videoaudiotext.config import llm_split_enabled

    llm_requested = llm_split_enabled()
    if llm_requested:
        cache_key = _split_cache_key(text)
        if use_cache and cache_key in _LLM_SPLIT_CACHE:
            segments, groups, visual_meta = _LLM_SPLIT_CACHE[cache_key]
            outcome = SplitOutcome(
                list(segments),
                "llm",
                True,
                False,
                segment_groups=list(groups),
                segment_visual_meta=list(visual_meta),
            )
            _LAST_SPLIT_OUTCOME = outcome
            return outcome
        from videoaudiotext.text.llm_split import split_sentence_llm_with_groups

        try:
            segments, groups, visual_meta = split_sentence_llm_with_groups(text)
            if use_cache:
                _LLM_SPLIT_CACHE[cache_key] = (
                    list(segments),
                    list(groups),
                    list(visual_meta),
                )
            outcome = SplitOutcome(
                segments,
                "llm",
                True,
                False,
                segment_groups=groups,
                segment_visual_meta=visual_meta,
            )
            _LAST_SPLIT_OUTCOME = outcome
            return outcome
        except Exception as exc:
            print(f"[llm_split] 语义分句失败，回退规则分句: {exc}", file=sys.stderr)
            segments = split_sentence_rule(text)
            groups = list(range(len(segments)))
            outcome = SplitOutcome(
                segments,
                "rule",
                True,
                True,
                str(exc),
                segment_groups=groups,
                segment_visual_meta=None,
            )
            _LAST_SPLIT_OUTCOME = outcome
            return outcome

    segments = split_sentence_rule(text)
    groups = list(range(len(segments)))
    outcome = SplitOutcome(
        segments,
        "rule",
        False,
        False,
        segment_groups=groups,
        segment_visual_meta=None,
    )
    _LAST_SPLIT_OUTCOME = outcome
    return outcome


def split_sentence(text: str, *, use_cache: bool = True) -> List[str]:
    """成片单元外层分句：优先 LLM 语义拆分，失败或未配置时回退规则分句。"""
    return split_sentence_with_outcome(text, use_cache=use_cache).segments


def _trim_short_trailing_comma(text: str, *, max_len: int | None = None) -> str:
    """极短子句去掉末尾「，」，TTS/字幕更干净（如「你瞧啊，」→「你瞧啊」）。"""
    t = text.strip()
    if not t or not t.endswith("，"):
        return t
    if max_len is None:
        from videoaudiotext.config import pre_split_short_comma_trim_chars

        max_len = pre_split_short_comma_trim_chars()
    if len(t) <= max_len:
        return t[:-1].strip()
    return t


def _is_emotional_short_whole(seg: str) -> bool:
    from videoaudiotext.config import pre_split_emotional_keep_max_chars

    t = seg.strip()
    if not t:
        return False
    return len(t) <= pre_split_emotional_keep_max_chars() and t.endswith(
        ("！", "?", "!", "？")
    )


def _collapse_flash_tails(parts: List[str]) -> List[str]:
    """切分尾句过短（如「太棒啦！」）并回上一段，避免 <1s 闪条分镜。"""
    from videoaudiotext.config import pre_split_tail_merge_max_chars

    max_tail = pre_split_tail_merge_max_chars()
    out = list(parts)
    while len(out) >= 2:
        last = out[-1].strip()
        if len(last) <= max_tail and last.endswith(("！", "?", "!", "？")):
            out[-2] = out[-2] + last
            out.pop()
        else:
            break
    return out


_SPLIT_PUNCT_CHARS = ("，", "、")


def _is_unpunctuated_clause(text: str) -> bool:
    """子句内无逗号/顿号 → 可走无标点容差放行。"""
    return "，" not in text and "、" not in text


def _strip_split_punct(text: str) -> str:
    return text.replace("，", "").replace("、", "")


def _try_absorb_incoming_flash(new_segments: List[str], seg: str) -> bool:
    """强情绪极短尾句（≤4 字且 ！？）并入上一段，防闪条。"""
    from videoaudiotext.config import pre_split_flash_incoming_max_chars

    t = seg.strip()
    if not t or not new_segments:
        return False
    if len(t) <= pre_split_flash_incoming_max_chars() and t.endswith(
        ("！", "?", "!", "？")
    ):
        new_segments[-1] = new_segments[-1] + t
        return True
    return False


def _clean_split_remainder(text: str) -> str:
    """切分后剔除右半句开头的顿号/空白。"""
    return text.strip().lstrip("、")


def _score_word_boundary_split(text: str, end: int) -> float:
    """词边界切分动态得分：对称性 + 距几何中心 + 单字 token 挂头/挂尾惩罚。"""
    from videoaudiotext.config import pre_split_min_part_chars, pre_split_score_base

    left, right = text[:end].strip(), text[end:].strip()
    if not left or not right:
        return -1e9
    p1_len, p2_len = len(left), len(right)
    min_part = pre_split_min_part_chars()
    # 词边界切分：允许恰好 min_part 字（如「人间烦恼」4 字主语）
    if p1_len < min_part or p2_len < min_part:
        return -1e9

    ideal_mid = len(text) / 2.0
    score = (
        float(pre_split_score_base())
        - abs(p1_len - p2_len)
        - abs(end - ideal_mid) * 0.5
    )

    try:
        import jieba

        tokens = list(jieba.tokenize(text))
        for i, (w, _s, e) in enumerate(tokens):
            if e != end:
                continue
            if len(w) == 1:
                score -= 500.0
            if i + 1 < len(tokens) and len(tokens[i + 1][0]) == 1:
                score -= 500.0
            break
    except ImportError:
        pass
    return score


def _find_smart_word_boundary_halves(text: str) -> tuple[str, str] | None:
    """
    无标点巨长句：jieba.tokenize 词边界 + 几何中心对称得分（不硬编码词表/词性）。
    仅在词与词交界处下刀，避免字内劈开；单字 token 挂头重罚。
    """
    text = text.strip()
    if not text:
        return None

    from videoaudiotext.config import pre_split_min_part_chars

    min_part = pre_split_min_part_chars()
    try:
        import jieba

        tokens = list(jieba.tokenize(text))
    except ImportError:
        tokens = []

    if len(tokens) <= 1:
        mid = len(text) // 2
        if min_part < mid < len(text) - min_part:
            return text[:mid].strip(), text[mid:].strip()
        return None

    best_end: int | None = None
    best_score = -1e18
    seen_ends: set[int] = set()
    for i in range(len(tokens) - 1):
        end = tokens[i][2]
        if end in seen_ends or end < min_part or end > len(text) - min_part:
            continue
        seen_ends.add(end)
        score = _score_word_boundary_split(text, end)
        if score > best_score:
            best_score = score
            best_end = end

    if best_end is None or best_score < 0:
        return None
    return text[:best_end].strip(), text[best_end:].strip()


def _split_oversized_unpunctuated(
    text: str,
    *,
    max_line: int,
    stretch_limit: int,
) -> List[str]:
    """无标点且 len > stretch_limit：词边界感知中心切分后递归。"""
    halves = _find_smart_word_boundary_halves(text)
    if not halves:
        mid = len(text) // 2
        halves = (text[:mid].strip(), text[mid:].strip())
    left, right = halves
    out = universal_text_splitter(
        left, max_line=max_line, stretch_limit=stretch_limit
    ) + universal_text_splitter(
        right, max_line=max_line, stretch_limit=stretch_limit
    )
    return _collapse_flash_tails(out)


def _score_punctuation_split(text: str, punct_idx: int) -> float:
    """
    动态得分：任一侧 ≤ min_part 字 → 一票否决；
    否则 score = base - |len(left) - len(right)|（越对称越高）。
    """
    from videoaudiotext.config import pre_split_min_part_chars, pre_split_score_base

    part1 = text[:punct_idx].strip()
    part2 = _clean_split_remainder(text[punct_idx + 1 :])
    if not part1 or not part2:
        return -1e9
    p1_len, p2_len = len(part1), len(part2)
    min_part = pre_split_min_part_chars()
    if p1_len <= min_part or p2_len <= min_part:
        return -1e9
    return float(pre_split_score_base()) - abs(p1_len - p2_len)


def _best_punctuation_split_index(text: str) -> int | None:
    indices = [i for i, ch in enumerate(text) if ch in _SPLIT_PUNCT_CHARS]
    if not indices:
        return None
    best_idx: int | None = None
    best_score = -1e18
    for idx in indices:
        score = _score_punctuation_split(text, idx)
        if score > best_score:
            best_score = score
            best_idx = idx
    if best_idx is None or best_score < 0:
        return None
    return best_idx


def universal_text_splitter(
    text: str,
    *,
    max_line: int,
    stretch_limit: int,
) -> List[str]:
    """
    通用源头切分器（纯长度得分 + 容差，不依赖具体词表）。
    max_line: 正常触发递归切分的标准字数（默认 12）
    stretch_limit: 无标点单行放行的物理极限（默认 15）
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_line:
        if _is_unpunctuated_clause(text):
            return [_trim_short_trailing_comma(text)]
        if _best_punctuation_split_index(text) is None:
            clean = _strip_split_punct(text)
            if clean != text and len(clean) <= stretch_limit:
                return [_trim_short_trailing_comma(clean)]
        return [_trim_short_trailing_comma(text)]

    if _is_unpunctuated_clause(text):
        if len(text) <= stretch_limit:
            return [_trim_short_trailing_comma(text)]
        return _split_oversized_unpunctuated(
            text, max_line=max_line, stretch_limit=stretch_limit
        )

    best_idx = _best_punctuation_split_index(text)
    if best_idx is None:
        clean = _strip_split_punct(text)
        if clean != text:
            return universal_text_splitter(
                clean, max_line=max_line, stretch_limit=stretch_limit
            )
        if len(text) <= stretch_limit:
            return [_trim_short_trailing_comma(text)]
        return _split_oversized_unpunctuated(
            text, max_line=max_line, stretch_limit=stretch_limit
        )

    part1 = text[:best_idx]
    part2 = text[best_idx + 1 :]
    out = universal_text_splitter(
        part1, max_line=max_line, stretch_limit=stretch_limit
    ) + universal_text_splitter(
        part2, max_line=max_line, stretch_limit=stretch_limit
    )
    return _collapse_flash_tails(out)


def _pre_split_one_segment(
    seg: str,
    max_chars: int,
    *,
    allow_single: int | None = None,
) -> List[str]:
    """单段入口：强情绪短尾保留 + universal_text_splitter。"""
    seg = seg.strip()
    if not seg:
        return []
    if _is_emotional_short_whole(seg):
        return [_trim_short_trailing_comma(seg)]

    from videoaudiotext.config import max_allow_single_line_chars

    stretch = allow_single if allow_single is not None else max_allow_single_line_chars()
    parts = universal_text_splitter(
        seg,
        max_line=max_chars,
        stretch_limit=stretch,
    )
    return _post_split_refine_parts(parts)


_NOUN_POS_PREFIXES = ("n", "ng", "nz", "nr", "ns", "nt", "nw", "nl")
_PARALLEL_VERB_MARKERS = frozenset("被把的在是有会能想要得地")


def _parallel_phrase_core(text: str) -> str:
    """并列检测用：去首尾空白与句读。"""
    return text.strip().strip("，、；：")


def _is_parallel_noun_phrase(text: str) -> bool:
    """
    并列短词不切镜：≤5 字名词/名词短语（如「酸辣粉」「炸货」）。
    用 jieba 词性；无 jieba 时用轻量启发式。
    """
    core = _parallel_phrase_core(text)
    if not core:
        return False
    from videoaudiotext.config import pre_split_parallel_max_chars

    if len(core) > pre_split_parallel_max_chars():
        return False
    if core.endswith(("。", "！", "？", "!", "?", "；", ";")):
        return False
    if any(ch in core for ch in _PARALLEL_VERB_MARKERS):
        return False
    try:
        import jieba.posseg as pseg

        tokens = [(w.strip(), f) for w, f in pseg.cut(core) if w.strip()]
        if not tokens:
            return False
        for word, flag in tokens:
            if not flag.startswith(_NOUN_POS_PREFIXES):
                return False
            if len(word) == 1 and word in _PARALLEL_VERB_MARKERS:
                return False
        return True
    except ImportError:
        return "，" not in core and "、" not in core


def _merge_parallel_short_parts(parts: List[str]) -> List[str]:
    """连续并列名词短语（各 ≤5 字）强制合并，避免 1s 级闪段。"""
    if len(parts) < 2:
        return list(parts)

    from videoaudiotext.config import pre_split_parallel_max_chars

    max_parallel = pre_split_parallel_max_chars()
    out: List[str] = [parts[0]]
    for seg in parts[1:]:
        prev = out[-1]
        prev_core = _parallel_phrase_core(prev)
        seg_core = _parallel_phrase_core(seg)
        if (
            len(prev_core) <= max_parallel
            and len(seg_core) <= max_parallel
            and _is_parallel_noun_phrase(prev)
            and _is_parallel_noun_phrase(seg)
        ):
            out[-1] = prev + seg
        else:
            out.append(seg)
    return out


def _merge_by_estimated_duration(parts: List[str]) -> List[str]:
    """
    时间戳预估融合：单段预估 <1.5s 且与邻段合并后 ≤15 字 → 强制并段。
    """
    if len(parts) < 2:
        return list(parts)

    from videoaudiotext.config import pre_split_min_est_duration_sec, pre_split_time_merge_max_chars
    from videoaudiotext.text.segments import estimate_duration

    min_dur = pre_split_min_est_duration_sec()
    max_merged = pre_split_time_merge_max_chars()
    out = list(parts)
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(out):
            seg = out[i].strip()
            if not seg:
                out.pop(i)
                changed = True
                continue
            if estimate_duration(seg) >= min_dur:
                i += 1
                continue
            merged_prev = False
            if i > 0 and len(out[i - 1]) + len(seg) <= max_merged:
                out[i - 1] = out[i - 1] + seg
                out.pop(i)
                changed = True
                merged_prev = True
            elif (
                not merged_prev
                and i + 1 < len(out)
                and len(seg) + len(out[i + 1]) <= max_merged
            ):
                out[i] = seg + out[i + 1]
                out.pop(i + 1)
                changed = True
            else:
                i += 1
    return out


def _post_split_refine_parts(parts: List[str]) -> List[str]:
    """pre_split 后处理：并列短词合并 + 时长预估并段。"""
    if not parts:
        return []
    refined = _merge_parallel_short_parts(parts)
    refined = _merge_by_estimated_duration(refined)
    return _collapse_flash_tails(refined)


def pre_split_long_segments(
    segments: List[str],
    *,
    max_chars: int | None = None,
    max_allow_single: int | None = None,
    outer_group_ids: List[int] | None = None,
) -> List[str] | tuple[List[str], List[int]]:
    """
    通用源头切分拦截器（闸门一）：
    - max_chars：触发切分标准线（默认 12）
    - max_allow_single：无标点容差放行线（默认 15，见 MAX_ALLOW_SINGLE_LINE）
    """
    if not segments:
        return ([], []) if outer_group_ids is not None else []

    if max_chars is None:
        from videoaudiotext.config import pre_split_max_chars

        max_chars = pre_split_max_chars()

    out: List[str] = []
    groups: List[int] = []
    track_groups = outer_group_ids is not None
    if track_groups and len(outer_group_ids) != len(segments):
        raise ValueError("outer_group_ids length mismatch")

    for i, seg in enumerate(segments):
        gi = outer_group_ids[i] if track_groups else i
        seg = seg.strip()
        if not seg:
            continue
        if _try_absorb_incoming_flash(out, seg):
            continue
        allow = max_allow_single
        if allow is None:
            from videoaudiotext.config import max_allow_single_line_chars

            allow = max_allow_single_line_chars()
        for part in _pre_split_one_segment(seg, max_chars, allow_single=allow):
            if part:
                out.append(part)
                groups.append(gi)

    if track_groups:
        return out, groups
    return out


def split_and_pre_split_with_groups(text: str) -> tuple[List[str], List[int]]:
    """兼容别名：仅外层 LLM/规则分句，预切分已停用。"""
    segments = split_sentence(text)
    return segments, list(range(len(segments)))


def split_and_pre_split(text: str) -> List[str]:
    """兼容别名：仅外层 LLM/规则分句，预切分已停用。"""
    return split_sentence(text)
