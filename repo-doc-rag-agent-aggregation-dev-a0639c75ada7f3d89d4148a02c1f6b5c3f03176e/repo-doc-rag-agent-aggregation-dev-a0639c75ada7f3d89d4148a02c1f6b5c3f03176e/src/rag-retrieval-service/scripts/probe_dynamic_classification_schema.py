#!/usr/bin/env python3
"""Probe runtime-generated strict JSON schemas through the project's LLM adapter."""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config.settings import Settings  # noqa: E402
from app.llm.gateway import LLMCallError, LLMRequest  # noqa: E402
from app.llm.provider import OpenAICompatibleProvider  # noqa: E402
from app.workflows.classification.contracts import (  # noqa: E402
    binary_decision_schema as build_dynamic_binary_schema,
)
from app.workflows.classification.contracts import (  # noqa: E402
    validate_binary_decisions as validate_dynamic_binary_decisions,
)


@dataclass(frozen=True, slots=True)
class ProbeCase:
    name: str
    criterion: str
    queries: dict[str, str]
    expected: dict[str, bool]


_CASES = (
    ProbeCase(
        name="dynamic_scope_keys",
        criterion=(
            "true 表示问题询问当前请求范围这一文档集合的可见性、数量、可访问文档或集合级信息；"
            "false 表示问题在询问少量具体文档中的内容。"
        ),
        queries={
            "q_001": "你能查到几篇文档？",
            "q_007": "A公司的营业收入是多少？",
            "q_042": "你能查到哪些信息？",
        },
        expected={"q_001": True, "q_007": False, "q_042": True},
    ),
    ProbeCase(
        name="dynamic_direct_keys",
        criterion=(
            "true 表示问题只需直接读取指定文档在数据库中已有的页数、章节数、指定页、指定章节、"
            "目录、章节结构或文档摘要；false 表示不能仅靠这些字段直接回答。数据库已有摘要只支持"
            "简要概览；详细总结、深入解释或整体对比必须判为 false，因为它们需要大范围原文。"
        ),
        queries={
            "q_105": "这个文件有多少页？",
            "q_219": "请详细总结这篇文档的核心论证。",
        },
        expected={"q_105": True, "q_219": False},
    ),
)


def _prompt(case: ProbeCase) -> list[dict[str, str]]:
    query_lines = "\n".join(f"- {ref}: {query}" for ref, query in case.queries.items())
    return [
        {
            "role": "system",
            "content": (
                "# 角色与任务\n你是严格的二分类器，只做分类，不回答问题。\n\n"
                f"# 分类标准\n{case.criterion}\n\n"
                "# 输出约束\n逐项判断输入中的每个 query ref。严格遵守结构化输出契约，"
                "不得遗漏、增加或改写 query ref，不输出理由、置信度或答案。"
            ),
        },
        {"role": "user", "content": f"# 待分类问题\n{query_lines}"},
    ]


async def _run_case(
    provider: OpenAICompatibleProvider, settings: Settings, case: ProbeCase
) -> None:
    result = await provider.complete_json(
        LLMRequest(
            request_id=f"dynamic-schema-probe-{uuid4()}",
            phase=case.name,
            messages=_prompt(case),
            model=settings.rag_llm_model,
            max_tokens=min(512, settings.rag_llm_max_output_tokens),
            response_format=build_dynamic_binary_schema(list(case.queries), schema_name=case.name),
        )
    )
    decisions = validate_dynamic_binary_decisions(result.data, list(case.queries))
    if decisions != case.expected:
        raise ValueError(
            f"schema was valid but semantic decisions differed: "
            f"expected={case.expected}, actual={decisions}"
        )
    print(
        f"[PASS] {case.name}: refs={list(case.queries)}, decisions={decisions}, "
        f"tokens={result.usage.total_tokens}, provider_request_id={result.request_id or '-'}",
        flush=True,
    )


async def _main_async() -> int:
    settings = Settings()
    if not settings.ark_api_key.strip():
        print("[FAIL] ARK_API_KEY is not configured in the retrieval-service .env")
        return 2
    if not settings.rag_llm_model.strip():
        print("[FAIL] RAG_LLM_MODEL is not configured in the retrieval-service .env")
        return 2

    print(f"Base URL: {settings.ark_base_url}")
    print(f"Model: {settings.rag_llm_model}")
    print("API key: <redacted>")
    provider = OpenAICompatibleProvider(
        api_key=settings.ark_api_key,
        base_url=settings.ark_base_url,
        timeout_seconds=settings.rag_llm_timeout_seconds,
        max_retries=0,
    )
    try:
        for case in _CASES:
            await _run_case(provider, settings, case)
    except LLMCallError as exc:
        print(f"[FAIL] provider error category={exc.error_category}", flush=True)
        return 1
    except (TypeError, ValueError) as exc:
        print(f"[FAIL] {exc}", flush=True)
        return 1
    finally:
        await provider.close()
    print("[PASS] Doubao accepted both runtime-generated strict schemas.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe dynamic strict structured output with retrieval-service .env settings."
    )
    parser.parse_args()
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
