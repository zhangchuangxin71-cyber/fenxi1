from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass


class TokenBudgetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BudgetItem:
    item_id: str
    text: str
    estimated_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TokenGroup:
    items: tuple[BudgetItem, ...]
    token_count: int


def group_by_token(
    items: Sequence[BudgetItem],
    *,
    context_window: int,
    reserved_tokens: int,
    count_tokens: Callable[[str], int],
    target_load_ratio: float = 0.65,
) -> list[TokenGroup]:
    available = int(context_window) - int(reserved_tokens)
    if available <= 0:
        raise TokenBudgetError("reserved tokens leave no available context")

    measured = [
        (
            index,
            item,
            max(
                0,
                int(item.estimated_tokens)
                if item.estimated_tokens is not None
                else int(count_tokens(item.text)),
            ),
        )
        for index, item in enumerate(items)
    ]
    oversized = [item.item_id for _, item, cost in measured if cost > available]
    if oversized:
        raise TokenBudgetError(f"items exceed context budget: {oversized}")
    if not measured:
        return []

    ratio = min(1.0, max(0.1, float(target_load_ratio)))
    target_load = max(1, int(available * ratio))
    desired_groups = min(
        len(measured),
        max(1, math.ceil(sum(cost for _, _, cost in measured) / target_load)),
    )
    bins: list[tuple[list[BudgetItem], int]] = [([], 0) for _ in range(desired_groups)]

    for _, item, cost in sorted(measured, key=lambda value: (-value[2], value[0])):
        fitting = [
            (used, index) for index, (_, used) in enumerate(bins) if used + cost <= available
        ]
        if not fitting:
            bins.append(([item], cost))
            continue
        _, target_index = min(fitting)
        group_items, used = bins[target_index]
        group_items.append(item)
        bins[target_index] = (group_items, used + cost)

    return [
        TokenGroup(items=tuple(group_items), token_count=used)
        for group_items, used in bins
        if group_items
    ]
