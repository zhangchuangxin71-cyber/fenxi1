from __future__ import annotations

"""Minimal Markdown-to-tree parser compatible with PageIndex output.

Kept features:
- Markdown heading detection outside fenced code blocks
- explicit ``[[PAGE N]]`` markers
- synthetic line-based pages when no page markers exist
- optional text thinning and node summaries
- PageIndex-style fields: title/node_id/start_index/end_index/text/nodes
"""

import os
import re
from typing import Any

from .utils import (
    count_tokens,
    create_clean_structure_for_description,
    format_structure,
    gather_with_concurrency,
    generate_doc_description,
    generate_node_summary,
    get_summary_concurrency,
    structure_to_list,
    write_node_id,
)


PAGE_MARKER_RE = re.compile(r"^\s*\[\[PAGE\s+(\d+)\]\]\s*$", flags=re.IGNORECASE)
HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$")
CODE_FENCE_RE = re.compile(r"^\s*```")


def _parse_page_markers(markdown_lines: list[str]) -> list[tuple[int, int]]:
    markers: list[tuple[int, int]] = []
    for line_no, line in enumerate(markdown_lines, 1):
        match = PAGE_MARKER_RE.match(line or "")
        if match:
            markers.append((int(match.group(1)), line_no))
    return markers


def _strip_page_markers_from_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"(?im)^\s*\[\[PAGE\s+\d+\]\]\s*\n?", "", text).strip()


def _build_line_to_page_mapper(markdown_lines: list[str], lines_per_page: int = 120):
    markers = _parse_page_markers(markdown_lines)
    if markers:
        spans: list[tuple[int, int, int]] = []
        for idx, (page_num, start_line) in enumerate(markers):
            end_line = markers[idx + 1][1] - 1 if idx + 1 < len(markers) else len(markdown_lines)
            spans.append((page_num, start_line, end_line))

        def _line_to_page(line_num: int | None) -> int | None:
            if not isinstance(line_num, int) or line_num <= 0:
                return None
            for page_num, start_line, end_line in spans:
                if start_line <= line_num <= end_line:
                    return page_num
            return spans[-1][0] if spans else None

        return _line_to_page

    lines_per_page = max(1, int(lines_per_page))

    def _line_to_page(line_num: int | None) -> int | None:
        if not isinstance(line_num, int) or line_num <= 0:
            return None
        return ((line_num - 1) // lines_per_page) + 1

    return _line_to_page


def extract_nodes_from_markdown(markdown_content: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract Markdown ATX headings while ignoring fenced code blocks."""
    nodes: list[dict[str, Any]] = []
    lines = markdown_content.splitlines()
    in_code_block = False

    for line_num, line in enumerate(lines, 1):
        stripped = line.strip()
        if CODE_FENCE_RE.match(stripped):
            in_code_block = not in_code_block
            continue
        if in_code_block or not stripped:
            continue
        match = HEADER_RE.match(stripped)
        if match:
            nodes.append(
                {
                    "title": match.group(2).strip(),
                    "line_num": line_num,
                    "level": len(match.group(1)),
                }
            )
    return nodes, lines


def extract_node_text_content(node_list: list[dict[str, Any]], markdown_lines: list[str]) -> list[dict[str, Any]]:
    """Attach line spans and text to heading nodes."""
    if not node_list:
        content = _strip_page_markers_from_text("\n".join(markdown_lines).strip())
        if not content:
            return []
        return [
            {
                "title": "全文",
                "line_num": 1,
                "level": 1,
                "start_line": 1,
                "end_line": max(1, len(markdown_lines)),
                "text": content,
            }
        ]

    result: list[dict[str, Any]] = []
    for idx, node in enumerate(node_list):
        start_line = int(node["line_num"])
        end_line = int(node_list[idx + 1]["line_num"]) - 1 if idx + 1 < len(node_list) else len(markdown_lines)
        raw_text = "\n".join(markdown_lines[start_line - 1:end_line]).strip()
        result.append(
            {
                "title": node["title"],
                "line_num": start_line,
                "level": int(node["level"]),
                "start_line": start_line,
                "end_line": max(start_line, end_line),
                "text": _strip_page_markers_from_text(raw_text),
            }
        )
    return result


def attach_page_indices(node_list: list[dict[str, Any]], line_to_page) -> list[dict[str, Any]]:
    for node in node_list:
        start_page = line_to_page(node.get("start_line"))
        end_page = line_to_page(node.get("end_line"))
        if isinstance(start_page, int):
            node["start_index"] = start_page
        if isinstance(end_page, int):
            node["end_index"] = end_page
        elif isinstance(start_page, int):
            node["end_index"] = start_page
    return node_list


def update_node_list_with_text_token_count(node_list: list[dict[str, Any]], model: str | None = None) -> list[dict[str, Any]]:
    result = [dict(node) for node in node_list]
    for node in result:
        node["text_token_count"] = count_tokens(str(node.get("text") or ""), model=model)
    return result


def tree_thinning_for_index(
    node_list: list[dict[str, Any]],
    min_node_token: int | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Merge tiny child nodes into their closest parent."""
    if not min_node_token:
        return node_list
    result = [dict(node) for node in node_list]
    remove_indices: set[int] = set()

    for i in range(len(result) - 1, -1, -1):
        if i in remove_indices:
            continue
        node = result[i]
        if int(node.get("text_token_count") or 0) >= int(min_node_token):
            continue
        parent_index = None
        for j in range(i - 1, -1, -1):
            if int(result[j].get("level") or 1) < int(node.get("level") or 1):
                parent_index = j
                break
        if parent_index is None:
            continue
        parent_text = str(result[parent_index].get("text") or "").rstrip()
        child_text = str(node.get("text") or "").strip()
        if child_text:
            result[parent_index]["text"] = (parent_text + "\n\n" + child_text).strip()
            result[parent_index]["text_token_count"] = count_tokens(result[parent_index]["text"], model=model)
        remove_indices.add(i)

    return [node for idx, node in enumerate(result) if idx not in remove_indices]


def build_tree_from_nodes(node_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stack: list[tuple[dict[str, Any], int]] = []
    roots: list[dict[str, Any]] = []
    for node in node_list:
        level = int(node.get("level") or 1)
        tree_node = {
            "title": node.get("title") or "未命名节点",
            "text": node.get("text") or "",
            "start_index": node.get("start_index"),
            "end_index": node.get("end_index"),
            "nodes": [],
        }
        while stack and stack[-1][1] >= level:
            stack.pop()
        if stack:
            stack[-1][0]["nodes"].append(tree_node)
        else:
            roots.append(tree_node)
        stack.append((tree_node, level))
    return roots


def build_pages_from_markdown(markdown_lines: list[str], lines_per_page: int = 120) -> list[dict[str, Any]]:
    if not markdown_lines:
        return []
    markers = _parse_page_markers(markdown_lines)
    if markers:
        pages: list[dict[str, Any]] = []
        for idx, (page_number, start_line) in enumerate(markers):
            end_line = markers[idx + 1][1] - 1 if idx + 1 < len(markers) else len(markdown_lines)
            content = _strip_page_markers_from_text("\n".join(markdown_lines[start_line - 1:end_line]).strip())
            pages.append({"page": int(page_number), "content": content})
        return pages

    pages = []
    lines_per_page = max(1, int(lines_per_page))
    for page_number, start in enumerate(range(0, len(markdown_lines), lines_per_page), 1):
        content = "\n".join(markdown_lines[start:start + lines_per_page]).strip()
        if content:
            pages.append({"page": page_number, "content": content})
    return pages


async def _get_node_summary(node: dict[str, Any], summary_token_threshold: int, model: str | None = None) -> str:
    text = str(node.get("text") or "")
    if count_tokens(text, model=model) < int(summary_token_threshold or 800):
        return text[:320]
    return await generate_node_summary(node, model=model)


async def generate_summaries_for_structure_md(
    structure: list[dict[str, Any]],
    summary_token_threshold: int,
    model: str | None = None,
) -> list[dict[str, Any]]:
    nodes = structure_to_list(structure)
    factories = [
        lambda node=node: _get_node_summary(node, summary_token_threshold=summary_token_threshold, model=model)
        for node in nodes
    ]
    summaries = await gather_with_concurrency(factories, get_summary_concurrency())
    for node, summary in zip(nodes, summaries):
        if node.get("nodes"):
            node["prefix_summary"] = summary
        else:
            node["summary"] = summary
    return structure


async def md_to_tree(
    md_path: str,
    if_thinning: bool = False,
    min_token_threshold: int | None = None,
    if_add_node_summary: str = "no",
    summary_token_threshold: int | None = None,
    model: str | None = None,
    if_add_doc_description: str = "no",
    if_add_node_text: str = "no",
    if_add_node_id: str = "yes",
) -> dict[str, Any]:
    """Parse Markdown into PageIndex-style JSON."""
    with open(md_path, "r", encoding="utf-8-sig") as f:
        markdown_content = f.read()

    line_count = markdown_content.count("\n") + 1 if markdown_content else 0
    node_list, markdown_lines = extract_nodes_from_markdown(markdown_content)
    pages = build_pages_from_markdown(markdown_lines)
    nodes = extract_node_text_content(node_list, markdown_lines)
    nodes = attach_page_indices(nodes, _build_line_to_page_mapper(markdown_lines))

    if if_thinning:
        nodes = update_node_list_with_text_token_count(nodes, model=model)
        nodes = tree_thinning_for_index(nodes, min_token_threshold, model=model)

    tree = build_tree_from_nodes(nodes)
    if if_add_node_id == "yes":
        write_node_id(tree)

    if if_add_node_summary == "yes":
        tree = format_structure(
            tree,
            order=["title", "node_id", "start_index", "end_index", "summary", "prefix_summary", "text", "nodes"],
        )
        tree = await generate_summaries_for_structure_md(
            tree,
            summary_token_threshold=int(summary_token_threshold or 800),
            model=model,
        )
        if if_add_node_text == "no":
            tree = format_structure(
                tree,
                order=["title", "node_id", "start_index", "end_index", "summary", "prefix_summary", "nodes"],
            )
    else:
        order = ["title", "node_id", "start_index", "end_index", "summary", "prefix_summary", "nodes"]
        if if_add_node_text == "yes":
            order.insert(5, "text")
        tree = format_structure(tree, order=order)

    result = {
        "doc_name": os.path.splitext(os.path.basename(md_path))[0],
        "line_count": line_count,
        "page_count": len(pages),
        "pages": pages,
        "structure": tree,
    }
    if if_add_doc_description == "yes":
        result["doc_description"] = generate_doc_description(create_clean_structure_for_description(tree), model=model)
    return result

