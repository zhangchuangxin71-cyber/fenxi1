"""Generic break-position guards (no domain word lists)."""

from __future__ import annotations

import re

# 时间/数量单位后不接方位/时间后缀
_TIME_UNIT_TAIL = frozenset("年月日岁周")
_TIME_SUFFIX_HEAD = frozenset("前后初末内外来去往")
# 副词 + 介词/助词/动词（仍在、还是、正在）
_ADV_TAIL = frozenset("仍还正刚")
_GLUE_HEAD = frozenset("在是会能")
# 数词量词块后不接单字方位（百年|前）
_DIGIT_RUN_RE = re.compile(r"\d+$")


def position_breaks_compound(text: str, pos: int) -> bool:
    """
    True when ``pos`` splits a glued compound and must not be used as break.

    Examples: 百年|前, 仍在|烧制, 500年|不灭 (when 年 is unit), 仍|在
    """
    if pos <= 0 or pos >= len(text):
        return False

    left_ch = text[pos - 1]
    right_ch = text[pos]

    if left_ch in _TIME_UNIT_TAIL and right_ch in _TIME_SUFFIX_HEAD:
        return True

    if left_ch in _ADV_TAIL and right_ch in _GLUE_HEAD:
        return True

    # jieba may tag 百年 as one token but char break can still land on 年|前
    if left_ch == "年" and right_ch in _TIME_SUFFIX_HEAD:
        return True

    if left_ch in "岁周" and right_ch in _TIME_SUFFIX_HEAD:
        return True

    # digits + 年/月: don't break inside «500|年»
    if right_ch in _TIME_UNIT_TAIL and pos >= 2:
        prefix = text[:pos]
        if _DIGIT_RUN_RE.search(prefix) or (prefix and prefix[-1].isdigit()):
            return True

    if left_ch.isdigit() and right_ch in _TIME_UNIT_TAIL:
        return True

    return False


def split_crosses_compound(left: str, right: str) -> bool:
    """True when the line boundary between left|right breaks a glued compound."""
    if not left or not right:
        return False
    return position_breaks_compound(left + right, len(left.rstrip()))
