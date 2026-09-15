"""Quote-span helpers and quote-safe screen-line packing."""

from __future__ import annotations

import os

# Opening → closing pairs (curly, straight, CJK book-title quotes)
QUOTE_OPENERS = frozenset('"\'\u201c\u2018\u300c\u300a')
QUOTE_CLOSERS = frozenset('"\'\u201d\u2019\u300d\u300b')
_QUOTE_PAIRS = {
    "\u201c": "\u201d",  # “ ”
    "\u2018": "\u2019",  # ‘ ’
    "\u300c": "\u300d",  # 「 」
    "\u300a": "\u300b",  # 《 》
    '"': '"',
    "'": "'",
}

# Aliases used by display.py
_QUOTE_OPENERS = QUOTE_OPENERS
_QUOTE_CLOSERS = QUOTE_CLOSERS


def quoted_char_spans(text: str) -> list[tuple[int, int]]:
    """Return [start, end) spans that must not be broken by line/phrase splits."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch not in QUOTE_OPENERS:
            i += 1
            continue
        close = _QUOTE_PAIRS.get(ch, ch)
        j = i + 1
        found = False
        while j < n:
            if text[j] == close:
                spans.append((i, j + 1))
                i = j + 1
                found = True
                break
            j += 1
        if not found:
            i += 1
    return spans


def position_inside_quotes(text: str, pos: int) -> bool:
    """True if ``pos`` falls strictly inside a quoted span (not on boundaries)."""
    if pos <= 0 or pos >= len(text):
        return False
    for start, end in quoted_char_spans(text):
        if start < pos < end:
            return True
    return False


def line_has_unbalanced_quotes(text: str) -> bool:
    """True when a screen line leaves an unmatched opening/closing quote."""
    spans = quoted_char_spans(text)
    covered: set[int] = set()
    for start, end in spans:
        covered.update(range(start, end))
    for i, ch in enumerate(text):
        if ch in QUOTE_OPENERS or ch in QUOTE_CLOSERS:
            if i not in covered:
                return True
    return False


def split_orphan_quote_boundary(line1: str, line2: str) -> bool:
    """True if the line break leaves dangling quotes on either side."""
    a = line1.strip()
    b = line2.strip()
    if not a or not b:
        return False
    return line_has_unbalanced_quotes(a) or line_has_unbalanced_quotes(b)


def _balanced_quote_pack_break(text: str, pos: int) -> bool:
    """Reject breaks that orphan opening/closing quotes on either side."""
    if pos <= 0 or pos >= len(text):
        return False
    left = text[:pos].strip()
    right = text[pos:].strip()
    if not left or not right:
        return False
    return not line_has_unbalanced_quotes(left) and not line_has_unbalanced_quotes(right)


def quote_aware_break_positions(text: str) -> list[int]:
    """Preferred break indices: before open-quote and after close-quote."""
    positions: list[int] = []
    for start, end in quoted_char_spans(text):
        if start > 0:
            positions.append(start)
        if end < len(text):
            positions.append(end)
    for i, ch in enumerate(text):
        if ch in QUOTE_OPENERS and ch not in QUOTE_CLOSERS and i > 0:
            positions.append(i)
        elif ch in QUOTE_CLOSERS and ch not in QUOTE_OPENERS and i + 1 < len(text):
            positions.append(i + 1)
    seen: set[int] = set()
    out: list[int] = []
    for pos in positions:
        if 0 < pos < len(text) and pos not in seen:
            seen.add(pos)
            out.append(pos)
    return sorted(out)


def preferred_semantic_break_positions(text: str) -> list[int]:
    """
    Preferred break indices outside quotes/parentheses: em-dash, dunhao, quote/paren edges.
    """
    from videoaudiotext.subtitle.protected_spans import position_inside_protected_span

    positions: list[int] = []
    n = len(text)
    i = 0
    while i < n:
        if (
            i + 2 <= n
            and text[i : i + 2] == "——"
            and not position_inside_protected_span(text, i + 2)
        ):
            if not position_inside_protected_span(text, i):
                positions.append(i)
            positions.append(i + 2)
            i += 2
            continue
        ch = text[i]
        if ch == "。" and i + 1 < n and not position_inside_protected_span(text, i + 1):
            positions.append(i + 1)
        if ch == "，" and i + 1 < n and not position_inside_protected_span(text, i + 1):
            positions.append(i + 1)
        if ch in "：:" and i + 1 < n and not position_inside_protected_span(text, i + 1):
            positions.append(i + 1)
        if ch == "、" and i + 1 < n and not position_inside_protected_span(text, i + 1):
            positions.append(i + 1)
        if ch in "；;" and i + 1 < n and not position_inside_protected_span(text, i + 1):
            positions.append(i + 1)
        i += 1
    from videoaudiotext.subtitle.protected_spans import protected_aware_break_positions

    positions.extend(protected_aware_break_positions(text))
    seen: set[int] = set()
    out: list[int] = []
    for pos in positions:
        if 0 < pos < n and pos not in seen:
            seen.add(pos)
            out.append(pos)
    return sorted(out)


def quote_line_overflow_allowance(max_line: int) -> int:
    """Extra core-char budget when a line must keep an atomic quote span intact."""
    raw = os.environ.get("SUBTITLE_QUOTE_LINE_OVERFLOW", "").strip()
    if raw:
        return max(2, int(float(raw)))
    return max(4, int(round(max_line * 0.28)))


def _non_quote_core(text: str) -> str:
    """Characters outside quoted spans, excluding punctuation/space."""
    spans = quoted_char_spans(text)
    if not spans:
        return text.strip()
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        if cursor < start:
            out.append(text[cursor:start])
        cursor = end
    if cursor < len(text):
        out.append(text[cursor:])
    joined = "".join(out).strip()
    punct = "，。、；：！？.!?; \t\n\r\u3000—"
    return "".join(ch for ch in joined if ch not in punct)


def is_quote_only_fragment(text: str) -> bool:
    """True when visible content is essentially one or more quoted spans."""
    t = text.strip()
    if not t or not quoted_char_spans(t):
        return False
    return _non_quote_core(t) == ""


def is_em_dash_only_line(text: str) -> bool:
    """True when a screen line is only em dash(es), which must not stand alone."""
    t = text.strip()
    return bool(t) and all(ch in "—\u2014" for ch in t)


def _starts_with_em_dash(text: str) -> bool:
    t = text.strip()
    return t.startswith("——") or t.startswith("—") or t.startswith("\u2014")


def _ends_with_em_dash(text: str) -> bool:
    t = text.strip()
    return t.endswith("——") or t.endswith("—") or t.endswith("\u2014")


def _peel_leading_em_dash(text: str) -> tuple[str, str]:
    """Return (dash_prefix, rest) when text opens with ——/—."""
    t = text.strip()
    if t.startswith("——"):
        return "——", t[2:].strip()
    if t and t[0] in "—\u2014":
        return t[0], t[1:].strip()
    return "", t


def attach_em_dash_orphan_lines(
    lines: list[str],
    max_line: int,
    *,
    content_rect=None,
) -> list[str]:
    """合并仅含破折号的 orphan 行，优先 —— 附前句，否则附后句。"""
    from videoaudiotext.config.subtitle import progressive_line_fits_hard_width

    work = [ln.strip() for ln in lines if ln.strip()]
    if len(work) <= 1:
        return work

    changed = True
    for _ in range(max(8, len(work) * 2)):
        if not changed:
            break
        changed = False
        for i, ln in enumerate(list(work)):
            if not is_em_dash_only_line(ln):
                continue
            if i > 0:
                merged = work[i - 1] + ln
                if progressive_line_fits_hard_width(merged, content_rect=content_rect):
                    work[i - 1 : i + 1] = [merged]
                    changed = True
                    break
            if i + 1 < len(work):
                merged = ln + work[i + 1]
                if progressive_line_fits_hard_width(merged, content_rect=content_rect):
                    work[i : i + 2] = [merged]
                    changed = True
                    break
            if i > 0 and i + 1 < len(work):
                repacked = _force_progressive_split(
                    work[i - 1] + ln + work[i + 1],
                    max_line,
                    content_rect=content_rect,
                )
                if len(repacked) >= 2:
                    work[i - 1 : i + 2] = repacked
                    changed = True
                    break
            if i > 0:
                work[i - 1 : i + 1] = [work[i - 1] + ln]
                changed = True
                break
            if i + 1 < len(work):
                work[i : i + 2] = [ln + work[i + 1]]
                changed = True
                break
    return work


def _atomic_unit_end(text: str, start: int = 0) -> int:
    """Return end index of the next atomic unit (—— kept intact)."""
    if start + 2 <= len(text) and text[start : start + 2] == "——":
        return start + 2
    if start < len(text):
        return start + 1
    return start


def cue_starts_with_open_quote(text: str) -> bool:
    t = text.strip()
    return bool(t) and t[0] in QUOTE_OPENERS


def continues_after_closed_quote(left: str, right: str) -> bool:
    """Left ends with a complete quote (+ optional 的/之); right continues the phrase."""
    left = left.strip()
    right = right.strip()
    if not left or not right or cue_starts_with_open_quote(right):
        return False
    spans = quoted_char_spans(left)
    if not spans:
        return False
    tail = left[spans[-1][1] :].strip()
    if tail and not all(ch in "的之等" for ch in tail):
        return False
    return right[0] not in QUOTE_OPENERS and right[0] not in QUOTE_CLOSERS


def is_short_tail_fragment(text: str, max_core: int = 4) -> bool:
    """Short non-quote fragment that often completes the previous screen line."""
    from videoaudiotext.subtitle.display import _core_char_len

    t = text.strip()
    if not t or cue_starts_with_open_quote(t) or is_quote_only_fragment(t):
        return False
    if t[0] in "，。、；：！？.!?;":
        return False
    core = _core_char_len(t)
    return 1 <= core <= max_core


def is_single_char_orphan(text: str) -> bool:
    """True when a screen line is a lone character that should not stand alone."""
    return is_too_short_screen_line(text) and not is_quote_only_fragment(text)


def progressive_lines_combine_fits(
    left: str,
    right: str,
    *,
    content_rect=None,
    max_line: int | None = None,
) -> bool:
    """合并后单行是否仍不超 pack 字数与 90% 像素硬宽。"""
    from videoaudiotext.config.subtitle import progressive_screen_line_needs_split
    from videoaudiotext.subtitle.display import _core_char_len, _normalize_subtitle_text

    combined = _normalize_subtitle_text(f"{left.strip()}{right.strip()}")
    if not combined:
        return False
    if max_line is not None and _core_char_len(combined) > max_line:
        return False
    return not progressive_screen_line_needs_split(combined, content_rect=content_rect)


def is_too_short_screen_line(text: str) -> bool:
    """屏显行少于 progressive_min_screen_chars（默认单字禁止）。"""
    from videoaudiotext.config.subtitle import progressive_min_screen_chars
    from videoaudiotext.subtitle.display import _core_char_len

    t = text.strip()
    if not t:
        return True
    if is_em_dash_only_line(t):
        return True
    if is_quote_only_fragment(t):
        return True
    return _core_char_len(t) < progressive_min_screen_chars()


def cue_starts_with_de_orphan(text: str) -> bool:
    """True when a screen line begins with the structural particle 的."""
    t = text.strip()
    return bool(t) and t[0] == "的"


def left_ends_with_de_orphan(text: str) -> bool:
    """True when a screen line ends with 的 and likely continues on the next line."""
    t = text.strip().rstrip("—")
    if not t or t[-1] != "的":
        return False
    return t.rstrip("，、；：").endswith("的")


def right_heads_into_de_phrase(text: str, *, window: int = 3) -> bool:
    """True when the line opens a 的-phrase whose head continued on the previous line."""
    t = text.strip()
    if not t:
        return False
    if t[0] == "的":
        return True
    pos = t.find("的")
    return 0 <= pos <= window


def left_ends_with_copula_orphan(text: str) -> bool:
    """True when a line ends with 是 and the predicate continues on the next line."""
    t = text.strip().rstrip("—")
    return bool(t) and t.rstrip("，、；：").endswith("是")


def right_starts_with_copula_orphan(text: str) -> bool:
    """True when a screen line begins with copula 是 (X|是Y)."""
    t = text.strip()
    return bool(t) and t[0] == "是"


def right_heads_into_adv_predicate(text: str) -> bool:
    """True when the line opens with 仍在/还是/正在-style predicates."""
    t = text.strip()
    return t.startswith(("仍在", "还是", "正在", "已是", "仍是"))


def is_intentional_comma_screen_break(left: str, right: str, max_line: int) -> bool:
    """两行已在逗号处切分且各自不超屏 → 勿因孤儿合并规则并回一行。"""
    from videoaudiotext.subtitle.display import _core_char_len

    left = left.strip()
    right = right.strip()
    if not left.endswith("，") or not right:
        return False
    return _core_char_len(left) <= max_line and _core_char_len(right) <= max_line


def progressive_screen_lines_should_stay_split(
    left: str,
    right: str,
    max_line: int,
    *,
    content_rect=None,
) -> bool:
    """两行各自已不超上限 → 勿为修 orphan 并回（含逗号分步去掉尾逗号的情况）。"""
    from videoaudiotext.config.subtitle import progressive_orphan_merge_line_limit
    from videoaudiotext.subtitle.display import _core_char_len

    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    if is_too_short_screen_line(left) or is_too_short_screen_line(right):
        return False
    effective = progressive_orphan_merge_line_limit(max_line, content_rect=content_rect)
    lc, rc = _core_char_len(left), _core_char_len(right)
    if lc <= effective and rc <= effective and lc + rc > effective:
        return True
    return is_intentional_comma_screen_break(left, right, effective)


def should_merge_de_orphan_lines(
    left: str,
    right: str,
    max_line: int,
    *,
    content_rect=None,
) -> bool:
    """Merge when 的/是 would dangle at a line boundary (X的|Y, X|的Y, X是|Y)."""
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    if progressive_screen_lines_should_stay_split(
        left, right, max_line, content_rect=content_rect
    ):
        return False
    if not (
        cue_starts_with_de_orphan(right)
        or left_ends_with_de_orphan(left)
        or right_heads_into_de_phrase(right)
        or right_heads_into_adv_predicate(right)
        or left_ends_with_copula_orphan(left)
        or right_starts_with_copula_orphan(right)
    ):
        return False
    if split_orphan_quote_boundary(left, right):
        if not (
            is_too_short_screen_line(left) or is_too_short_screen_line(right)
        ):
            return False
    return progressive_lines_combine_fits(left, right, content_rect=content_rect)


def should_merge_short_tail_lines(
    left: str,
    right: str,
    max_line: int,
    *,
    content_rect=None,
) -> bool:
    """Merge a short tail onto a substantial previous line when width allows."""
    from videoaudiotext.subtitle.display import _core_char_len

    left = left.strip()
    right = right.strip()
    if not left or not right or not is_short_tail_fragment(right):
        return False
    right_core = _core_char_len(right)
    min_left = 4 if right_core == 1 else 8
    if _core_char_len(left) < min_left:
        return False
    if progressive_screen_lines_should_stay_split(
        left, right, max_line, content_rect=content_rect
    ):
        return False
    return progressive_lines_combine_fits(left, right, content_rect=content_rect)


def should_merge_single_char_orphan_lines(
    left: str,
    right: str,
    max_line: int,
    *,
    content_rect=None,
) -> bool:
    """单字 orphan（如「是」「的」）在像素宽允许时并入邻行。"""
    from videoaudiotext.subtitle.protected_spans import split_orphan_protected_boundary

    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    if not (is_single_char_orphan(right) or is_single_char_orphan(left)):
        return False
    if progressive_screen_lines_should_stay_split(
        left, right, max_line, content_rect=content_rect
    ):
        return False
    if split_orphan_quote_boundary(left, right) or split_orphan_protected_boundary(
        left, right
    ):
        if not (
            is_too_short_screen_line(left) or is_too_short_screen_line(right)
        ):
            return False
    return progressive_lines_combine_fits(left, right, content_rect=content_rect)


def _peel_unit_from_line_end(line: str) -> tuple[str, str]:
    """从行尾剥一个 jieba 词（或单字）给下一行。"""
    t = line.strip()
    if not t:
        return "", ""
    try:
        import jieba

        words = [w for w in jieba.cut(t) if w]
        if len(words) >= 2:
            tail = words[-1]
            head = "".join(words[:-1])
            if head and tail:
                return head, tail
    except ImportError:
        pass
    return _peel_char_from_line_end(line)


def _borrow_unit_from_line_start(line: str) -> tuple[str, str]:
    """从下一行开头借一个 jieba 词（或单字）给上一行。"""
    t = line.strip()
    if not t:
        return "", ""
    try:
        import jieba

        words = [w for w in jieba.cut(t) if w]
        if len(words) >= 2:
            head = words[0]
            rest = "".join(words[1:])
            return head, rest
    except ImportError:
        pass
    return _borrow_char_from_line_start(line)


def _peel_char_from_line_end(line: str) -> tuple[str, str]:
    """从行尾剥一个字符给下一行（避免单字 orphan）。"""
    t = line.strip()
    if len(t) <= 1:
        return "", t
    return t[:-1], t[-1]


def _borrow_char_from_line_start(line: str) -> tuple[str, str]:
    """从下一行开头借一个字符给上一行（避免单字 orphan，不放宽像素宽）。"""
    t = line.strip()
    if len(t) <= 1:
        return t, ""
    return t[0], t[1:]


def rebalance_short_tail_pair(
    left: str,
    right: str,
    *,
    content_rect=None,
) -> tuple[str, str] | list[str]:
    """
    将 left|right 重平衡为至少 min_screen 字/行（严格不超像素硬宽）。
    仅当合并后仍 fit 时才合并为一行。
    """
    from videoaudiotext.config.subtitle import progressive_min_screen_chars
    from videoaudiotext.subtitle.display import _core_char_len

    min_scr = progressive_min_screen_chars()
    work_l, work_r = left.strip(), right.strip()
    if _core_char_len(work_r) >= min_scr and _core_char_len(work_l) >= min_scr:
        return work_l, work_r
    if progressive_lines_combine_fits(work_l, work_r, content_rect=content_rect):
        return [work_l + work_r]
    while _core_char_len(work_r) < min_scr and _core_char_len(work_l) > 1:
        work_l, unit = _peel_unit_from_line_end(work_l)
        if not unit:
            break
        work_r = unit + work_r
    if _core_char_len(work_r) >= min_scr and _core_char_len(work_l) >= min_scr:
        return work_l, work_r
    while _core_char_len(work_l) < min_scr and _core_char_len(work_r) > 1:
        unit, work_r = _borrow_unit_from_line_start(work_r)
        if not unit:
            break
        work_l = work_l + unit
    if _core_char_len(work_r) >= min_scr and _core_char_len(work_l) >= min_scr:
        return work_l, work_r
    if progressive_lines_combine_fits(work_l, work_r, content_rect=content_rect):
        return [work_l + work_r]
    return work_l, work_r


def eliminate_single_char_screen_lines(
    lines: list[str],
    max_line: int,
    *,
    content_rect=None,
) -> list[str]:
    """最终兜底：不允许 progressive 屏显出现单字（或 quote-only）行。"""
    from videoaudiotext.config.subtitle import progressive_min_screen_chars
    from videoaudiotext.subtitle.display import _core_char_len

    min_scr = progressive_min_screen_chars()
    work = [ln.strip() for ln in lines if ln.strip()]
    if len(work) <= 1:
        return work

    changed = True
    for _ in range(max(8, len(work) * 3)):
        if not changed:
            break
        changed = False
        for i in range(len(work)):
            if not is_too_short_screen_line(work[i]):
                continue
            if i > 0:
                left, right = work[i - 1], work[i]
                if should_merge_single_char_orphan_lines(
                    left, right, max_line, content_rect=content_rect
                ) or should_merge_quote_orphan_lines(
                    left, right, max_line, content_rect=content_rect
                ):
                    work[i - 1 : i + 1] = [left + right]
                    changed = True
                    break
                rebalanced = rebalance_short_tail_pair(
                    left, right, content_rect=content_rect
                )
                if isinstance(rebalanced, list):
                    work[i - 1 : i + 1] = rebalanced
                    changed = True
                    break
                nl, nr = rebalanced
                if _core_char_len(nr) >= min_scr:
                    if _core_char_len(nl) >= min_scr:
                        work[i - 1 : i + 1] = [nl, nr]
                    elif progressive_lines_combine_fits(nl, nr, content_rect=content_rect):
                        work[i - 1 : i + 1] = [nl + nr]
                    changed = True
                    break
            if i + 1 < len(work):
                left, right = work[i], work[i + 1]
                if should_merge_single_char_orphan_lines(
                    left, right, max_line, content_rect=content_rect
                ) or should_merge_quote_orphan_lines(
                    left, right, max_line, content_rect=content_rect
                ):
                    work[i : i + 2] = [left + right]
                    changed = True
                    break
                rebalanced = rebalance_short_tail_pair(
                    left, right, content_rect=content_rect
                )
                if isinstance(rebalanced, list):
                    work[i : i + 2] = rebalanced
                    changed = True
                    break
                nl, nr = rebalanced
                if _core_char_len(nl) >= min_scr:
                    if _core_char_len(nr) >= min_scr:
                        work[i : i + 2] = [nl, nr]
                    elif progressive_lines_combine_fits(nl, nr, content_rect=content_rect):
                        work[i : i + 2] = [nl + nr]
                    changed = True
                    break
    return work


def should_merge_quote_orphan_lines(
    left: str,
    right: str,
    max_line: int,
    *,
    content_rect=None,
) -> bool:
    """Whether two screen/progressive lines should stay on one timed cue."""
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    if progressive_screen_lines_should_stay_split(
        left, right, max_line, content_rect=content_rect
    ):
        return False
    if not (
        is_quote_only_fragment(right)
        or cue_starts_with_open_quote(right)
        or continues_after_closed_quote(left, right)
        or should_merge_short_tail_lines(
            left, right, max_line, content_rect=content_rect
        )
        or should_merge_de_orphan_lines(
            left, right, max_line, content_rect=content_rect
        )
        or should_merge_single_char_orphan_lines(
            left, right, max_line, content_rect=content_rect
        )
    ):
        return False
    if split_orphan_quote_boundary(left, right):
        if not (
            is_too_short_screen_line(left)
            or is_too_short_screen_line(right)
            or is_quote_only_fragment(right)
        ):
            return False
    return progressive_lines_combine_fits(left, right, content_rect=content_rect)


def merge_quote_orphan_screen_lines(lines: list[str], max_line: int) -> list[str]:
    """Pull quote/de orphan lines onto neighbors when width allows."""
    if len(lines) <= 1:
        return lines
    work = [ln.strip() for ln in lines if ln.strip()]
    changed = True
    for _ in range(len(work)):
        if not changed or len(work) <= 1:
            break
        changed = False
        for i in range(1, len(work)):
            left, right = work[i - 1], work[i]
            if should_merge_quote_orphan_lines(left, right, max_line):
                work[i - 1 : i + 1] = [left + right]
                changed = True
                break
    return work


def _break_between_adjacent_quotes(left: str, right: str) -> bool:
    """True when breaking between two back-to-back quoted spans (""…"")."""
    left = left.rstrip()
    right = right.lstrip()
    if not left or not right:
        return False
    if not left.endswith(tuple(QUOTE_CLOSERS)):
        return False
    if right[0] not in QUOTE_OPENERS:
        return False
    spans = quoted_char_spans(left)
    return bool(spans and spans[-1][1] == len(left))


def _line_fits_pixel_limit(text: str, *, content_rect=None) -> bool:
    from videoaudiotext.config.subtitle import progressive_line_fits_hard_width

    return progressive_line_fits_hard_width(text, content_rect=content_rect)


def _pixel_safe_break_positions(text: str) -> list[int]:
    """Candidate breaks for pixel reflow: quote edges, semantics, jieba."""
    from videoaudiotext.subtitle.display import _is_break_allowed, _jieba_word_end_positions

    positions: set[int] = set()
    positions.update(quote_aware_break_positions(text))
    positions.update(preferred_semantic_break_positions(text))
    positions.update(_jieba_word_end_positions(text))
    out: list[int] = []
    for pos in sorted(positions, reverse=True):
        if (
            0 < pos < len(text)
            and _valid_pack_break(text, pos)
            and _is_break_allowed(text, pos)
        ):
            out.append(pos)
    return out


def _try_peel_prefix_before_open_quote(text: str) -> tuple[str, str] | None:
    """Peel 1–3 chars before opening quote so the quoted tail can reflow."""
    from videoaudiotext.subtitle.display import _core_char_len

    t = text.strip()
    for i, ch in enumerate(t):
        if ch not in QUOTE_OPENERS or i <= 0:
            continue
        prefix = t[:i].strip()
        rest = t[i:].strip()
        if not prefix or not rest or _core_char_len(prefix) > 3:
            continue
        return prefix, rest
    return None


def _single_quote_span_inner(text: str) -> str | None:
    """If text is exactly one quoted span, return its inner content."""
    t = text.strip()
    spans = quoted_char_spans(t)
    if len(spans) != 1:
        return None
    start, end = spans[0]
    if start != 0 or end != len(t):
        return None
    inner = t[start + 1 : end - 1].strip()
    return inner or None


def _force_hard_px_split(
    text: str,
    *,
    content_rect=None,
) -> list[str]:
    """按 90% 硬宽强制切分（语义/jieba 失败时的兜底，保证不溢出）。"""
    from videoaudiotext.config.subtitle import (
        estimate_progressive_text_width_px,
        progressive_hard_max_width_px,
        progressive_line_fits_hard_width,
    )
    from videoaudiotext.subtitle.display import _is_break_allowed

    t = text.strip()
    if not t:
        return []
    if progressive_line_fits_hard_width(t, content_rect=content_rect):
        return [t]

    hard_px = progressive_hard_max_width_px(content_rect=content_rect)
    positions: list[int] = []
    for pos in sorted(
        set(quote_aware_break_positions(t))
        | set(preferred_semantic_break_positions(t))
        | set(range(1, len(t))),
        reverse=True,
    ):
        if pos <= 0 or pos >= len(t):
            continue
        left = t[:pos].strip()
        if not left:
            continue
        if not _is_break_allowed(t, pos):
            continue
        if pos >= 1 and pos < len(t) and t[pos - 1] == "—" and t[pos] == "—":
            continue
        if estimate_progressive_text_width_px(left) <= hard_px + 0.5:
            positions.append(pos)
            break

    if not positions:
        for pos in range(len(t) - 1, 0, -1):
            if pos >= 1 and pos < len(t) and t[pos - 1] == "—" and t[pos] == "—":
                continue
            left = t[:pos].strip()
            if left and estimate_progressive_text_width_px(left) <= hard_px + 0.5:
                positions.append(pos)
                break

    if not positions:
        unit_end = _atomic_unit_end(t, 0)
        return [t[:unit_end]] + _force_hard_px_split(t[unit_end:], content_rect=content_rect)

    pos = positions[0]
    left = t[:pos].strip()
    right = t[pos:].strip()
    out: list[str] = []
    if left:
        out.append(left)
    if right:
        out.extend(_force_hard_px_split(right, content_rect=content_rect))
    return out


def _force_progressive_split(
    text: str,
    max_line: int,
    *,
    content_rect=None,
) -> list[str]:
    """软字数与 90% 硬宽双约束下的强制切分（装箱兜底）。"""
    from videoaudiotext.config.subtitle import (
        progressive_line_fits_hard_width,
    )
    from videoaudiotext.subtitle.display import _core_char_len, _is_break_allowed

    t = text.strip()
    if not t:
        return []
    if _core_char_len(t) <= max_line and progressive_line_fits_hard_width(
        t, content_rect=content_rect
    ):
        return [t]

    positions: list[int] = []
    for pos in sorted(
        set(quote_aware_break_positions(t))
        | set(preferred_semantic_break_positions(t))
        | set(range(1, len(t))),
        reverse=True,
    ):
        if pos <= 0 or pos >= len(t):
            continue
        left = t[:pos].strip()
        if not left:
            continue
        if not _is_break_allowed(t, pos):
            continue
        if _core_char_len(left) > max_line:
            continue
        if not progressive_line_fits_hard_width(left, content_rect=content_rect):
            continue
        positions.append(pos)
        break

    if not positions:
        for pos in range(len(t) - 1, 0, -1):
            left = t[:pos].strip()
            if not left:
                continue
            if _core_char_len(left) > max_line:
                continue
            if progressive_line_fits_hard_width(left, content_rect=content_rect):
                positions.append(pos)
                break

    if positions:
        pos = positions[0]
        left = t[:pos].strip()
        right = t[pos:].strip()
        out: list[str] = []
        if left:
            out.append(left)
        if right:
            out.extend(
                _force_progressive_split(right, max_line, content_rect=content_rect)
            )
        return out

    if not progressive_line_fits_hard_width(t, content_rect=content_rect):
        return _force_hard_px_split(t, content_rect=content_rect)

    unit_end = _atomic_unit_end(t, 0)
    pos = max(unit_end, max_line)
    while pos > unit_end and _core_char_len(t[:pos].strip()) > max_line:
        pos -= 1
    if pos <= 0:
        pos = unit_end
    left = t[:pos].strip()
    right = t[pos:].strip()
    out: list[str] = []
    if left:
        out.append(left)
    if right:
        out.extend(_force_progressive_split(right, max_line, content_rect=content_rect))
    return out


def _split_line_for_pixel_limit(
    text: str,
    pixel_limit: int,
    *,
    content_rect=None,
) -> list[str]:
    """Recursively split until each screen line fits 90% pixel width."""
    from videoaudiotext.config.subtitle import progressive_min_screen_chars
    from videoaudiotext.subtitle.display import _core_char_len

    t = text.strip()
    if not t:
        return []
    if _line_fits_pixel_limit(t, content_rect=content_rect):
        return [t]

    min_scr = progressive_min_screen_chars()
    peeled = _try_peel_prefix_before_open_quote(t)
    if peeled:
        prefix, rest = peeled
        rest_parts = _split_line_for_pixel_limit(
            rest, pixel_limit, content_rect=content_rect
        )
        if rest_parts and all(
            _line_fits_pixel_limit(p, content_rect=content_rect) for p in rest_parts
        ):
            if _core_char_len(prefix) >= min_scr:
                return [prefix, *rest_parts]
            return rest_parts

    for pos in _pixel_safe_break_positions(t):
        left = t[:pos].strip()
        right = t[pos:].strip()
        if not left or not right:
            continue
        left_parts = (
            _split_line_for_pixel_limit(left, pixel_limit, content_rect=content_rect)
            if not _line_fits_pixel_limit(left, content_rect=content_rect)
            else [left]
        )
        right_parts = (
            _split_line_for_pixel_limit(right, pixel_limit, content_rect=content_rect)
            if not _line_fits_pixel_limit(right, content_rect=content_rect)
            else [right]
        )
        parts = left_parts + right_parts
        if parts and all(
            _line_fits_pixel_limit(p, content_rect=content_rect) for p in parts
        ):
            return parts

    inner = _single_quote_span_inner(t)
    if inner and _line_fits_pixel_limit(inner, content_rect=content_rect):
        return [inner]

    return _force_hard_px_split(t, content_rect=content_rect)


def _apply_pixel_limit_to_screen_lines(
    lines: list[str],
    pixel_limit: int,
    *,
    content_rect=None,
) -> list[str]:
    """Second pass: enforce pixel width, including adjacent quote-pair splits."""
    from videoaudiotext.config.subtitle import progressive_min_screen_chars
    from videoaudiotext.subtitle.display import _core_char_len

    min_scr = progressive_min_screen_chars()
    out: list[str] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if _line_fits_pixel_limit(ln, content_rect=content_rect):
            out.append(ln)
            continue
        peeled = _try_peel_prefix_before_open_quote(ln)
        if peeled:
            prefix, rest = peeled
            if out and _line_fits_pixel_limit(
                out[-1] + prefix, content_rect=content_rect
            ):
                out[-1] += prefix
                ln = rest
            elif not _line_fits_pixel_limit(ln, content_rect=content_rect):
                rest_parts = _split_line_for_pixel_limit(
                    rest, pixel_limit, content_rect=content_rect
                )
                if rest_parts and all(
                    _line_fits_pixel_limit(p, content_rect=content_rect)
                    for p in rest_parts
                ):
                    out.extend(rest_parts)
                    continue
        parts = _split_line_for_pixel_limit(
            ln, pixel_limit, content_rect=content_rect
        )
        if len(parts) >= 2 and _core_char_len(parts[0]) <= 3 and out:
            merged = out[-1] + parts[0]
            if _line_fits_pixel_limit(merged, content_rect=content_rect):
                out[-1] = merged
                parts = parts[1:]
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if (
                out
                and _core_char_len(part) < min_scr
                and _line_fits_pixel_limit(out[-1] + part, content_rect=content_rect)
            ):
                out[-1] += part
            else:
                out.append(part)
    return out


def _valid_pack_break(text: str, pos: int) -> bool:
    from videoaudiotext.subtitle.parentheses import PAREN_CLOSERS, PAREN_OPENERS
    from videoaudiotext.subtitle.protected_spans import (
        position_inside_protected_span,
        split_orphan_protected_boundary,
    )

    if pos <= 0 or pos >= len(text):
        return False
    if position_inside_protected_span(text, pos):
        return False
    left = text[:pos].strip()
    right = text[pos:].strip()
    if not left or not right:
        return False
    if split_orphan_protected_boundary(left, right):
        return False
    if not _balanced_quote_pack_break(text, pos):
        return False
    if left[-1] in PAREN_OPENERS:
        return False
    if left[-1] in QUOTE_OPENERS:
        spans = quoted_char_spans(left)
        if not (spans and spans[-1][1] == len(left)):
            return False
    if right[0] in QUOTE_CLOSERS or right[0] in PAREN_CLOSERS:
        return False
    if right[0] in PAREN_OPENERS:
        return False
    if right[0] in QUOTE_OPENERS:
        if not _break_between_adjacent_quotes(left, right):
            return False
    if right[0] == "的":
        from videoaudiotext.subtitle.display import _jieba_word_end_positions

        if not (
            left.rstrip().endswith(tuple(QUOTE_CLOSERS))
            or continues_after_closed_quote(left, right)
            or pos in _jieba_word_end_positions(text)
        ):
            return False
    if right_heads_into_de_phrase(right):
        from videoaudiotext.subtitle.display import _jieba_word_end_positions

        if not (
            continues_after_closed_quote(left, right)
            or pos in _jieba_word_end_positions(text)
        ):
            return False
    if right_heads_into_adv_predicate(right):
        return False
    if left.rstrip("，、；：").endswith("的"):
        return False
    if left.rstrip("，、；：").endswith("是"):
        return False
    from videoaudiotext.subtitle.breaks import split_crosses_compound

    if split_crosses_compound(left, right):
        return False
    return True


def _opening_clause_core_len(clause: str) -> int:
    from videoaudiotext.subtitle.display import _core_char_len

    return _core_char_len(clause.strip().rstrip("，"))


def _merge_short_opener_clauses(
    clauses: list[str],
    *,
    max_line: int | None = None,
) -> list[str]:
    """Merge a short comma-ending opener clause with the following clause."""
    from videoaudiotext.config import COMMA_SHORT_OPENING_MAX_CHARS
    from videoaudiotext.subtitle.display import _core_char_len

    if len(clauses) < 2:
        return clauses
    out: list[str] = []
    i = 0
    while i < len(clauses):
        cur = clauses[i].strip()
        if (
            i + 1 < len(clauses)
            and cur.endswith("，")
            and 0 < _opening_clause_core_len(cur) <= COMMA_SHORT_OPENING_MAX_CHARS
        ):
            nxt = clauses[i + 1].strip()
            combined = cur + nxt
            if max_line is not None and _core_char_len(combined) > max_line + 2:
                out.append(cur)
                i += 1
                continue
            out.append(combined)
            i += 2
            continue
        out.append(cur)
        i += 1
    return out


def _is_post_period_screen_opener(clause: str) -> bool:
    from videoaudiotext.config import COMMA_SHORT_OPENING_MAX_CHARS

    clause = clause.strip()
    if not clause.endswith("，"):
        return False
    body = clause.rstrip("，").strip()
    if "。" not in body:
        return False
    body = body.rsplit("。", 1)[-1].strip()
    core = _opening_clause_core_len(body + "，")
    return 0 < core <= COMMA_SHORT_OPENING_MAX_CHARS + 2


def _merge_post_period_opener_clauses(clauses: list[str]) -> list[str]:
    """Merge «基因。而今天的佛山，»-style short bridge with the next clause."""
    if len(clauses) < 2:
        return clauses
    out: list[str] = []
    i = 0
    while i < len(clauses):
        cur = clauses[i].strip()
        if i + 1 < len(clauses) and _is_post_period_screen_opener(cur):
            out.append(cur + clauses[i + 1].strip())
            i += 2
            continue
        out.append(cur)
        i += 1
    return out


def _top_level_clauses(text: str) -> list[str]:
    """First-level split at comma or em-dash outside quotes/parentheses."""
    from videoaudiotext.subtitle.protected_spans import position_inside_protected_span

    chunks: list[str] = []
    buf = ""
    i = 0
    n = len(text)
    while i < n:
        if (
            i + 2 <= n
            and text[i : i + 2] == "——"
            and not position_inside_protected_span(text, i)
        ):
            if buf.strip():
                chunks.append(buf)
            buf = ""
            i += 2
            continue
        ch = text[i]
        if ch == "。" and not position_inside_protected_span(text, i):
            buf += ch
            i += 1
            if buf.strip():
                chunks.append(buf)
            buf = ""
            continue
        if ch == "，" and not position_inside_protected_span(text, i):
            buf += ch
            i += 1
            if buf.strip():
                chunks.append(buf)
            buf = ""
            continue
        if ch in "；;" and not position_inside_protected_span(text, i):
            buf += ch
            i += 1
            if buf.strip():
                chunks.append(buf)
            buf = ""
            continue
        if ch in "：:" and not position_inside_protected_span(text, i):
            buf += ch
            i += 1
            if buf.strip():
                chunks.append(buf)
            buf = ""
            continue
        buf += ch
        i += 1
    if buf.strip():
        chunks.append(buf)
    return chunks if chunks else [text]


def _breaks_after_primary_punctuation(left: str) -> bool:
    t = left.rstrip()
    return bool(t) and t[-1] in "，、；：。！？.!?;:"


def _progressive_clause_display(text: str) -> str:
    """标点分步屏显：子句末尾逗号/顿号/破折号不带到下一屏（句末。！？保留）。"""
    t = text.strip()
    while t and t[-1] in "，、":
        t = t[:-1].strip()
    while t.endswith("——"):
        t = t[:-2].strip()
    while t.endswith("—"):
        t = t[:-1].strip()
    return t


def all_punctuation_clauses_fit_screen(
    clauses: list[str],
    *,
    max_line: int | None = None,
    content_rect=None,
) -> bool:
    """各标点子句单独不超 90% 硬宽 → 可在标点处语义切分。"""
    from videoaudiotext.config.subtitle import (
        progressive_layout_limits,
        progressive_line_fits_hard_width,
        progressive_min_screen_chars,
    )
    from videoaudiotext.subtitle.display import _core_char_len

    if len(clauses) < 2:
        return False
    limits = progressive_layout_limits(content_rect=content_rect)
    pack = int(limits["pack_chars"])
    soft = max_line if max_line is not None else int(limits["soft_chars"])
    effective = max(1, min(int(soft), pack))
    min_clause = max(1, min(progressive_min_screen_chars(), int(limits["pixel_chars"])))
    for clause in clauses:
        shown = _progressive_clause_display(clause)
        if not shown:
            return False
        n = _core_char_len(shown)
        if n > effective:
            return False
        if not progressive_line_fits_hard_width(shown, content_rect=content_rect):
            return False
        if n < min_clause:
            return False
    return True


def try_progressive_punctuation_lines(
    text: str,
    max_line: int,
    *,
    pack_limit: int | None = None,
    content_rect=None,
) -> list[str] | None:
    """
    标点优先：在，；。：等一级标点处切分，每段不超上限则直接返回。
    无合适标点切分点时返回 None，由后续语义/jieba 装箱兜底。
    """
    from videoaudiotext.config.subtitle import (
        progressive_line_fits_hard_width,
        progressive_text_needs_split,
    )
    from videoaudiotext.subtitle.display import _core_char_len, _normalize_subtitle_text

    plain = _normalize_subtitle_text(text.replace("\\N", "\n"))
    if not plain:
        return []
    limit = pack_limit if pack_limit is not None else max_line
    if not progressive_text_needs_split(plain, max_line=max_line, content_rect=content_rect):
        return [plain]

    clauses = _merge_post_period_opener_clauses(
        _merge_short_opener_clauses(_top_level_clauses(plain), max_line=limit)
    )
    if len(clauses) < 2:
        return None
    if not all_punctuation_clauses_fit_screen(
        clauses, max_line=max_line, content_rect=content_rect
    ):
        return None
    lines = [_progressive_clause_display(c.strip()) for c in clauses if c.strip()]
    if len(lines) < 2:
        return None
    if all(
        _core_char_len(ln) <= limit
        and progressive_line_fits_hard_width(ln, content_rect=content_rect)
        for ln in lines
    ):
        return lines
    return None


def _tail_orphan_penalty(left: str, right: str, *, min_part: int = 4, max_line: int = 18) -> int:
    from videoaudiotext.subtitle.display import _core_char_len

    penalty = 0
    right_body = right.strip().rstrip("—")
    left_body = left.strip()
    right_core = _core_char_len(right_body)
    left_core = _core_char_len(left_body)
    if right_core < min_part:
        penalty += 1000
    if left_core < min_part:
        penalty += 500
    from videoaudiotext.config.subtitle import progressive_min_screen_chars

    min_scr = progressive_min_screen_chars()
    if right_core < min_scr:
        penalty += 2500
    if left_core < min_scr:
        penalty += 1800
    from videoaudiotext.subtitle.quotes import QUOTE_CLOSERS

    if right_body.startswith("的"):
        if left_body.rstrip().endswith(tuple(QUOTE_CLOSERS)):
            penalty -= 1500
        elif continues_after_closed_quote(left_body, right_body):
            penalty -= 1200
        else:
            penalty += 1200
    if right_heads_into_de_phrase(right_body):
        penalty += 1150
    if right_heads_into_adv_predicate(right_body):
        penalty += 1150
    if left_body.rstrip("，、；：").endswith("的"):
        penalty += 1100
    if left_body.rstrip("，、；：").endswith("是"):
        penalty += 1100
    if left_body.endswith(("”", '"', "」", "》")) and right_body.startswith("的"):
        penalty -= 800
    if "。" in right_body[:8]:
        period_at = right_body.index("。")
        if period_at <= 4 and not left_body.endswith("。"):
            penalty += 500
    from videoaudiotext.subtitle.breaks import split_crosses_compound

    if split_crosses_compound(left_body, right_body):
        penalty += 1200
    if right_body and right_body[0] in QUOTE_OPENERS:
        penalty += 1200
    if line_has_unbalanced_quotes(left_body) or line_has_unbalanced_quotes(right_body):
        penalty += 5000
    if _break_between_adjacent_quotes(left_body, right_body):
        penalty -= 1800
    from videoaudiotext.subtitle.parentheses import PAREN_OPENERS

    if right_body and right_body[0] in PAREN_OPENERS:
        penalty += 1200
    if continues_after_closed_quote(left_body, right_body):
        penalty -= 1000
    if should_merge_short_tail_lines(left_body, right_body, max_line):
        penalty += 850
    return penalty


def _dunhao_forced_break_positions(rest: str) -> list[int] | None:
    """When a dunhao list is present, only break after full list items."""
    if "、" not in rest:
        return None
    positions = [
        pos
        for pos in preferred_semantic_break_positions(rest)
        if pos > 0 and rest[pos - 1] == "、"
    ]
    return positions or None


def _pick_pack_break(
    rest: str,
    max_line: int,
    *,
    lim_soft: int,
    lim_hard: int,
    min_part: int = 4,
    content_rect=None,
) -> int | None:
    from videoaudiotext.config.subtitle import (
        estimate_progressive_text_width_px,
        progressive_hard_max_width_px,
    )
    from videoaudiotext.subtitle.display import _core_char_len, _is_break_allowed

    forced = _dunhao_forced_break_positions(rest)
    candidates: list[tuple[int, int, int]] = []
    hard_px = progressive_hard_max_width_px(content_rect=content_rect)

    def _maybe_add(pos: int) -> None:
        if forced is not None and pos not in forced:
            return
        if not _valid_pack_break(rest, pos):
            return
        if not _is_break_allowed(rest, pos):
            return
        if _core_char_len(rest[:pos]) > lim_soft:
            return
        left, right = rest[:pos].strip(), rest[pos:].strip()
        if estimate_progressive_text_width_px(left) > hard_px:
            return
        left_core = _core_char_len(left)
        right_core = _core_char_len(right)
        from videoaudiotext.config.subtitle import progressive_min_screen_chars

        min_scr = progressive_min_screen_chars()
        if left_core < min_scr or right_core < min_scr:
            if not (
                _breaks_after_primary_punctuation(left)
                and left_core >= min_scr
                and right_core >= min_scr
            ):
                return
        if left_core < min_part or right_core < min_part:
            if not (
                _breaks_after_primary_punctuation(left)
                and left_core >= 4
                and right_core >= 4
            ):
                return
        balance = abs(left_core - right_core)
        orphan = _tail_orphan_penalty(left, right, min_part=min_part, max_line=max_line)
        if _breaks_after_primary_punctuation(left):
            orphan -= 900
        entry = (orphan, balance, -pos)
        if entry not in candidates:
            candidates.append(entry)

    for pos in preferred_semantic_break_positions(rest):
        _maybe_add(pos)

    for pos in range(len(rest), 0, -1):
        _maybe_add(pos)

    for pos in range(1, len(rest)):
        if _core_char_len(rest[:pos]) > lim_hard:
            continue
        _maybe_add(pos)

    if not candidates:
        return None
    if min(candidates)[0] >= 1000:
        return None
    return -min(candidates)[2]


def _force_strict_max_break(text: str, max_line: int) -> int | None:
    """strict_max 兜底：仅在 jieba/语义边界且引号平衡处切分。"""
    from videoaudiotext.config.subtitle import progressive_min_screen_chars
    from videoaudiotext.subtitle.display import (
        _core_char_len,
        _is_break_allowed,
        _jieba_word_end_positions,
    )

    rest = text.strip()
    if not rest or _core_char_len(rest) <= max_line:
        return None

    min_scr = progressive_min_screen_chars()
    candidates: list[tuple[int, int, int]] = []

    def _maybe_add(pos: int) -> None:
        if not _valid_pack_break(rest, pos):
            return
        if not _is_break_allowed(rest, pos):
            return
        left = rest[:pos].strip()
        right = rest[pos:].strip()
        left_core = _core_char_len(left)
        right_core = _core_char_len(right.rstrip("—"))
        if left_core > max_line or left_core < 1:
            return
        if right_core < min_scr and pos > 1:
            return
        balance = abs(left_core - right_core)
        orphan = _tail_orphan_penalty(left, right, min_part=1, max_line=max_line)
        entry = (orphan, balance, -pos)
        if entry not in candidates:
            candidates.append(entry)

    for pos in preferred_semantic_break_positions(rest):
        _maybe_add(pos)

    for pos in quote_aware_break_positions(rest):
        _maybe_add(pos)

    for pos in sorted(_jieba_word_end_positions(rest), reverse=True):
        _maybe_add(pos)

    if not candidates:
        return None
    return -min(candidates)[2]


def _pack_clause_lines(
    clause: str,
    max_line: int,
    overflow: int,
    *,
    strict_max: bool = False,
    content_rect=None,
) -> list[str]:
    from videoaudiotext.config.subtitle import progressive_line_fits_hard_width
    from videoaudiotext.subtitle.display import _core_char_len
    from videoaudiotext.subtitle.protected_spans import all_protected_spans

    clause = clause.strip()
    if not clause:
        return []

    lim_soft = max_line if strict_max else max_line + 2
    lim_hard = max_line + (0 if strict_max else overflow)
    min_part = 1 if strict_max else 4
    if _core_char_len(clause) <= max_line and progressive_line_fits_hard_width(
        clause, content_rect=content_rect
    ):
        return [clause]

    lines: list[str] = []
    rest = clause
    while rest:
        if _core_char_len(rest) <= max_line and progressive_line_fits_hard_width(
            rest, content_rect=content_rect
        ):
            lines.append(rest)
            break

        best_pos = _pick_pack_break(
            rest,
            max_line,
            lim_soft=lim_soft,
            lim_hard=lim_hard,
            min_part=min_part,
            content_rect=content_rect,
        )
        if best_pos is None:
            from videoaudiotext.config.subtitle import progressive_min_screen_chars
            from videoaudiotext.subtitle.display import _is_break_allowed

            min_scr = progressive_min_screen_chars()
            for pos in sorted(
                set(quote_aware_break_positions(rest))
                | set(preferred_semantic_break_positions(rest)),
                reverse=True,
            ):
                if not _valid_pack_break(rest, pos) or not _is_break_allowed(rest, pos):
                    continue
                left = rest[:pos].strip()
                right = rest[pos:].strip()
                if (
                    _core_char_len(left) < min_scr
                    or _core_char_len(right) < min_scr
                ):
                    continue
                best_pos = pos
                break
            if best_pos is not None:
                lines.append(rest[:best_pos].strip())
                rest = rest[best_pos:].strip()
                continue
            if strict_max and _core_char_len(rest) > max_line:
                forced = _force_strict_max_break(rest, max_line)
                if forced is not None and 0 < forced < len(rest):
                    lines.append(rest[:forced].strip())
                    rest = rest[forced:].strip()
                    continue
            if strict_max:
                lines.extend(
                    _force_progressive_split(rest, max_line, content_rect=content_rect)
                )
            elif (
                not strict_max
                and all_protected_spans(rest)
                and _core_char_len(rest) <= lim_hard
            ):
                lines.append(rest)
            else:
                lines.extend(
                    _force_progressive_split(rest, max_line, content_rect=content_rect)
                )
            break

        lines.append(rest[:best_pos].strip())
        rest = rest[best_pos:].strip()
    return lines


def pack_text_to_screen_lines(
    text: str,
    max_line: int,
    *,
    strict_max: bool = False,
) -> list[str]:
    """
    Pack text into screen lines without breaking inside quoted spans.

    Uses semantic breaks (comma, em-dash, dunhao, quote edges) and allows a
    small overflow budget for indivisible quote blocks unless ``strict_max``.
    """
    from videoaudiotext.subtitle.display import _core_char_len

    plain = text.replace("\\N", "\n").strip()
    if not plain:
        return []

    overflow = 0 if strict_max else quote_line_overflow_allowance(max_line)
    pack_soft = max_line if strict_max else max_line + 2
    pack_keep = max_line if strict_max else max_line + overflow

    if _core_char_len(plain) <= pack_keep:
        if _core_char_len(plain) <= max_line:
            return [plain]
        packed = _pack_clause_lines(plain, max_line, overflow, strict_max=strict_max)
        return packed if packed else [plain]

    clauses = _merge_post_period_opener_clauses(
        _merge_short_opener_clauses(_top_level_clauses(plain), max_line=max_line)
    )
    lines: list[str] = []
    buf = ""
    for clause in clauses:
        candidate = f"{buf}{clause}" if buf else clause
        if _core_char_len(candidate) <= pack_soft:
            buf = candidate
            continue
        if buf:
            lines.extend(
                _pack_clause_lines(buf, max_line, overflow, strict_max=strict_max)
            )
            buf = ""
        if _core_char_len(clause) <= pack_keep and _core_char_len(clause) <= pack_soft:
            buf = clause
        elif _core_char_len(clause) <= pack_keep:
            lines.append(clause)
        else:
            lines.extend(
                _pack_clause_lines(clause, max_line, overflow, strict_max=strict_max)
            )
    if buf:
        if _core_char_len(buf) <= max_line:
            lines.append(buf)
        else:
            lines.extend(
                _pack_clause_lines(buf, max_line, overflow, strict_max=strict_max)
            )
    if not strict_max:
        from videoaudiotext.subtitle.protected_spans import merge_protected_orphan_screen_lines

        lines = merge_protected_orphan_screen_lines(lines, max_line)
    return [ln for ln in lines if ln.strip()]


def _merge_single_char_orphan_lines(
    lines: list[str],
    max_line: int,
    *,
    content_rect=None,
) -> list[str]:
    """合并尾行单字孤儿（如「市」「是」）到上一行，若合并后仍不超像素上限。"""
    from videoaudiotext.subtitle.display import _core_char_len

    work = [ln.strip() for ln in lines if ln.strip()]
    if len(work) <= 1:
        return work
    merged: list[str] = []
    for ln in work:
        if merged and _core_char_len(ln) <= 2:
            if should_merge_single_char_orphan_lines(
                merged[-1],
                ln,
                max_line,
                content_rect=content_rect,
            ) or (
                not is_single_char_orphan(ln)
                and progressive_lines_combine_fits(
                    merged[-1], ln, content_rect=content_rect
                )
            ):
                merged[-1] = merged[-1] + ln
                continue
        merged.append(ln)
    return merged


def pack_progressive_screen_lines(
    text: str,
    max_line: int,
    *,
    content_rect=None,
) -> list[str]:
    """
    渐进屏显装箱：先按分辨率×字号×font_scale 计算 90% 画布硬宽与 pack_chars，
    再在该硬约束下做标点/语义/jieba 切分（不再先按软上限拆完再补像素 pass）。
    """
    from videoaudiotext.config.subtitle import (
        SUBTITLE_PROGRESSIVE_MIN_PART_CHARS,
        progressive_layout_limits,
        progressive_line_fits_hard_width,
        progressive_text_needs_split,
    )
    from videoaudiotext.subtitle.display import _core_char_len, _normalize_subtitle_text

    plain = _normalize_subtitle_text(text.replace("\\N", "\n"))
    if not plain:
        return []

    limits = progressive_layout_limits(content_rect=content_rect)
    pack_limit = max(1, min(int(max_line), int(limits["pack_chars"])))
    pixel_limit = int(limits["pixel_chars"])

    if not progressive_text_needs_split(plain, max_line=pack_limit, content_rect=content_rect):
        return [plain]

    punct_lines = try_progressive_punctuation_lines(
        plain, pack_limit, pack_limit=pack_limit, content_rect=content_rect
    )
    if punct_lines is not None:
        return _finalize_progressive_screen_lines(
            punct_lines, pack_limit, pixel_limit, content_rect=content_rect
        )

    min_part = max(
        3,
        min(
            SUBTITLE_PROGRESSIVE_MIN_PART_CHARS,
            pack_limit - 1,
            pack_limit * 2 // 3 + 1,
        ),
    )
    _ = min_part  # 装箱 strict 模式由 _pick_pack_break min_part 控制

    clauses = _merge_post_period_opener_clauses(
        _merge_short_opener_clauses(_top_level_clauses(plain), max_line=pack_limit)
    )
    lines: list[str] = []
    buf = ""
    for clause in clauses:
        candidate = f"{buf}{clause}" if buf else clause
        if (
            _core_char_len(candidate) <= pack_limit
            and progressive_line_fits_hard_width(candidate, content_rect=content_rect)
        ):
            buf = candidate
            continue
        if buf:
            lines.extend(
                _pack_clause_lines(
                    buf,
                    pack_limit,
                    0,
                    strict_max=True,
                    content_rect=content_rect,
                )
            )
            buf = ""
        if (
            _core_char_len(clause) <= pack_limit
            and progressive_line_fits_hard_width(clause, content_rect=content_rect)
        ):
            buf = clause
        else:
            lines.extend(
                _pack_clause_lines(
                    clause,
                    pack_limit,
                    0,
                    strict_max=True,
                    content_rect=content_rect,
                )
            )
    if buf:
        if (
            _core_char_len(buf) <= pack_limit
            and progressive_line_fits_hard_width(buf, content_rect=content_rect)
        ):
            lines.append(buf)
        else:
            lines.extend(
                _pack_clause_lines(
                    buf,
                    pack_limit,
                    0,
                    strict_max=True,
                    content_rect=content_rect,
                )
            )

    return _finalize_progressive_screen_lines(
        lines, pack_limit, pixel_limit, content_rect=content_rect
    )


def _finalize_progressive_screen_lines(
    lines: list[str],
    pack_limit: int,
    pixel_limit: int,
    *,
    content_rect=None,
) -> list[str]:
    """合并 orphan 后再次校验 90% 硬宽（兜底）。"""
    from videoaudiotext.config.subtitle import progressive_line_fits_hard_width
    from videoaudiotext.subtitle.display import _core_char_len
    from videoaudiotext.subtitle.protected_spans import merge_protected_orphan_screen_lines

    changed = True
    for _ in range(len(lines)):
        if not changed or len(lines) <= 1:
            break
        changed = False
        for i in range(len(lines) - 1):
            if progressive_screen_lines_should_stay_split(
                lines[i], lines[i + 1], pack_limit
            ):
                continue
            combined = lines[i] + lines[i + 1]
            if _core_char_len(combined) <= pack_limit and progressive_lines_combine_fits(
                lines[i], lines[i + 1], content_rect=content_rect, max_line=pack_limit
            ):
                lines[i : i + 2] = [combined]
                changed = True
                break

    lines = merge_protected_orphan_screen_lines(lines, pack_limit)

    merged: list[str] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if merged and _core_char_len(ln) <= 3:
            if should_merge_single_char_orphan_lines(
                merged[-1], ln, pack_limit
            ) or progressive_lines_combine_fits(
                merged[-1], ln, content_rect=content_rect, max_line=pack_limit
            ):
                merged[-1] = merged[-1] + ln
                continue
        if merged and _core_char_len(merged[-1]) <= 3:
            if should_merge_single_char_orphan_lines(
                merged[-1], ln, pack_limit
            ) or progressive_lines_combine_fits(
                merged[-1], ln, content_rect=content_rect, max_line=pack_limit
            ):
                merged[-1] = merged[-1] + ln
                continue
        merged.append(ln)
    lines = merged

    pixel_lines = _apply_pixel_limit_to_screen_lines(
        lines, pixel_limit, content_rect=content_rect
    )
    dash_lines = attach_em_dash_orphan_lines(
        pixel_lines, pack_limit, content_rect=content_rect
    )
    enforced: list[str] = []
    for ln in dash_lines:
        ln = ln.strip()
        if not ln:
            continue
        if _core_char_len(ln) <= pack_limit and progressive_line_fits_hard_width(
            ln, content_rect=content_rect
        ):
            enforced.append(ln)
        else:
            enforced.extend(
                _force_progressive_split(ln, pack_limit, content_rect=content_rect)
            )
    return eliminate_single_char_screen_lines(
        _merge_single_char_orphan_lines(enforced, pixel_limit),
        pack_limit,
    )
