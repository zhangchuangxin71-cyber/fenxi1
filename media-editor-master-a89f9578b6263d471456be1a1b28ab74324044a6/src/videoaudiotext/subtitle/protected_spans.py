"""Unified quote + parenthesis atomic spans for subtitle break decisions."""

from __future__ import annotations

from videoaudiotext.subtitle.parentheses import (
    PAREN_CLOSERS,
    PAREN_OPENERS,
    parenthesized_char_spans,
    position_inside_parentheses,
    paren_aware_break_positions,
    should_merge_paren_orphan_lines,
    split_orphan_paren_boundary,
)
from videoaudiotext.subtitle.quotes import (
    QUOTE_CLOSERS,
    QUOTE_OPENERS,
    position_inside_quotes,
    quoted_char_spans,
    quote_aware_break_positions,
    should_merge_quote_orphan_lines,
    split_orphan_quote_boundary,
)


def position_inside_protected_span(text: str, pos: int) -> bool:
    return position_inside_quotes(text, pos) or position_inside_parentheses(text, pos)


def split_orphan_protected_boundary(line1: str, line2: str) -> bool:
    return split_orphan_quote_boundary(line1, line2) or split_orphan_paren_boundary(
        line1, line2
    )


def all_protected_spans(text: str) -> list[tuple[int, int]]:
    return sorted(quoted_char_spans(text) + parenthesized_char_spans(text))


def protected_aware_break_positions(text: str) -> list[int]:
    positions = list(quote_aware_break_positions(text))
    positions.extend(paren_aware_break_positions(text))
    seen: set[int] = set()
    out: list[int] = []
    n = len(text)
    for pos in positions:
        if 0 < pos < n and pos not in seen:
            seen.add(pos)
            out.append(pos)
    return sorted(out)


def should_merge_protected_orphan_lines(left: str, right: str, max_line: int) -> bool:
    return should_merge_quote_orphan_lines(left, right, max_line) or (
        should_merge_paren_orphan_lines(left, right, max_line)
    )


def merge_protected_orphan_screen_lines(lines: list[str], max_line: int) -> list[str]:
    from videoaudiotext.subtitle.quotes import merge_quote_orphan_screen_lines

    work = merge_quote_orphan_screen_lines(lines, max_line)
    from videoaudiotext.subtitle.parentheses import merge_paren_orphan_screen_lines

    return merge_paren_orphan_screen_lines(work, max_line)
