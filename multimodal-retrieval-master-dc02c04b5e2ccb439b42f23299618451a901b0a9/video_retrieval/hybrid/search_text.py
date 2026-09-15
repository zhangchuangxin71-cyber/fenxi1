from __future__ import annotations

import unicodedata


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return " ".join(normalized.split())


def _is_ascii_word_char(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _is_cjk_char(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _append_token(tokens: list[str], seen: set[str], token: str) -> None:
    item = token.strip().lower()
    if not item or item in seen:
        return
    tokens.append(item)
    seen.add(item)


def _iter_term_chunks(text: str):
    chunk: list[str] = []
    chunk_kind: str | None = None

    def flush():
        nonlocal chunk, chunk_kind
        if chunk:
            yield "".join(chunk)
        chunk = []
        chunk_kind = None

    for char in text:
        if _is_ascii_word_char(char):
            kind = "ascii"
        elif _is_cjk_char(char):
            kind = "cjk"
        else:
            kind = None

        if kind is None:
            yield from flush()
            continue

        if chunk_kind is not None and kind != chunk_kind:
            yield from flush()

        chunk.append(char)
        chunk_kind = kind

    yield from flush()


def tokenize_search_terms(text: str) -> list[str]:
    normalized = normalize_text(text)
    tokens: list[str] = []
    seen: set[str] = set()

    for chunk in _iter_term_chunks(normalized):
        if all(_is_ascii_word_char(char) for char in chunk):
            _append_token(tokens, seen, chunk)
            continue

        if len(chunk) == 1:
            _append_token(tokens, seen, chunk)
            continue

        if len(chunk) <= 4:
            _append_token(tokens, seen, chunk)

        for width in (2, 3):
            if len(chunk) < width:
                continue
            for start in range(0, len(chunk) - width + 1):
                _append_token(tokens, seen, chunk[start : start + width])

    return tokens


def build_tag_term_text(tags: list[str]) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        tag = normalize_text(raw_tag)
        if not tag:
            continue
        _append_token(tokens, seen, tag)
        for token in tokenize_search_terms(tag):
            _append_token(tokens, seen, token)
    return " ".join(tokens)


def build_description_term_text(description: str) -> str:
    return " ".join(tokenize_search_terms(description))


def build_caption_term_text(tags: list[str], description: str, caption: str) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        tag = normalize_text(raw_tag)
        if tag:
            _append_token(tokens, seen, tag)
    for text in (description, caption):
        for token in tokenize_search_terms(text):
            _append_token(tokens, seen, token)
    return " ".join(tokens)


def build_sparse_query(query_text: str, *, limit: int = 24) -> str:
    tokens = tokenize_search_terms(query_text)
    if not tokens:
        fallback = normalize_text(query_text)
        return f'"{fallback.replace(chr(34), chr(34) * 2)}"' if fallback else ""
    selected = tokens[:limit]
    quoted = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in selected]
    return " OR ".join(quoted)
