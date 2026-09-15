from __future__ import annotations

import re
from dataclasses import dataclass

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_STOPWORDS = {"是谁", "什么", "怎么", "如何", "以及", "一个", "这个", "那个", "请问"}


@dataclass(frozen=True, slots=True)
class RankedText:
    item_id: str
    score: float
    original_index: int


def tokenize_query(query: str) -> list[str]:
    text = (query or "").strip().lower()
    tokens: set[str] = set(_WORD_RE.findall(text))
    for sequence in _CJK_RE.findall(text):
        if len(sequence) <= 8:
            tokens.add(sequence)
        for size in (2, 3, 4):
            for index in range(max(0, len(sequence) - size + 1)):
                tokens.add(sequence[index : index + size])
    return sorted(token for token in tokens if token and token not in _STOPWORDS)


def score_text(query: str, terms: list[str], *, content: str, metadata: str = "") -> float:
    normalized_content = (content or "").lower()
    normalized_metadata = (metadata or "").lower()
    query_text = (query or "").strip().lower()
    score = 0.0
    if query_text and query_text in normalized_content:
        score += 40.0
    if query_text and query_text in normalized_metadata:
        score += 20.0
    for term in terms:
        if term in normalized_content:
            score += 10.0 + min(normalized_content.count(term), 5)
        if term in normalized_metadata:
            score += 5.0 + min(normalized_metadata.count(term), 3)
    return score


def rank_texts(query: str, items: list[tuple[str, str, str]]) -> list[RankedText]:
    terms = tokenize_query(query)
    ranked = [
        RankedText(
            item_id=item_id,
            score=score_text(query, terms, content=content, metadata=metadata),
            original_index=index,
        )
        for index, (item_id, content, metadata) in enumerate(items)
    ]
    return sorted(ranked, key=lambda item: (-item.score, item.original_index))
