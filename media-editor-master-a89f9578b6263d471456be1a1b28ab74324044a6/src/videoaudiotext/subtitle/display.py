"""Subtitle display formatting: line wrap, balance, layout."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from videoaudiotext.config import (
    scaled_comma_short_opening_max_chars,
    scaled_subtitle_ass_max_chars_per_line,
    scaled_subtitle_cue_max_chars,
    scaled_subtitle_cue_wrap_max_chars,
    scaled_subtitle_line_max_chars,
    scaled_subtitle_line_max_diff,
    subtitle_max_lines_per_cue,
    SUBTITLE_MAX_LINES,
)
from videoaudiotext.subtitle.types import Cue

# 优先断行点（断在标点后，标点留在上一行末尾）
_PREFER_BREAK_AFTER = "，、；：。！？"
_PREFER_BREAK_BEFORE = "与和及而"
_FORBID_BREAK_BEFORE = "不没未得的了过吗呢吧啊呀么"
_ORPHAN_LINE2_CHARS = frozenset("的的了着过吗呢吧啊呀么是与和及而")
def strip_trailing_em_dash(text: str) -> str:
    """Remove trailing em dashes from display text."""
    t = text.rstrip()
    while t.endswith("——"):
        t = t[:-2].rstrip()
    while t.endswith("—"):
        t = t[:-1].rstrip()
    return t


def _normalize_subtitle_text(text: str) -> str:
    """通用字幕双端符号清洗（不依赖具体词表）。"""
    t = text.strip().replace("\n", "").replace("\r", "")
    if not t:
        return ""

    t = t.lstrip("、，。；,.;")

    from videoaudiotext.config import subtitle_strip_trailing_punct

    if subtitle_strip_trailing_punct():
        while t and t[-1] in "，。、；,.;":
            t = t[:-1]

    t = re.sub(r"\s+", " ", t).strip()
    return t


def universal_subtitle_cleaner(text: str) -> str:
    """模块二公开别名：与 _normalize_subtitle_text 同源。"""
    return _normalize_subtitle_text(text)


def _break_penalty(text: str, pos: int) -> int:
    if pos <= 0 or pos >= len(text):
        return 100
    penalty = 0
    if pos >= 2 and text[pos - 2 : pos] == "——":
        penalty -= 14
    if text[pos - 1] in _PREFER_BREAK_AFTER:
        penalty -= 8
    elif text[pos - 1] == "、":
        penalty -= 10
    elif text[pos - 1] not in " \t\u3000":
        penalty += 6
    if text[pos] in _FORBID_BREAK_BEFORE:
        penalty += 8
    if text[pos] in _PREFER_BREAK_BEFORE:
        penalty += 2
    return penalty


def _jieba_word_end_positions(text: str) -> set[int]:
    """词边界切分点（字符索引，表示可在该位置断开）。"""
    positions: set[int] = set()
    try:
        import jieba
    except ImportError:
        return positions
    current = 0
    for word in jieba.cut(text):
        current += len(word)
        if 0 < current < len(text):
            positions.add(current)
    return positions


def _is_break_allowed(text: str, pos: int) -> bool:
    if pos <= 0 or pos >= len(text):
        return False
    from videoaudiotext.subtitle.protected_spans import position_inside_protected_span
    from videoaudiotext.subtitle.quotes import (
        QUOTE_CLOSERS,
        QUOTE_OPENERS,
        right_heads_into_adv_predicate,
        right_heads_into_de_phrase,
    )

    if position_inside_protected_span(text, pos):
        return False
    if pos >= 1 and pos < len(text) and text[pos - 1] == "—" and text[pos] == "—":
        return False
    from videoaudiotext.subtitle.breaks import position_breaks_compound

    if position_breaks_compound(text, pos):
        return False
    left = text[:pos].strip()
    right = text[pos:].strip()
    if right and right[0] in QUOTE_OPENERS:
        from videoaudiotext.subtitle.quotes import _break_between_adjacent_quotes

        if _break_between_adjacent_quotes(left, right):
            return True
    if right.startswith("的"):
        if text[pos - 1] in '"\'\u201d\u2019\u300d\u300b':
            return True
        if left.rstrip().endswith(tuple(QUOTE_CLOSERS)):
            return True
        if pos in _jieba_word_end_positions(text):
            return True
        return False
    if right_heads_into_de_phrase(right):
        return False
    if right_heads_into_adv_predicate(right):
        return False
    if left.rstrip("，、；：").endswith("的"):
        return False
    if left.rstrip("，、；：").endswith("是"):
        return False
    if text[pos - 1] in _PREFER_BREAK_AFTER:
        return True
    if text[pos - 1] in '"\'\u201d\u2019\u300d\u300b':
        return True
    from videoaudiotext.subtitle.parentheses import PAREN_CLOSERS

    if text[pos - 1] in PAREN_CLOSERS:
        return True
    return pos in _jieba_word_end_positions(text)


def _semantic_balance_two_lines(
    text: str,
    max_line: int,
    *,
    wrap_max: int | None = None,
) -> str | None:
    """
    jieba 词性 + 字数天平：在词边界选最佳双行切点。
    得分越低越好：靠近句中、标点/连词后加分；结构词/量词剥离扣分。
    """
    t = _normalize_subtitle_text(text.replace("\n", ""))
    if not t:
        return None
    if len(t) <= max_line:
        return t

    wrap_lim = wrap_max if wrap_max is not None else scaled_subtitle_cue_wrap_max_chars()
    max_diff = scaled_subtitle_line_max_diff()
    mid = len(t) / 2.0
    best: tuple[float, str, str] | None = None

    def _consider(pos: int, left_word: str = "", right_word: str = "", lf: str = "", rf: str = "") -> None:
        nonlocal best
        if pos <= 0 or pos >= len(t):
            return
        line1 = t[:pos].strip()
        line2 = t[pos:].strip()
        if not line1 or not line2:
            return
        if len(line1) > wrap_lim or len(line2) > wrap_lim:
            return
        c1, c2 = _core_char_len(line1), _core_char_len(line2)
        if abs(c1 - c2) > max_diff:
            return
        from videoaudiotext.subtitle.parentheses import PAREN_CLOSERS, PAREN_OPENERS
        from videoaudiotext.subtitle.protected_spans import (
            position_inside_protected_span,
            split_orphan_protected_boundary,
        )
        from videoaudiotext.subtitle.quotes import _QUOTE_CLOSERS, _QUOTE_OPENERS

        if position_inside_protected_span(t, pos) or split_orphan_protected_boundary(line1, line2):
            return
        if line1 and line1[-1] in _QUOTE_OPENERS | PAREN_OPENERS:
            return
        if line2 and line2[0] in _QUOTE_CLOSERS | PAREN_CLOSERS:
            return
        try:
            from videoaudiotext.subtitle.text_timing import _accept_wrap_split

            if not _accept_wrap_split(
                line1, line2,
                left_word=left_word, right_word=right_word,
                left_flag=lf, right_flag=rf,
            ):
                return
        except ImportError:
            if not _is_break_allowed(t, pos):
                return

        score = abs(pos - mid) * 2.0
        if pos >= 2 and t[pos - 2 : pos] == "——":
            score -= 28.0
        if pos > 0 and t[pos - 1] in _PREFER_BREAK_AFTER + "。！？；.!?;":
            score -= 24.0
        if pos > 0 and t[pos - 1] == "、":
            score -= 20.0
        if rf.startswith(("c", "cc")) or right_word in _PREFER_BREAK_BEFORE:
            score -= 12.0
        if lf.startswith(("u", "uj", "ul")) or rf.startswith(("u", "uj", "ul")):
            score += 14.0
        if right_word in ("的", "了", "着", "过", "吗", "呢", "吧", "啊", "呀", "么"):
            score += 16.0
        if left_word in ("一", "个", "条", "位", "份", "块") and c2 <= 6:
            score += 12.0
        score += _break_penalty(t, pos)

        if best is None or score < best[0]:
            best = (score, line1, line2)

    try:
        import jieba.posseg as pseg

        words = list(pseg.cut(t))
        pos = 0
        for i, pair in enumerate(words):
            word, flag = pair.word, pair.flag
            if i > 0:
                _consider(pos, words[i - 1].word, word, words[i - 1].flag, flag)
            pos += len(word)
    except ImportError:
        pass

    for i, ch in enumerate(t):
        if ch == "，" and 0 < i + 1 < len(t):
            _consider(i + 1)

    from videoaudiotext.subtitle.quotes import preferred_semantic_break_positions

    for pos in preferred_semantic_break_positions(t):
        _consider(pos)

    if best:
        return f"{best[1]}\n{best[2]}"

    relaxed = _jieba_balanced_two_lines(t, wrap_lim)
    if relaxed and "\n" in relaxed:
        a, b = relaxed.split("\n", 1)
        from videoaudiotext.subtitle.protected_spans import split_orphan_protected_boundary

        if (
            len(a) <= wrap_lim
            and len(b) <= wrap_lim
            and not split_orphan_protected_boundary(a, b)
        ):
            return relaxed
    return _balanced_two_lines(t, wrap_lim)


def _jieba_balanced_two_lines(text: str, max_line: int) -> str | None:
    """无标点长句：在允许的词边界上找最接近中点的折行点。"""
    try:
        import jieba
    except ImportError:
        return None

    if len(text) <= max_line:
        return text

    words = list(jieba.cut(text))
    if len(words) <= 1:
        return None

    mid = len(text) / 2.0
    current = 0
    best: tuple[int, str, str] | None = None
    for word in words:
        current += len(word)
        if not _is_break_allowed(text, current):
            continue
        after_comma = current > 0 and text[current - 1] in "，、；"
        lim = _line_len_limit(max_line, after_comma=after_comma)
        line1 = text[:current].strip()
        line2 = text[current:].strip()
        if not line1 or not line2:
            continue
        if len(line1) > lim or len(line2) > max_line:
            continue
        from videoaudiotext.subtitle.parentheses import PAREN_CLOSERS, PAREN_OPENERS
        from videoaudiotext.subtitle.protected_spans import split_orphan_protected_boundary
        from videoaudiotext.subtitle.quotes import _QUOTE_CLOSERS, _QUOTE_OPENERS

        if split_orphan_protected_boundary(line1, line2):
            continue
        if line1 and line1[-1] in _QUOTE_OPENERS | PAREN_OPENERS:
            continue
        if line2 and line2[0] in _QUOTE_CLOSERS | PAREN_CLOSERS:
            continue
        if len(line2) == 1 and line2 not in _PREFER_BREAK_AFTER + "。！？；.!?;":
            continue
        if not _wrap_split_acceptable(line1, line2, text, current):
            continue
        score = abs(len(line1) - len(line2)) + _break_penalty(text, current)
        if best is None or score < best[0]:
            best = (score, line1, line2)

    if best:
        return f"{best[1]}\n{best[2]}"
    return None


def _wrap_split_acceptable(
    line1: str,
    line2: str,
    full_text: str,
    pos: int,
) -> bool:
    """fallback 折行与主路径共享最低语义约束（可选 jieba 词性）。"""
    try:
        from videoaudiotext.subtitle.text_timing import _accept_wrap_split
        import jieba.posseg as pseg

        words = list(pseg.cut(full_text))
        left_word = right_word = ""
        left_flag = right_flag = ""
        cursor = 0
        for i, pair in enumerate(words):
            word, flag = pair.word, pair.flag
            if cursor == pos and i > 0:
                left_word, left_flag = words[i - 1].word, words[i - 1].flag
                right_word, right_flag = word, flag
                break
            cursor += len(word)
        return _accept_wrap_split(
            line1,
            line2,
            left_word=left_word,
            right_word=right_word,
            left_flag=left_flag,
            right_flag=right_flag,
        )
    except ImportError:
        return _is_break_allowed(full_text, pos)


def _line_core_diff(line1: str, line2: str) -> int:
    return abs(_core_char_len(line1) - _core_char_len(line2))


def _line_len_limit(max_line: int, *, after_comma: bool) -> int:
    return max_line + 3 if after_comma else max_line


def _balanced_two_lines(text: str, max_line: int) -> str | None:
    n = len(text)
    if n <= max_line:
        return text
    best: tuple[int, str, str] | None = None
    for pos in range(1, n):
        if not _is_break_allowed(text, pos):
            continue
        after_comma = text[pos - 1] in "，、；"
        lim = _line_len_limit(max_line, after_comma=after_comma)
        line1 = text[:pos].strip()
        line2 = text[pos:].strip()
        if not line1 or not line2:
            continue
        if len(line1) > lim or len(line2) > max_line:
            continue
        from videoaudiotext.subtitle.parentheses import PAREN_CLOSERS, PAREN_OPENERS
        from videoaudiotext.subtitle.protected_spans import split_orphan_protected_boundary
        from videoaudiotext.subtitle.quotes import _QUOTE_CLOSERS, _QUOTE_OPENERS

        if split_orphan_protected_boundary(line1, line2):
            continue
        if line1 and line1[-1] in _QUOTE_OPENERS | PAREN_OPENERS:
            continue
        if line2 and line2[0] in _QUOTE_CLOSERS | PAREN_CLOSERS:
            continue
        if len(line2) == 1 and line2 not in _PREFER_BREAK_AFTER + "。！？；.!?;":
            continue
        if not _wrap_split_acceptable(line1, line2, text, pos):
            continue
        score = abs(len(line1) - len(line2)) + _break_penalty(text, pos)
        if best is None or score < best[0]:
            best = (score, line1, line2)
    if best:
        return f"{best[1]}\n{best[2]}"
    return None


def _jieba_pack_lines(text: str, max_line: int, max_lines: int) -> str | None:
    """按 jieba 词边界贪心装箱，避免固定字数截断。"""
    try:
        import jieba
    except ImportError:
        return None

    words = list(jieba.cut(text))
    if not words:
        return None

    from videoaudiotext.subtitle.parentheses import PAREN_CLOSERS, PAREN_OPENERS
    from videoaudiotext.subtitle.protected_spans import split_orphan_protected_boundary
    from videoaudiotext.subtitle.quotes import _QUOTE_CLOSERS, _QUOTE_OPENERS

    lines: List[str] = []
    buf = ""
    for word in words:
        if not buf:
            buf = word
            continue
        candidate = buf + word
        if len(candidate) <= max_line:
            buf = candidate
            continue
        if len(lines) + 1 >= max_lines:
            buf = candidate
        else:
            if buf and (
                split_orphan_protected_boundary(buf, word)
                or (buf and buf[-1] in _QUOTE_OPENERS | PAREN_OPENERS)
                or (word and word[0] in _QUOTE_CLOSERS | PAREN_CLOSERS)
            ):
                buf = candidate
            else:
                lines.append(buf)
                buf = word
    if buf:
        lines.append(buf)

    if len(lines) <= max_lines and all(len(ln) <= max_line for ln in lines):
        return "\n".join(lines)
    if len(lines) == max_lines + 1 and len(lines[-1]) <= max_line:
        lines[-2] += lines[-1]
        lines.pop()
        if all(len(ln) <= max_line * 2 for ln in lines):
            return "\n".join(lines)
    return None


def _balanced_multi_lines(text: str, max_line: int, max_lines: int) -> str:
    packed = _jieba_pack_lines(text, max_line, max_lines)
    if packed:
        return packed

    remaining = text
    lines: List[str] = []
    for _ in range(max_lines):
        if not remaining:
            break
        if len(remaining) <= max_line:
            lines.append(remaining)
            remaining = ""
            break
        two = _balanced_two_lines(remaining, max_line)
        if two and "\n" in two:
            first, rest = two.split("\n", 1)
            lines.append(first)
            remaining = rest
        else:
            cut = None
            for pos in range(min(max_line, len(remaining)), 0, -1):
                if _is_break_allowed(remaining, pos):
                    cut = pos
                    break
            if cut is None:
                lines.append(remaining)
                remaining = ""
                break
            lines.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
    if remaining and lines:
        merged = lines[-1] + remaining
        if len(merged) <= max_line * max_lines:
            lines[-1] = merged
        else:
            extra = _jieba_pack_lines(merged, max_line, 1)
            lines[-1] = extra or merged[: max_line * 2]
    elif remaining:
        lines.append(remaining)
    return "\n".join(lines)


def wrap_text_for_screen(
    text: str,
    max_line: int | None = None,
    max_lines: int = SUBTITLE_MAX_LINES,
) -> str:
    """
    双行均衡折行：尽量让各行字数接近；优先在 ，、与和 等标点后断行。
    """
    if max_line is None:
        max_line = scaled_subtitle_line_max_chars()
    text = _normalize_subtitle_text(text)
    if not text:
        return ""
    if len(text) <= max_line:
        return text

    if max_lines >= 2:
        two = _balanced_two_lines(text, max_line)
        if two:
            return two
        if "，" not in text:
            jieba_two = _jieba_balanced_two_lines(text, max_line)
            if jieba_two:
                return jieba_two

    if len(text) <= max_line * max_lines:
        return _balanced_multi_lines(text, max_line, max_lines)

    return _balanced_multi_lines(text, max_line, max_lines)
def _collapse_orphan_punct_lines(text: str) -> str:
    """避免折行后出现仅含标点的行（如单独一个 。）。"""
    if not text or "\n" not in text:
        return text
    lines = text.split("\n")
    out: List[str] = []
    for ln in lines:
        stripped = ln.strip()
        if (
            out
            and stripped
            and len(stripped) <= 2
            and all(ch in _PREFER_BREAK_AFTER + "。！？；.!?;" for ch in stripped)
        ):
            out[-1] = out[-1].rstrip() + stripped
        elif stripped:
            out.append(stripped)
    return "\n".join(out)


def _reclaim_orphan_line2_chars(
    text: str,
    *,
    wrap_max: int | None = None,
) -> str:
    """第二行仅 1 个虚词/助词时回并到第一行（允许略超 wrap_max）。"""
    if not text or "\n" not in text:
        return text
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) != 2:
        return text
    line1, line2 = lines[0], lines[1]
    if len(line2) != 1 or line2 not in _ORPHAN_LINE2_CHARS:
        return text
    wrap_lim = wrap_max if wrap_max is not None else scaled_subtitle_cue_wrap_max_chars()
    merged = f"{line1}{line2}"
    overflow = 1
    if len(merged) <= wrap_lim + overflow and _core_char_len(merged) <= wrap_lim + overflow:
        return merged
    return text


def _finalize_wrapped_display(
    text: str,
    *,
    wrap_max: int | None = None,
) -> str:
    """折行后统一后处理：标点孤儿合并 + 虚词孤儿回吸。"""
    if not text:
        return ""
    text = _collapse_orphan_punct_lines(text)
    return _reclaim_orphan_line2_chars(text, wrap_max=wrap_max)


def _rebalance_two_lines_if_improved(
    plain: str,
    wrapped: str,
    *,
    line_lim: int,
    wrap_lim: int,
    max_lines: int,
) -> str:
    """仅在句中再拆后行差严格变小时才替换初稿，避免 2:13 比逗号断行更差。"""
    if "\n" not in wrapped or max_lines < 2:
        return wrapped
    lines = [ln.strip() for ln in wrapped.split("\n") if ln.strip()]
    if len(lines) != 2:
        return wrapped
    current_diff = _line_core_diff(lines[0], lines[1])
    if current_diff <= scaled_subtitle_line_max_diff():
        return wrapped

    left, right = _split_plain_at_center(plain)
    if not left or not right:
        return wrapped
    l1 = _format_balance_wrap(
        left,
        max_line=line_lim,
        max_lines=1,
        wrap_max=wrap_lim,
        skip_comma=True,
    )
    l2 = _format_balance_wrap(
        right,
        max_line=line_lim,
        max_lines=1,
        wrap_max=wrap_lim,
        skip_comma=True,
    )
    if "\n" in l1 or "\n" in l2:
        return wrapped
    if _line_core_diff(l1, l2) >= current_diff:
        return wrapped
    from videoaudiotext.subtitle.protected_spans import split_orphan_protected_boundary

    if split_orphan_protected_boundary(l1, l2):
        return wrapped
    return f"{l1}\n{l2}"
@dataclass(frozen=True)
class SubtitleLayoutContext:
    """屏显折行布局：有效画面区与单行字数约束。"""

    content_rect: object | None = None
    max_line: int | None = None
    prefer_single_line: bool | None = None


def format_cue_display(
    text: str,
    layout_ctx: SubtitleLayoutContext | None = None,
) -> str:
    """单一折行入口：预览与 ASS/SRT 烧录共用。"""
    ctx = layout_ctx or SubtitleLayoutContext()
    max_line = ctx.max_line
    prefer_single_line = ctx.prefer_single_line
    if max_line is None or prefer_single_line is None:
        if ctx.content_rect is not None:
            from videoaudiotext.subtitle.safe_area import subtitle_layout_for_content

            layout_max, layout_prefer = subtitle_layout_for_content(ctx.content_rect)
        else:
            layout_max = scaled_subtitle_cue_max_chars()
            layout_prefer = False
        if max_line is None:
            max_line = layout_max
        if prefer_single_line is None:
            prefer_single_line = layout_prefer

    display = _display_text_for_cue(
        text,
        max_line=max_line,
        prefer_single_line=prefer_single_line,
    )
    return _clamp_display_lines(display, max_lines=subtitle_max_lines_per_cue())


def subtitle_display_for_sentence(
    sentence: str,
    *,
    content_rect=None,
    max_line: int | None = None,
    prefer_single_line: bool | None = None,
) -> str:
    """与 preview_split「字幕:」行同源：按有效画面区折行，供预览与 ASS/SRT 烧录共用。"""
    return format_cue_display(
        sentence,
        SubtitleLayoutContext(
            content_rect=content_rect,
            max_line=max_line,
            prefer_single_line=prefer_single_line,
        ),
    )
def _content_rect_for_cue(
    start: float,
    end: float,
    *,
    content_rect=None,
    segment_bounds: List[tuple[float, float]] | None = None,
    content_rects: list | None = None,
):
    if content_rect is not None:
        return content_rect
    if content_rects and segment_bounds:
        from videoaudiotext.subtitle.safe_area import segment_index_for_cue

        si = segment_index_for_cue(start, end, segment_bounds)
        si = min(si, len(content_rects) - 1)
        return content_rects[si]
    return None
def _core_char_len(text: str) -> int:
    """可见字符数（含常见标点，不含空白），用于字数/合并/像素阈值。"""
    return len("".join(ch for ch in text if ch not in " \t\n\r\u3000"))
def _clamp_display_lines(text: str, max_lines: int | None = None) -> str:
    """屏显/ASS 硬限制：最多 max_lines 行（默认 SUBTITLE_MAX_LINES=2）。"""
    if max_lines is None:
        max_lines = subtitle_max_lines_per_cue()
    plain = text.replace("\\N", "\n")
    lines = [ln.strip() for ln in plain.split("\n") if ln.strip()]
    if not lines:
        return ""
    if len(lines) <= max_lines:
        return "\n".join(lines)
    if max_lines <= 1:
        return lines[0]
    merged = lines[: max_lines - 1] + ["".join(lines[max_lines - 1 :])]
    merged_text = "\n".join(merged)
    wrapped = _ass_enforce_max_chars_per_line(merged_text)
    if "\n" in wrapped:
        return _clamp_display_lines(wrapped, max_lines=max_lines)
    return merged_text


def _ass_enforce_max_chars_per_line(text: str, max_line: int | None = None) -> str:
    """
    ASS 单行硬限制：超过 max_line 时在词边界插入 \\N。
    例：「人间烦恼被一顿热气腾腾的小吃」→ 两行各 ≤10 字。
    """
    if max_line is None:
        max_line = scaled_subtitle_ass_max_chars_per_line()
    plain = _normalize_subtitle_text(text.replace("\\N", "\n").replace("\n", ""))
    if not plain:
        return ""
    if _core_char_len(plain) <= max_line and len(plain) <= max_line + 2:
        return plain

    if "\n" in text:
        lines = [
            _ass_enforce_max_chars_per_line(ln, max_line=max_line)
            for ln in text.replace("\\N", "\n").split("\n")
        ]
        return "\n".join(ln for ln in lines if ln)

    left, right = _split_plain_at_center(plain)
    if left and right:
        l1 = _ass_enforce_max_chars_per_line(left, max_line=max_line)
        l2 = _ass_enforce_max_chars_per_line(right, max_line=max_line)
        if "\n" not in l1 and "\n" not in l2:
            return f"{l1}\n{l2}"

    wrapped = _format_balance_wrap(
        plain,
        max_line=max_line,
        max_lines=SUBTITLE_MAX_LINES,
        wrap_max=max_line,
        skip_comma=False,
    )
    if "\n" in wrapped:
        return _clamp_display_lines(
            _finalize_wrapped_display(wrapped, wrap_max=max_line),
            max_lines=max_lines,
        )
    if len(plain) > max_line:
        cut = max_line
        try:
            import jieba

            pos = 0
            for word in jieba.cut(plain):
                pos += len(word)
                if pos >= max_line:
                    cut = pos
                    break
        except ImportError:
            cut = max_line
        if 0 < cut < len(plain):
            return _clamp_display_lines(
                _finalize_wrapped_display(
                    f"{plain[:cut].strip()}\n{plain[cut:].strip()}",
                    wrap_max=max_line,
                ),
                max_lines=max_lines,
            )
    return _clamp_display_lines(
        _finalize_wrapped_display(plain, wrap_max=max_line),
        max_lines=max_lines,
    )
def _join_phrase_units(parts: List[str]) -> str:
    """合并多个逗号分步短语，恢复自然标点。"""
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not out:
            out = p
            continue
        if out[-1] in _PREFER_BREAK_AFTER + "。！？；":
            out += p
        elif p and p[0] in _PREFER_BREAK_AFTER:
            out += p
        else:
            out += "，" + p
    return out
def _format_balance_wrap(
    text: str,
    *,
    max_line: int | None = None,
    max_lines: int = SUBTITLE_MAX_LINES,
    wrap_max: int | None = None,
    skip_comma: bool = False,
) -> str:
    """语义天平双行折行；逗号切分失败或超长时走 jieba 词性打分。"""
    line_lim = max_line if max_line is not None else scaled_subtitle_cue_max_chars()
    wrap_lim = wrap_max if wrap_max is not None else scaled_subtitle_cue_wrap_max_chars()
    plain = _normalize_subtitle_text(text.replace("\n", ""))
    if not plain:
        return ""

    if len(plain) <= line_lim and _core_char_len(plain) <= line_lim:
        return plain

    if not skip_comma and "，" in plain:
        ok, comma_wrap = _wrap_at_comma_display(
            plain, max_line=line_lim, wrap_max=wrap_lim
        )
        if ok and comma_wrap:
            return _finalize_wrapped_display(comma_wrap, wrap_max=wrap_lim)

    if max_lines >= 2:
        balanced = _semantic_balance_two_lines(plain, line_lim, wrap_max=wrap_lim)
        if balanced and "\n" in balanced:
            return _finalize_wrapped_display(balanced, wrap_max=wrap_lim)

    wrapped = wrap_text_for_screen(plain, max_line=line_lim, max_lines=max_lines)
    wrapped = _finalize_wrapped_display(wrapped, wrap_max=wrap_lim)
    wrapped = _rebalance_two_lines_if_improved(
        plain,
        wrapped,
        line_lim=line_lim,
        wrap_lim=wrap_lim,
        max_lines=max_lines,
    )
    return _finalize_wrapped_display(wrapped, wrap_max=wrap_lim)
def _split_plain_at_center(text: str) -> tuple[str, str]:
    """语义天平：在 50% 字数附近找逗号或 jieba 词边界，拆成两段。"""
    text = _normalize_subtitle_text(text)
    if not text:
        return "", ""
    if len(text) <= 1:
        return text, ""

    mid = len(text) / 2.0
    comma_positions = [
        i + 1
        for i, ch in enumerate(text)
        if ch in "，、；" and 0 < i + 1 < len(text)
    ]
    from videoaudiotext.subtitle.protected_spans import position_inside_protected_span

    comma_positions = [
        p for p in comma_positions if not position_inside_protected_span(text, p)
    ]
    if comma_positions:
        best = min(comma_positions, key=lambda p: abs(p - mid))
        left = text[:best].strip()
        right = text[best:].strip()
        from videoaudiotext.subtitle.protected_spans import split_orphan_protected_boundary

        if left and right and not split_orphan_protected_boundary(left, right):
            return left, right

    try:
        import jieba

        words = list(jieba.cut(text))
        curr = 0
        split_pos = len(text)
        for word in words:
            curr += len(word)
            if curr >= mid:
                split_pos = curr
                break
        if 0 < split_pos < len(text):
            left = text[:split_pos].strip()
            right = text[split_pos:].strip()
            from videoaudiotext.subtitle.protected_spans import (
                position_inside_protected_span,
                split_orphan_protected_boundary,
            )

            if (
                left
                and right
                and not position_inside_protected_span(text, split_pos)
                and not split_orphan_protected_boundary(left, right)
            ):
                return left, right
    except ImportError:
        pass

    two = _jieba_balanced_two_lines(text, scaled_subtitle_cue_max_chars())
    if two and "\n" in two:
        a, b = two.split("\n", 1)
        from videoaudiotext.subtitle.protected_spans import split_orphan_protected_boundary

        if not split_orphan_protected_boundary(a.strip(), b.strip()):
            return a.strip(), b.strip()

    best_pos: int | None = None
    best_dist = len(text) + 1
    for pos in range(1, len(text)):
        if not _is_break_allowed(text, pos):
            continue
        dist = abs(pos - mid)
        if dist < best_dist:
            best_dist = dist
            best_pos = pos
    if best_pos is not None:
        left = text[:best_pos].strip()
        right = text[best_pos:].strip()
        from videoaudiotext.subtitle.protected_spans import split_orphan_protected_boundary

        if left and right and not split_orphan_protected_boundary(left, right):
            return left, right

    cut = len(text) // 2
    split_pair = None
    try:
        from videoaudiotext.subtitle.text_timing import split_phrase_at_jieba_midpoint

        split_pair = split_phrase_at_jieba_midpoint(text)
    except ImportError:
        pass
    if split_pair:
        from videoaudiotext.subtitle.protected_spans import split_orphan_protected_boundary

        if not split_orphan_protected_boundary(split_pair[0], split_pair[1]):
            return split_pair
    left = text[:cut].strip()
    right = text[cut:].strip()
    from videoaudiotext.subtitle.protected_spans import split_orphan_protected_boundary

    if left and right and not split_orphan_protected_boundary(left, right):
        return left, right
    return text, ""
def _wrap_at_comma_display(
    text: str,
    *,
    max_line: int,
    wrap_max: int | None = None,
    style_wrap: bool = True,
) -> tuple[bool, str | None]:
    """
    含逗号 Cue 的屏显折行。返回 (成功与否, 双行文本)。
    规则 1/2：短尾、短起；规则 3：放宽至 wrap_max 且行差 ≤ scaled_subtitle_line_max_diff。
    """
    short_open = scaled_comma_short_opening_max_chars()
    wrap_lim = wrap_max if wrap_max is not None else scaled_subtitle_cue_wrap_max_chars()
    max_diff = scaled_subtitle_line_max_diff()
    t = text.strip()
    if "，" not in t:
        return False, None

    if _core_char_len(t) <= max_line and len(t) <= max_line:
        return False, None

    if style_wrap:
        body, tail = t.rsplit("，", 1)
        tail = tail.strip()
        body = body.strip()
        if body and tail:
            rc = _core_char_len(tail)
            if 0 < rc <= 4:
                wrapped = f"{body}，\n{tail}"
                if _comma_split_valid(wrapped, wrap_lim, max_diff):
                    return True, wrapped

        idx = t.find("，")
        if idx > 0:
            left = t[: idx + 1].strip()
            right = t[idx + 1 :].strip()
            from videoaudiotext.subtitle.text_timing import _is_mid_comma_interjection

            if (
                right
                and _core_char_len(left.rstrip("，")) <= short_open
                and not _is_mid_comma_interjection(left)
            ):
                wrapped = f"{left}\n{right}"
                if _comma_split_valid(wrapped, wrap_lim, max_diff):
                    return True, wrapped

    need_wrap = _core_char_len(t) > max_line or len(t) > max_line
    if need_wrap:
        from videoaudiotext.subtitle.text_timing import _is_mid_comma_interjection

        best: tuple[int, str, str] | None = None
        for i, ch in enumerate(t):
            if ch != "，" or i >= len(t) - 1:
                continue
            pos = i + 1
            line1 = t[:pos].strip()
            line2 = t[pos:].strip()
            if not line1 or not line2:
                continue
            if _is_mid_comma_interjection(line1):
                continue
            if len(line1) > wrap_lim or len(line2) > wrap_lim:
                continue
            diff = abs(_core_char_len(line1) - _core_char_len(line2))
            if diff > max_diff:
                continue
            score = diff
            if best is None or score < best[0]:
                best = (score, line1, line2)
        if best:
            return True, f"{best[1]}\n{best[2]}"

    return False, None


def _comma_split_valid(wrapped: str, wrap_lim: int, max_diff: int) -> bool:
    if "\n" not in wrapped:
        return False
    a, b = wrapped.split("\n", 1)
    a, b = a.strip(), b.strip()
    if not a or not b:
        return False
    if len(a) > wrap_lim or len(b) > wrap_lim:
        return False
    return abs(_core_char_len(a) - _core_char_len(b)) <= max_diff


def _display_text_for_cue(
    plain: str,
    *,
    max_line: int | None = None,
    prefer_single_line: bool = False,
) -> str:
    """屏显折行：超长强制天平双行；含逗号优先逗号切分，失败必 fallback 天平。"""
    if max_line is None:
        max_line = scaled_subtitle_cue_max_chars()
    wrap_max = max_line if prefer_single_line else scaled_subtitle_cue_wrap_max_chars()
    plain = _normalize_subtitle_text(plain.replace("\n", ""))
    if not plain:
        return ""

    max_lines = subtitle_max_lines_per_cue()
    style_wrap = not prefer_single_line

    if (
        len(plain) > wrap_max
        or _core_char_len(plain) > wrap_max
        or len(plain) > max_line
        or _core_char_len(plain) > max_line
    ):
        return _clamp_display_lines(
            _format_balance_wrap(
                plain, max_line=max_line, wrap_max=wrap_max, skip_comma=False
            ),
            max_lines=max_lines,
        )

    if "，" in plain:
        ok, comma_wrap = _wrap_at_comma_display(
            plain,
            max_line=max_line,
            wrap_max=wrap_max,
            style_wrap=style_wrap,
        )
        if ok and comma_wrap:
            return _clamp_display_lines(
                _finalize_wrapped_display(comma_wrap, wrap_max=wrap_max),
                max_lines=max_lines,
            )
        if _core_char_len(plain) > max_line or len(plain) > max_line:
            return _clamp_display_lines(
                _format_balance_wrap(
                    plain, max_line=max_line, wrap_max=wrap_max, skip_comma=True
                ),
                max_lines=max_lines,
            )
        return plain

    if len(plain) <= max_line and _core_char_len(plain) <= max_line:
        return plain

    try:
        from videoaudiotext.subtitle.text_timing import wrap_display_two_lines

        wrapped = wrap_display_two_lines(plain, max_line=max_line)
        if wrapped and "\n" in wrapped:
            return _clamp_display_lines(
                _finalize_wrapped_display(wrapped, wrap_max=wrap_max),
                max_lines=max_lines,
            )
    except ImportError:
        pass

    return _clamp_display_lines(
        _format_balance_wrap(
            plain, max_line=max_line, wrap_max=wrap_max, skip_comma=True
        ),
        max_lines=max_lines,
    )
def _layout_for_cue(
    start: float,
    end: float,
    *,
    content_rect=None,
    segment_bounds: List[tuple[float, float]] | None = None,
    content_rects: list | None = None,
) -> tuple[int, bool]:
    from videoaudiotext.subtitle.safe_area import (
        max_chars_for_content_width,
        content_prefers_single_line,
        segment_index_for_cue,
    )

    rect = content_rect
    if rect is None and content_rects and segment_bounds:
        si = segment_index_for_cue(start, end, segment_bounds)
        si = min(si, len(content_rects) - 1)
        rect = content_rects[si]
    if rect is not None:
        return (
            max_chars_for_content_width(rect.width, content_height=rect.height),
            content_prefers_single_line(rect),
        )
    return scaled_subtitle_cue_max_chars(), False


def _max_line_for_cue(
    start: float,
    end: float,
    *,
    content_rect=None,
    segment_bounds: List[tuple[float, float]] | None = None,
    content_rects: list | None = None,
) -> int:
    max_line, _ = _layout_for_cue(
        start,
        end,
        content_rect=content_rect,
        segment_bounds=segment_bounds,
        content_rects=content_rects,
    )
    return max_line


def _finalize_cue_displays(
    cues: List[Cue],
    *,
    content_rect=None,
    segment_bounds: List[tuple[float, float]] | None = None,
    content_rects: list | None = None,
) -> List[Cue]:
    out: List[Cue] = []
    from videoaudiotext.subtitle.safe_area import strip_emotion_punct_for_layout

    for text, start, end in cues:
        from videoaudiotext.config import subtitle_match_pipeline_segments
        from videoaudiotext.subtitle.wrap_reveal import wrap_reveal_enabled

        rect = _content_rect_for_cue(
            start,
            end,
            content_rect=content_rect,
            segment_bounds=segment_bounds,
            content_rects=content_rects,
        )
        if subtitle_match_pipeline_segments() or wrap_reveal_enabled():
            raw = text.replace("\n", "").replace("\\N", "\n")
            if wrap_reveal_enabled():
                display = _normalize_subtitle_text(raw.replace("\n", ""))
            else:
                display = format_cue_display(
                    raw,
                    SubtitleLayoutContext(
                        content_rect=rect,
                        prefer_single_line=True if wrap_reveal_enabled() else None,
                    ),
                )
            out.append((display, start, end))
            continue

        plain = _normalize_subtitle_text(text.replace("\n", ""))
        if plain and plain[0] in _PREFER_BREAK_AFTER:
            plain = plain.lstrip("，、；：。！？")
        layout_plain = strip_emotion_punct_for_layout(plain)
        max_line, prefer_single = _layout_for_cue(
            start,
            end,
            content_rect=rect,
            segment_bounds=segment_bounds,
            content_rects=content_rects,
        )
        display = _display_text_for_cue(
            plain, max_line=max_line, prefer_single_line=prefer_single
        )
        ass_line_lim = max_line
        if _core_char_len(layout_plain) > ass_line_lim or any(
            _core_char_len(strip_emotion_punct_for_layout(ln)) > ass_line_lim
            for ln in display.replace("\\N", "\n").split("\n")
        ):
            display = _ass_enforce_max_chars_per_line(display, max_line=ass_line_lim)
        display = _clamp_display_lines(display, max_lines=subtitle_max_lines_per_cue())
        out.append((display, start, end))
    return out
