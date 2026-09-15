#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from dotenv import dotenv_values

from app.api.schemas import RetrieveOptions, RetrieveRequest
from app.config.settings import Settings
from app.container import AppContainer
from app.llm.gateway import LLMRequest, LLMResult


@dataclass(slots=True)
class ProviderRecord:
    phase: str
    status: str
    duration_ms: int
    output: dict[str, Any]
    error_category: str | None = None


class RecordingProvider:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.records: list[ProviderRecord] = []

    async def complete_json(self, request: LLMRequest) -> LLMResult:
        started = monotonic()
        try:
            result = await self.delegate.complete_json(request)
        except Exception as exc:
            self.records.append(
                ProviderRecord(
                    phase=request.phase,
                    status="failed",
                    duration_ms=int((monotonic() - started) * 1000),
                    output={},
                    error_category=str(getattr(exc, "error_category", type(exc).__name__)),
                )
            )
            raise
        output: dict[str, Any] = {"structured_output": result.data}
        if result.tool_calls:
            output["tool_calls"] = [
                {"name": call.name, "arguments": call.arguments} for call in result.tool_calls
            ]
        self.records.append(
            ProviderRecord(
                phase=request.phase,
                status="ok",
                duration_ms=int((monotonic() - started) * 1000),
                output=output,
            )
        )
        return result


def _settings(env_file: Path) -> Settings:
    values = dotenv_values(env_file)
    required = {
        "postgres_dsn": values.get("POSTGRES_DSN"),
        "ark_api_key": values.get("ARK_API_KEY"),
        "ark_base_url": values.get("ARK_BASE_URL"),
        "rag_llm_model": values.get("RAG_LLM_MODEL"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(f"smoke environment is missing required settings: {missing}")
    return Settings(
        _env_file=None,
        **required,
        rag_rate_limit_enabled=False,
        rag_debug_enabled=True,
        request_soft_deadline_seconds=600,
        request_hard_deadline_seconds=900,
        request_finalization_reserve_seconds=30,
        rag_default_return_tokens=4096,
        rag_max_return_tokens=32768,
    )


def _replace(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        output = value
        for original, replacement in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if original:
                output = output.replace(original, replacement)
        return output
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, tuple):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            str(_replace(key, replacements)): _replace(item, replacements)
            for key, item in value.items()
        }
    return value


def _response_summary(response: Any, replacements: dict[str, str]) -> dict[str, Any]:
    return _replace(
        {
            "chunk_count": len(response.chunks),
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "document_id": chunk.document_id,
                    "source_type": chunk.source_type,
                    "path": chunk.path,
                    "page_number": chunk.chunk_meta.get("page_number"),
                    "content_characters": len(chunk.content),
                    "content_truncated": bool(chunk.chunk_meta.get("content_truncated")),
                }
                for chunk in response.chunks
            ],
            "warning_codes": [warning.code for warning in response.warnings],
            "coverage": response.coverage.model_dump(),
            "usage": response.usage.model_dump(),
            "classification": response.debug.state_summary.get("groups_by_category")
            if response.debug
            else None,
        },
        replacements,
    )


def _render_report(
    *,
    generated_at: datetime,
    scope_count: int,
    repeat: int,
    cases: list[dict[str, Any]],
) -> str:
    lines = [
        "# Retrieval Service Live Smoke Test",
        "",
        f"- Generated at: `{generated_at.isoformat()}`",
        "- Backend: real read-only PostgreSQL evaluation scope and real Ark API",
        f"- Visible document count: `{scope_count}`",
        f"- Iterations per category: `{repeat}`",
        "- Sensitive document IDs, names and extracted query terms are redacted.",
        "",
        "## Summary",
        "",
        (
            "| Category | Success | LLM failures | Average latency (ms) | "
            "Average LLM calls | Average chunks |"
        ),
        "|---|---:|---:|---:|---:|---:|",
    ]
    for case in cases:
        runs = case["runs"]
        successful = [run for run in runs if run["status"] == "ok"]
        llm_failures = sum(
            record["status"] == "failed" for run in runs for record in run["llm_records"]
        )
        average_latency = round(
            sum(run["response"]["usage"]["latency_ms"] for run in successful)
            / max(1, len(successful)),
            1,
        )
        average_calls = round(
            sum(run["response"]["usage"]["llm_request_count"] for run in successful)
            / max(1, len(successful)),
            1,
        )
        average_chunks = round(
            sum(run["response"]["chunk_count"] for run in successful)
            / max(1, len(successful)),
            1,
        )
        lines.append(
            f"| `{case['category']}` | {len(successful)}/{len(runs)} | {llm_failures} | "
            f"{average_latency} | {average_calls} | {average_chunks} |"
        )
    lines.extend(["", "## Run Details", ""])
    for case in cases:
        lines.extend(
            [
                f"### {case['category']}",
                "",
                f"Query template: `{case['query_template']}`",
                "",
            ]
        )
        for run in case["runs"]:
            lines.extend(
                [
                    f"#### Iteration {run['iteration']}",
                    "",
                    "```json",
                    json.dumps(run, ensure_ascii=False, indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
    lines.extend(
        [
            "## Acceptance Notes",
            "",
            (
                "- Every successful run must classify into its expected category and return "
                "at least one chunk."
            ),
            "- `routed_direct` and `routed_focused` exercise strict OpenAI function calling.",
            (
                "- Focused tree scan records contain node IDs only; candidate page arrays are "
                "expanded by code."
            ),
            (
                "- LLM failures, rule fallback warnings, token truncation and incomplete "
                "coverage remain visible above."
            ),
            "",
        ]
    )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> None:
    container = AppContainer.build(_settings(args.env_file))
    recorder = RecordingProvider(container.provider)
    container.gateway.provider = recorder
    await container.start()
    try:
        profiles, _ = await container.db_executor.run(
            container.repository.fetch_scope,
            user_id=args.user_id,
            kb_id=args.kb_id,
            doc_ids=[],
            temp_doc_ids=[],
            session_id=None,
        )
        profile = None
        for candidate in profiles:
            if not (2 <= candidate.page_count <= 20 and candidate.node_count > 1):
                continue
            nodes = await container.db_executor.run(
                container.repository.fetch_nodes,
                user_id=args.user_id,
                kb_id=args.kb_id,
                doc_id=candidate.doc_id,
                session_id=None,
            )
            all_roots = [node for node in nodes if node.parent_node_id is None]
            roots = [
                node
                for node in all_roots
                if node.level is not None
                and node.start_page is not None
                and node.end_page is not None
            ]
            if roots and len(roots) == len(all_roots) and all(
                any(
                    child.parent_node_id == root.node_id
                    and child.level == root.level + 1
                    and child.start_page is not None
                    and child.end_page is not None
                    and child.start_page > 0
                    and child.end_page >= child.start_page
                    for child in nodes
                )
                for root in roots
            ):
                profile = candidate
                break
        if profile is None:
            raise RuntimeError("no multi-page document with a usable node tree was found")
        first_page = (
            await container.db_executor.run(
                container.repository.fetch_pages,
                user_id=args.user_id,
                kb_id=args.kb_id,
                doc_id=profile.doc_id,
                pages=[1],
                session_id=None,
            )
        )[0]
        terms = re.findall(r"[\u4e00-\u9fff]{4,}", first_page.content)
        focused_term = terms[0][:8] if terms else profile.doc_name[:8]
        replacements = {
            profile.doc_id: "<DOC_ID>",
            profile.doc_name: "<DOC_NAME>",
            focused_term: "<FOCUSED_TERM>",
        }
        common = {
            "user_id": args.user_id,
            "kb_id": args.kb_id,
            "top_k": 3,
            "max_return_tokens": 4096,
            "search_mode": "semantic",
            "options": RetrieveOptions(include_debug=True, ensure_document_coverage=True),
        }
        definitions = [
            ("scope_direct", "你能看到哪些文档", [], "你能看到哪些文档"),
            ("routed_direct", "这篇文档有几页", [profile.doc_id], "这篇文档有几页"),
            (
                "routed_focused",
                f"这篇文档中关于{focused_term}的具体内容是什么",
                [profile.doc_id],
                "这篇文档中关于<FOCUSED_TERM>的具体内容是什么",
            ),
            (
                "routed_broad",
                "请详细总结这篇文档",
                [profile.doc_id],
                "请详细总结这篇文档",
            ),
        ]
        cases: list[dict[str, Any]] = []
        for category, query, doc_ids, query_template in definitions:
            runs: list[dict[str, Any]] = []
            for iteration in range(1, args.repeat + 1):
                record_start = len(recorder.records)
                try:
                    response = await container.engine.retrieve(
                        RetrieveRequest(
                            **common,
                            query=query,
                            doc_ids=doc_ids,
                        )
                    )
                    run: dict[str, Any] = {
                        "iteration": iteration,
                        "status": "ok",
                        "response": _response_summary(response, replacements),
                    }
                except Exception as exc:
                    run = {
                        "iteration": iteration,
                        "status": "failed",
                        "error": {
                            "type": type(exc).__name__,
                            "code": getattr(exc, "code", None),
                            "category": getattr(exc, "error_category", None),
                        },
                    }
                run["llm_records"] = _replace(
                    [
                        {
                            "phase": record.phase,
                            "status": record.status,
                            "duration_ms": record.duration_ms,
                            "output": record.output,
                            "error_category": record.error_category,
                        }
                        for record in recorder.records[record_start:]
                    ],
                    replacements,
                )
                if run["status"] == "ok":
                    acceptance_errors: list[str] = []
                    classification = run["response"]["classification"] or {}
                    if classification.get(category, 0) < 1:
                        acceptance_errors.append(f"expected classification {category}")
                    if run["response"]["chunk_count"] < 1:
                        acceptance_errors.append("retrieval returned no chunks")
                    if category == "routed_direct" and not any(
                        record["phase"] == "direct_tool_planner"
                        and record["output"].get("tool_calls")
                        for record in run["llm_records"]
                    ):
                        acceptance_errors.append("direct strict function call was not exercised")
                    if category == "routed_focused" and not any(
                        record["phase"].startswith("tree_navigation:")
                        and record["output"].get("tool_calls")
                        for record in run["llm_records"]
                    ):
                        acceptance_errors.append("focused tree function call was not exercised")
                    if acceptance_errors:
                        run["status"] = "failed"
                        run["acceptance_errors"] = acceptance_errors
                runs.append(run)
            cases.append(
                {"category": category, "query_template": query_template, "runs": runs}
            )
    finally:
        await container.close()
    report = _render_report(
        generated_at=datetime.now(UTC),
        scope_count=len(profiles),
        repeat=args.repeat,
        cases=cases,
    )
    args.output.write_text(report, encoding="utf-8")
    print(args.output)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run and record the four-category live smoke test."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(os.getenv("RAG_TEST_ENV_FILE", "")),
        required=not bool(os.getenv("RAG_TEST_ENV_FILE")),
    )
    parser.add_argument("--user-id", default="eval-user")
    parser.add_argument("--kb-id", default="eval-rag-kb")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs") / f"smoke-test-{datetime.now(UTC).date().isoformat()}.md",
    )
    args = parser.parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be positive")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
