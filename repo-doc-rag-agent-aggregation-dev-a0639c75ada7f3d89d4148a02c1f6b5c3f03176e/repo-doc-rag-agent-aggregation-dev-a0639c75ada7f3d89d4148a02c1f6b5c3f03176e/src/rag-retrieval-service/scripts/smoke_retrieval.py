#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        raise SystemExit(f"retrieval smoke request failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise SystemExit("retrieval smoke request could not reach the service") from exc


def _safe_summary(name: str, response: dict[str, Any]) -> dict[str, Any]:
    chunks = response.get("chunks") or []
    if not chunks:
        raise SystemExit(f"{name} smoke request returned no chunks")
    return {
        "case": name,
        "chunk_count": len(chunks),
        "source_types": sorted({str(chunk.get("source_type")) for chunk in chunks}),
        "warning_codes": [warning.get("code") for warning in response.get("warnings", [])],
        "coverage_complete": bool((response.get("coverage") or {}).get("complete")),
        "usage": {
            key: (response.get("usage") or {}).get(key)
            for key in ("latency_ms", "llm_request_count", "total_tokens", "returned_tokens")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run four safe retrieval smoke requests without printing document content."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8120")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--doc-id", required=True)
    parser.add_argument(
        "--focused-query",
        required=True,
        help="A precise fact/process query known to have evidence in --doc-id.",
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    endpoint = f"{args.base_url.rstrip('/')}/rag/v1/retrieve"
    common = {
        "user_id": args.user_id,
        "kb_id": args.kb_id,
        "top_k": 3,
        "max_return_tokens": 2048,
        "search_mode": "semantic",
        "options": {"include_debug": True, "ensure_document_coverage": True},
    }
    cases = [
        ("scope_direct", {**common, "query": "你能看到哪些文档", "doc_ids": []}),
        (
            "routed_direct",
            {**common, "query": "这篇文档有几页", "doc_ids": [args.doc_id]},
        ),
        (
            "routed_focused",
            {**common, "query": args.focused_query, "doc_ids": [args.doc_id]},
        ),
        (
            "routed_broad",
            {**common, "query": "请详细总结这篇文档", "doc_ids": [args.doc_id]},
        ),
    ]
    summaries = [
        _safe_summary(name, _post(endpoint, payload, args.timeout)) for name, payload in cases
    ]
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
