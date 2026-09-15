"""Parenthesis-span helpers and paren-safe screen-line packing."""

from __future__ import annotations

PAREN_OPENERS = frozenset("（(")
PAREN_CLOSERS = frozenset("）)")
_PAREN_PAIRS = {
    "（": "）",
    "(": ")",
}


def parenthesized_char_spans(text: str) -> list[tuple[int, int]]:
    """Return [start, end) spans that must not be broken by line/phrase splits."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch not in PAREN_OPENERS:
            i += 1
            continue
        close = _PAREN_PAIRS.get(ch, ch)
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


def position_inside_parentheses(text: str, pos: int) -> bool:
    """True if ``pos`` falls strictly inside a parenthesized span (not on boundaries)."""
    if pos <= 0 or pos >= len(text):
        return False
    for start, end in parenthesized_char_spans(text):
        if start < pos < end:
            return True
    return False


def line_has_unbalanced_parentheses(text: str) -> bool:
    """True when a screen line leaves an unmatched opening/closing parenthesis."""
    depth = 0
    for ch in text:
        if ch in PAREN_OPENERS:
            depth += 1
        elif ch in PAREN_CLOSERS:
            depth -= 1
        if depth < 0:
            return True
    return depth != 0


def split_orphan_paren_boundary(line1: str, line2: str) -> bool:
    """True if the line break leaves dangling parentheses on either side."""
    a = line1.strip()
    b = line2.strip()
    if not a or not b:
        return False
    return line_has_unbalanced_parentheses(a) or line_has_unbalanced_parentheses(b)


def paren_aware_break_positions(text: str) -> list[int]:
    """Preferred break indices: before open-paren and after close-paren."""
    positions: list[int] = []
    for i, ch in enumerate(text):
        if ch in PAREN_OPENERS and i > 0:
            positions.append(i)
        elif ch in PAREN_CLOSERS and i + 1 < len(text):
            positions.append(i + 1)
    return positions


def cue_starts_with_open_paren(text: str) -> bool:
    t = text.strip()
    return bool(t) and t[0] in PAREN_OPENERS


def continues_after_closed_paren(left: str, right: str) -> bool:
    """Left ends with a complete paren group; right continues the phrase."""
    left = left.strip()
    right = right.strip()
    if not left or not right or cue_starts_with_open_paren(right):
        return False
    spans = parenthesized_char_spans(left)
    if not spans:
        return False
    tail = left[spans[-1][1] :].strip()
    if tail and not all(ch in "的之等" for ch in tail):
        return False
    return right[0] not in PAREN_OPENERS and right[0] not in PAREN_CLOSERS


def should_merge_paren_orphan_lines(left: str, right: str, max_line: int) -> bool:
    """Merge when a paren would dangle at a line boundary."""
    from videoaudiotext.subtitle.display import _core_char_len
    from videoaudiotext.subtitle.quotes import progressive_lines_combine_fits

    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    if not (
        cue_starts_with_open_paren(right)
        or continues_after_closed_paren(left, right)
        or split_orphan_paren_boundary(left, right)
    ):
        return False
    combined = left + right
    if not progressive_lines_combine_fits(left, right, max_line=max_line):
        return False
    return _core_char_len(combined) <= max_line and len(combined) <= max_line + 6


def merge_paren_orphan_screen_lines(lines: list[str], max_line: int) -> list[str]:
    """Pull paren orphan lines onto neighbors when width allows."""
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
            if should_merge_paren_orphan_lines(left, right, max_line):
                work[i - 1 : i + 1] = [left + right]
                changed = True
                break
    return work
