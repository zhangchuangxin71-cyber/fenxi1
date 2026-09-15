from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any


SUPPORTED_DOC_TYPES = {"md", "markdown", "txt", "html", "htm"}
DEFAULT_HEADING_TITLE_MAX_CHARS = 60
DEFAULT_MAX_NEW_NODES_PER_LEAF = 100
CHINESE_NUMERAL = "一二三四五六七八九十百千万两"


@dataclass(frozen=True)
class HeadingCandidate:
    line_index: int
    title: str
    level: int


@dataclass(frozen=True)
class TextChunk:
    title: str
    text: str
    level: int


def split_long_leaf_nodes(
    nodes: list[dict[str, Any]],
    *,
    doc_type: str,
    enabled: bool,
    min_chars: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    """Split only overlong text-like leaf nodes by conservative weak headings."""
    normalized_type = str(doc_type or "").strip().lower()
    if not enabled or normalized_type not in SUPPORTED_DOC_TYPES:
        return nodes

    min_chars = max(1, int(min_chars))
    max_chars = max(min_chars + 1, int(max_chars))
    return _split_node_list(nodes, min_chars=min_chars, max_chars=max_chars)


def _split_node_list(nodes: list[dict[str, Any]], *, min_chars: int, max_chars: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        children = node.get("nodes")
        if isinstance(children, list) and children:
            copied = copy.deepcopy(node)
            copied["nodes"] = _split_node_list(children, min_chars=min_chars, max_chars=max_chars)
            output.append(copied)
            continue

        text = _clean_text(node.get("text"))
        if len(text) <= max_chars:
            output.append(node)
            continue

        chunks = split_overlong_text(
            text,
            fallback_title=_clean_text(node.get("title")) or "文本片段",
            base_level=int(node.get("level") or 1),
            min_chars=min_chars,
            max_chars=max_chars,
        )
        if len(chunks) <= 1:
            output.append(node)
            continue

        for chunk in chunks:
            new_node = copy.deepcopy(node)
            new_node.pop("nodes", None)
            new_node.pop("node_id", None)
            new_node["title"] = chunk.title[:200]
            new_node["level"] = chunk.level
            new_node["text"] = chunk.text
            new_node["summary"] = ""
            new_node["_summary_source"] = chunk.text
            output.append(new_node)
    return output


def split_overlong_text(
    text: str,
    *,
    fallback_title: str,
    base_level: int,
    min_chars: int,
    max_chars: int,
) -> list[TextChunk]:
    lines = text.splitlines()
    candidates = _detect_heading_candidates(lines)
    if len(candidates) >= 2:
        chunks = _split_by_candidates(lines, candidates, base_level=base_level)
        chunks = _merge_short_chunks(chunks, min_chars=min_chars)
        chunks = _split_overlong_chunks(chunks, min_chars=min_chars, max_chars=max_chars)
        if len(chunks) > 1 and len(chunks) <= DEFAULT_MAX_NEW_NODES_PER_LEAF:
            return chunks

    chunks = _split_by_paragraphs(text, fallback_title=fallback_title, base_level=base_level, min_chars=min_chars, max_chars=max_chars)
    if _chunks_are_usable(chunks, max_chars=max_chars):
        return chunks
    return _split_by_fixed_chars(text, fallback_title=fallback_title, base_level=base_level, max_chars=max_chars)


def _detect_heading_candidates(lines: list[str]) -> list[HeadingCandidate]:
    candidates: list[HeadingCandidate] = []
    in_code_block = False
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if re.match(r"^(```|~~~)", stripped):
            in_code_block = not in_code_block
            continue
        if in_code_block or not stripped:
            continue
        candidate = _match_heading_line(stripped, index)
        if candidate:
            candidates.append(candidate)
    return candidates


def _match_heading_line(line: str, line_index: int) -> HeadingCandidate | None:
    if not _passes_common_heading_filters(line):
        return None

    md = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
    if md:
        title = re.sub(r"\s+#+$", "", md.group(2)).strip()
        if _valid_title_text(title, allow_sentence_punctuation=False, max_chars=80):
            return HeadingCandidate(line_index=line_index, title=title, level=len(md.group(1)))
        return None

    patterns: list[tuple[str, int, bool]] = [
        (rf"^第[{CHINESE_NUMERAL}0-9]+[章节篇部]\s*[:：、.．-]?\s*(\S.{{0,{DEFAULT_HEADING_TITLE_MAX_CHARS - 1}}})?$", 1, True),
        (rf"^[{CHINESE_NUMERAL}]+[、.．]\s*(\S.{{0,{DEFAULT_HEADING_TITLE_MAX_CHARS - 1}}})$", 1, False),
        (rf"^[（(]\s*[{CHINESE_NUMERAL}]+\s*[）)]\s*(\S.{{0,{DEFAULT_HEADING_TITLE_MAX_CHARS - 1}}})$", 2, False),
        (r"^\d{1,2}\.\d{1,2}\.\d{1,2}(?![%\d])\s+(\S.{0,59})$", 4, False),
        (r"^\d{1,2}\.\d{1,2}(?![%\d])\s+(\S.{0,59})$", 3, False),
        (r"^\d{1,2}[、．]\s*(\S.{0,59})$", 2, False),
        (r"^\d{1,2}\.(?![%\d])\s*(\S.{0,59})$", 2, False),
    ]
    for pattern, level, allow_empty_suffix in patterns:
        match = re.match(pattern, line, flags=re.IGNORECASE)
        if not match:
            continue
        suffix = (match.group(1) or "").strip() if match.lastindex else ""
        if suffix or allow_empty_suffix:
            return HeadingCandidate(line_index=line_index, title=line[:200], level=level)
    return None


def _passes_common_heading_filters(line: str) -> bool:
    if not line or len(line) > 100:
        return False
    if "|" in line:
        return False
    if "://" in line or line.startswith(("http:", "https:", "www.")):
        return False
    if re.search(r"[。！？；，,;]$", line):
        return False
    if re.fullmatch(r"[\d\s./年月日:-]+", line):
        return False
    return True


def _valid_title_text(text: str, *, allow_sentence_punctuation: bool, max_chars: int) -> bool:
    text = text.strip()
    if not text or len(text) > max_chars:
        return False
    if not allow_sentence_punctuation and re.search(r"[。！？；，,;]$", text):
        return False
    if re.fullmatch(r"[\d\s./年月日:-]+", text):
        return False
    return True


def _split_by_candidates(lines: list[str], candidates: list[HeadingCandidate], *, base_level: int) -> list[TextChunk]:
    min_candidate_level = min(candidate.level for candidate in candidates)
    chunks: list[TextChunk] = []
    first = candidates[0]
    preamble = _join_lines(lines[: first.line_index])
    for index, candidate in enumerate(candidates):
        next_line = candidates[index + 1].line_index if index + 1 < len(candidates) else len(lines)
        chunk_lines = lines[candidate.line_index:next_line]
        chunk_text = _join_lines(chunk_lines)
        if index == 0 and preamble:
            chunk_text = f"{preamble}\n\n{chunk_text}".strip()
        if not chunk_text:
            continue
        level = max(1, base_level + candidate.level - min_candidate_level)
        chunks.append(TextChunk(title=candidate.title, text=chunk_text, level=level))
    return chunks[:DEFAULT_MAX_NEW_NODES_PER_LEAF]


def _merge_short_chunks(chunks: list[TextChunk], *, min_chars: int) -> list[TextChunk]:
    if len(chunks) <= 1:
        return chunks
    merged: list[TextChunk] = []
    for chunk in chunks:
        if len(chunk.text) >= min_chars or not merged:
            merged.append(chunk)
            continue
        previous = merged[-1]
        merged[-1] = TextChunk(
            title=previous.title,
            text=f"{previous.text}\n\n{chunk.text}".strip(),
            level=previous.level,
        )
    if len(merged) > 1 and len(merged[0].text) < min_chars:
        first, second = merged[0], merged[1]
        merged[1] = TextChunk(title=second.title, text=f"{first.text}\n\n{second.text}".strip(), level=second.level)
        merged = merged[1:]
    return merged


def _chunks_are_usable(chunks: list[TextChunk], *, max_chars: int) -> bool:
    if len(chunks) <= 1 or len(chunks) > DEFAULT_MAX_NEW_NODES_PER_LEAF:
        return False
    return all(len(chunk.text) <= max_chars for chunk in chunks)


def _split_overlong_chunks(chunks: list[TextChunk], *, min_chars: int, max_chars: int) -> list[TextChunk]:
    output: list[TextChunk] = []
    for chunk in chunks:
        if len(chunk.text) <= max_chars:
            output.append(chunk)
            continue
        output.extend(
            _split_by_paragraphs(
                chunk.text,
                fallback_title=chunk.title,
                base_level=chunk.level,
                min_chars=min_chars,
                max_chars=max_chars,
            )
        )
    return output[:DEFAULT_MAX_NEW_NODES_PER_LEAF]


def _split_by_paragraphs(
    text: str,
    *,
    fallback_title: str,
    base_level: int,
    min_chars: int,
    max_chars: int,
) -> list[TextChunk]:
    parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(parts) <= 1:
        parts = [part.strip() for part in text.splitlines() if part.strip()]
    chunks: list[TextChunk] = []
    current: list[str] = []
    for part in parts:
        candidate = "\n\n".join([*current, part]).strip()
        if current and len(candidate) > max_chars and len("\n\n".join(current)) >= min_chars:
            chunks.append(_make_numbered_chunk(fallback_title, len(chunks) + 1, "\n\n".join(current), base_level))
            current = [part]
        else:
            current.append(part)
    if current:
        chunks.append(_make_numbered_chunk(fallback_title, len(chunks) + 1, "\n\n".join(current), base_level))
    chunks = _merge_short_chunks(chunks, min_chars=min_chars)
    overflow: list[TextChunk] = []
    for chunk in chunks:
        if len(chunk.text) <= max_chars:
            overflow.append(chunk)
        else:
            overflow.extend(_split_by_fixed_chars(chunk.text, fallback_title=chunk.title, base_level=chunk.level, max_chars=max_chars))
    return overflow


def _split_by_fixed_chars(text: str, *, fallback_title: str, base_level: int, max_chars: int) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    start = 0
    while start < len(text):
        part = text[start : start + max_chars].strip()
        if part:
            chunks.append(_make_numbered_chunk(fallback_title, len(chunks) + 1, part, base_level))
        start += max_chars
    return chunks


def _make_numbered_chunk(title: str, index: int, text: str, level: int) -> TextChunk:
    suffix = f" {index}" if index > 1 else ""
    return TextChunk(title=f"{title}{suffix}"[:200], text=text.strip(), level=level)


def _join_lines(lines: list[str]) -> str:
    return "\n".join(line.rstrip() for line in lines).strip()


def _clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()
