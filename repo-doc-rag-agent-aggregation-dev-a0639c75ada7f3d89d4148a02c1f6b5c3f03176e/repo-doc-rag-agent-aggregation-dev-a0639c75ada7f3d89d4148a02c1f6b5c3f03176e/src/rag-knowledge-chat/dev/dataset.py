from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any


def _load_labels(manifest_dir: Path) -> list[dict[str, Any]]:
    path = manifest_dir / "query_labels.jsonl"
    if not path.is_file():
        raise ValueError(f"query label manifest not found: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid query label at line {line_number}") from exc
        if not all(row.get(key) for key in ("query_id", "query_text", "doc_id")):
            raise ValueError(f"incomplete query label at line {line_number}")
        rows.append(row)
    if not rows:
        raise ValueError("query label manifest is empty")
    return rows


def generate_request(
    *,
    manifest_dir: Path,
    query_id: str | None,
    user_id: str,
    kb_id: str,
    noise_count: int,
    seed: int,
    top_k: int = 5,
    max_return_tokens: int = 32768,
) -> dict[str, Any]:
    rows = _load_labels(manifest_dir)
    selected = next((row for row in rows if row["query_id"] == query_id), None)
    if selected is None:
        selected = random.Random(seed).choice(rows)
    gold = str(selected["doc_id"])
    pool = sorted({str(row["doc_id"]) for row in rows if str(row["doc_id"]) != gold})
    if noise_count < 0 or noise_count > len(pool):
        raise ValueError("noise_count exceeds available noise documents")
    noise = random.Random(seed).sample(pool, noise_count)
    return {
        "model": "rag-knowledge-chat",
        "messages": [{"role": "user", "content": str(selected["query_text"])}],
        "stream": True,
        "rag": {
            "user_id": user_id,
            "kb_id": kb_id,
            "doc_ids": [gold, *noise],
            "temp_doc_ids": [],
            "incremental_doc_ids": [],
            "top_k": top_k,
            "max_return_tokens": max_return_tokens,
            "include_debug": True,
        },
    }
