from __future__ import annotations

from collections.abc import Callable

import tiktoken


class QueryBudgetError(ValueError):
    def __init__(self, message: str, *, code: str = "QUERY_TOO_LONG_OR_INVALID") -> None:
        super().__init__(message)
        self.code = code


class TiktokenCounter:
    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self.encoding_name = encoding_name
        self._encoding = tiktoken.get_encoding(encoding_name)

    def __call__(self, text: str) -> int:
        return len(self._encoding.encode(text or ""))


def ensure_fixed_prompt_fits(
    *,
    fixed_text: str,
    context_window: int,
    safety_margin: int,
    count_tokens: Callable[[str], int],
) -> int:
    used = max(0, int(count_tokens(fixed_text))) + max(0, int(safety_margin))
    if used > max(1, int(context_window)):
        raise QueryBudgetError("query groups and fixed prompt exceed the model context window")
    return used


def split_text_by_token(
    text: str,
    *,
    max_tokens: int,
    count_tokens: Callable[[str], int],
    overlap_ratio: float = 0.1,
) -> list[str]:
    """Split text into complete ordered windows without needing tokenizer decode support."""
    if not text:
        return [""]
    budget = max(1, int(max_tokens))
    if count_tokens(text) <= budget:
        return [text]
    overlap = min(0.5, max(0.0, float(overlap_ratio)))
    windows: list[str] = []
    start = 0
    while start < len(text):
        low, high = start + 1, len(text)
        best_end = start
        while low <= high:
            middle = (low + high) // 2
            if count_tokens(text[start:middle]) <= budget:
                best_end = middle
                low = middle + 1
            else:
                high = middle - 1
        if best_end == start:
            raise QueryBudgetError("a single character exceeds the model input budget")
        windows.append(text[start:best_end])
        if best_end == len(text):
            break
        overlap_chars = max(1, int((best_end - start) * overlap)) if overlap else 0
        start = max(start + 1, best_end - overlap_chars)
    return windows
