from __future__ import annotations

"""Minimal compatibility helpers for Markdown PageIndex parsing.

Only the functions used by ``page_index_md.py`` are kept here. The original
utility module also included PDF helpers, retrieval helpers, logging wrappers,
and broad LLM provider glue; those are intentionally excluded from the
standalone ingestion service.
"""

import asyncio
import os
from typing import Any, Awaitable, Callable


def count_tokens(text: str | None, model: str | None = None) -> int:
    """Return an approximate token count with an optional LiteLLM fast path."""
    if not text:
        return 0
    try:
        import litellm  # type: ignore

        return int(litellm.token_counter(model=model, text=text))
    except Exception:
        # Good enough for thinning/threshold decisions in the ingestion service.
        return max(1, len(str(text)) // 4)


def get_summary_concurrency(default: int = 8) -> int:
    """Read summary concurrency from env with a safe fallback."""
    raw = (
        os.getenv("PAGEINDEX_SUMMARY_CONCURRENCY")
        or os.getenv("SUMMARY_CONCURRENCY")
        or os.getenv("DA_SUMMARY_CONCURRENCY")
        or ""
    ).strip()
    if not raw:
        return max(1, int(default))
    try:
        return max(1, int(raw))
    except ValueError:
        return max(1, int(default))


async def gather_with_concurrency(
    task_factories: list[Callable[[], Awaitable[Any]]],
    concurrency: int,
) -> list[Any]:
    """Run async factories with bounded concurrency while preserving order."""
    if not task_factories:
        return []
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    results: list[Any] = [None] * len(task_factories)

    async def _runner(index: int, factory: Callable[[], Awaitable[Any]]) -> None:
        async with semaphore:
            results[index] = await factory()

    await asyncio.gather(*(_runner(i, fn) for i, fn in enumerate(task_factories)))
    return results


def write_node_id(data: Any, node_id: int = 0) -> int:
    """Assign zero-padded preorder node IDs in-place."""
    if isinstance(data, dict):
        data["node_id"] = str(node_id).zfill(4)
        node_id += 1
        children = data.get("nodes")
        if isinstance(children, list):
            node_id = write_node_id(children, node_id)
    elif isinstance(data, list):
        for item in data:
            node_id = write_node_id(item, node_id)
    return node_id


def structure_to_list(structure: Any) -> list[dict[str, Any]]:
    """Flatten a tree/list of nodes to preorder list."""
    if isinstance(structure, dict):
        nodes = [structure]
        children = structure.get("nodes")
        if isinstance(children, list):
            nodes.extend(structure_to_list(children))
        return nodes
    if isinstance(structure, list):
        nodes: list[dict[str, Any]] = []
        for item in structure:
            nodes.extend(structure_to_list(item))
        return nodes
    return []


def reorder_dict(data: dict[str, Any], key_order: list[str]) -> dict[str, Any]:
    """Return dict ordered by keys present in ``key_order``."""
    return {key: data[key] for key in key_order if key in data}


def format_structure(structure: Any, order: list[str] | None = None) -> Any:
    """Clean empty child arrays and optionally reorder keys."""
    if isinstance(structure, dict):
        children = structure.get("nodes")
        if isinstance(children, list):
            structure["nodes"] = format_structure(children, order)
        if not structure.get("nodes"):
            structure.pop("nodes", None)
        return reorder_dict(structure, order) if order else structure
    if isinstance(structure, list):
        return [format_structure(item, order) for item in structure]
    return structure


def create_clean_structure_for_description(structure: Any) -> Any:
    """Keep only compact fields needed for document description prompts."""
    if isinstance(structure, dict):
        clean = {
            key: structure[key]
            for key in ("title", "node_id", "summary", "prefix_summary")
            if key in structure
        }
        children = structure.get("nodes")
        if isinstance(children, list) and children:
            clean["nodes"] = create_clean_structure_for_description(children)
        return clean
    if isinstance(structure, list):
        return [create_clean_structure_for_description(item) for item in structure]
    return structure


async def generate_node_summary(node: dict[str, Any], model: str | None = None) -> str:
    """Local fallback summary for Markdown compatibility.

    The main ingestion parser already has the Doubao summarizer, so this helper
    intentionally avoids another LLM dependency and returns a compact text
    snippet.
    """
    text = str(node.get("text") or "").strip()
    return text[:320]


def generate_doc_description(structure: Any, model: str | None = None) -> str:
    """Build a simple document description from top-level titles."""
    titles = [
        str(node.get("title") or "").strip()
        for node in structure_to_list(structure)
        if str(node.get("title") or "").strip()
    ]
    if not titles:
        return ""
    return "主要内容包括：" + "、".join(titles[:8]) + "。"

