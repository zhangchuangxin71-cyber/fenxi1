from __future__ import annotations

import pytest

from app.budgeting.lpt import BudgetItem, group_by_token
from app.budgeting.tokens import QueryBudgetError, ensure_fixed_prompt_fits, split_text_by_token


def _count(text: str) -> int:
    return len(text)


def test_lpt_keeps_every_item_even_when_batches_exceed_concurrency() -> None:
    items = [BudgetItem(item_id=f"i{index}", text="x" * 6) for index in range(5)]

    groups = group_by_token(
        items,
        context_window=10,
        reserved_tokens=0,
        count_tokens=_count,
        target_load_ratio=1.0,
    )

    assert len(groups) == 5
    assert sorted(item.item_id for group in groups for item in group.items) == [
        "i0",
        "i1",
        "i2",
        "i3",
        "i4",
    ]


def test_lpt_balances_items_with_stable_order() -> None:
    items = [
        BudgetItem(item_id="a", text="x" * 6),
        BudgetItem(item_id="b", text="x" * 4),
        BudgetItem(item_id="c", text="x" * 4),
        BudgetItem(item_id="d", text="x" * 2),
    ]

    groups = group_by_token(
        items,
        context_window=10,
        reserved_tokens=0,
        count_tokens=_count,
        target_load_ratio=1.0,
    )

    assert [group.token_count for group in groups] == [8, 8]
    assert [[item.item_id for item in group.items] for group in groups] == [
        ["a", "d"],
        ["b", "c"],
    ]


def test_fixed_group_prompt_that_exceeds_context_is_rejected() -> None:
    with pytest.raises(QueryBudgetError) as exc_info:
        ensure_fixed_prompt_fits(
            fixed_text="x" * 101,
            context_window=100,
            safety_margin=0,
            count_tokens=_count,
        )

    assert exc_info.value.code == "QUERY_TOO_LONG_OR_INVALID"


def test_split_text_by_token_is_complete_ordered_and_overlapping() -> None:
    text = "0123456789" * 8
    windows = split_text_by_token(text, max_tokens=20, count_tokens=len, overlap_ratio=0.2)

    assert len(windows) > 1
    assert all(len(window) <= 20 for window in windows)
    assert windows[0][-4:] == windows[1][:4]
    assert text.startswith(windows[0])
    assert text.endswith(windows[-1])
