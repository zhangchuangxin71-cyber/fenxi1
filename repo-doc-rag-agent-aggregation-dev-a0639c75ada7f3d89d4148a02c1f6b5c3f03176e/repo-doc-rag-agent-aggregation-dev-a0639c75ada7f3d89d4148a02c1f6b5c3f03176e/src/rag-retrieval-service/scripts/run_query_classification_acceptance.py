#!/usr/bin/env python3
"""Run repeatable fast/robust query-classification acceptance against the real LLM."""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config.settings import Settings  # noqa: E402
from app.llm.circuit_breaker import LLMCircuitBreaker  # noqa: E402
from app.llm.gateway import LLMGateway  # noqa: E402
from app.llm.provider import OpenAICompatibleProvider  # noqa: E402
from app.workflows.classification.models import QueryGroup  # noqa: E402
from app.workflows.classification.robust_strategy import (  # noqa: E402
    RobustLLMClassificationStrategy,
)
from app.workflows.classification.strategies import (  # noqa: E402
    ClassificationService,
    FastLLMClassificationStrategy,
    RuleClassificationStrategy,
)

Category = Literal["scope_direct", "routed_direct", "routed_focused", "routed_broad"]
InputMode = Literal["str", "list"]
StrategyName = Literal["fast", "robust"]


@dataclass(frozen=True, slots=True)
class ExpectedQuestion:
    anchors: tuple[str, ...]
    category: Category
    document_key: str | None = None
    document_anchors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AcceptanceCase:
    name: str
    raw_query: str
    prepared_queries: tuple[str, ...]
    expected: tuple[ExpectedQuestion, ...]


@dataclass(frozen=True, slots=True)
class Evaluation:
    retained: int
    expected: int
    category_correct: int
    route_usable: int
    route_expected: int
    cluster_correct: int
    cluster_pairs: int


@dataclass(frozen=True, slots=True)
class RunRecord:
    strategy: StrategyName
    mode: InputMode
    case_name: str
    repetition: int
    success: bool
    schema_failure: bool
    fallback_used: bool
    latency_ms: int
    waiting_rounds: int
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    evaluation: Evaluation
    categories: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    error: str | None = None


CASES: tuple[AcceptanceCase, ...] = (
    AcceptanceCase(
        name="scope_visibility",
        raw_query="你能看到哪些文档？",
        prepared_queries=("你能看到哪些文档？",),
        expected=(ExpectedQuestion(("文档",), "scope_direct"),),
    ),
    AcceptanceCase(
        name="scope_count",
        raw_query="你能查到几篇文档？",
        prepared_queries=("你能查到几篇文档？",),
        expected=(ExpectedQuestion(("文档",), "scope_direct"),),
    ),
    AcceptanceCase(
        name="scope_information",
        raw_query="你能查到哪些信息？",
        prepared_queries=("你能查到哪些信息？",),
        expected=(ExpectedQuestion(("信息",), "scope_direct"),),
    ),
    AcceptanceCase(
        name="scope_summary",
        raw_query="这些文档主要讲了什么？",
        prepared_queries=("这些文档主要讲了什么？",),
        expected=(ExpectedQuestion(("文档",), "scope_direct"),),
    ),
    AcceptanceCase(
        name="direct_page_count",
        raw_query="中国铁物2023年半年度报告文件有多少页？",
        prepared_queries=("中国铁物2023年半年度报告文件有多少页？",),
        expected=(
            ExpectedQuestion(
                ("中国铁物",),
                "routed_direct",
                "china_rail_report",
                ("中国铁物", "半年度报告"),
            ),
        ),
    ),
    AcceptanceCase(
        name="focused_fact",
        raw_query="《中华人民共和国能源法》中规定的能源规划类型有哪些？",
        prepared_queries=("《中华人民共和国能源法》中规定的能源规划类型有哪些？",),
        expected=(
            ExpectedQuestion(
                ("能源规划",),
                "routed_focused",
                "energy_law",
                ("中华人民共和国能源法",),
            ),
        ),
    ),
    AcceptanceCase(
        name="broad_summary",
        raw_query="请详细总结《中华人民共和国能源法》的内容。",
        prepared_queries=("请详细总结《中华人民共和国能源法》的内容。",),
        expected=(
            ExpectedQuestion(
                ("中华人民共和国能源法",),
                "routed_broad",
                "energy_law",
                ("中华人民共和国能源法",),
            ),
        ),
    ),
    AcceptanceCase(
        name="cross_document_fact",
        raw_query="龙江交通和新乡化纤的2023年营业收入谁更高？",
        prepared_queries=(
            "龙江交通2023年营业收入是多少？",
            "新乡化纤2023年营业收入是多少？",
        ),
        expected=(
            ExpectedQuestion(
                ("龙江交通", "营业收入"),
                "routed_focused",
                "longjiang_report",
                ("龙江交通",),
            ),
            ExpectedQuestion(
                ("新乡化纤", "营业收入"),
                "routed_focused",
                "xinxiang_report",
                ("新乡化纤",),
            ),
        ),
    ),
    AcceptanceCase(
        name="mixed_three_categories",
        raw_query=(
            "你能看到哪些文档？《中华人民共和国能源法》中规定的能源规划类型有哪些？"
            "这个文件的章节结构是什么？"
        ),
        prepared_queries=(
            "你能看到哪些文档？",
            "《中华人民共和国能源法》中规定的能源规划类型有哪些？",
            "《中华人民共和国能源法》的章节结构是什么？",
        ),
        expected=(
            ExpectedQuestion(("文档",), "scope_direct"),
            ExpectedQuestion(
                ("能源规划",),
                "routed_focused",
                "energy_law",
                ("中华人民共和国能源法",),
            ),
            ExpectedQuestion(
                ("章节结构",),
                "routed_direct",
                "energy_law",
                ("中华人民共和国能源法",),
            ),
        ),
    ),
    AcceptanceCase(
        name="ambiguous_document_content",
        raw_query="《中华人民共和国能源法》这份文件大致怎么样？",
        prepared_queries=("《中华人民共和国能源法》这份文件大致怎么样？",),
        expected=(
            ExpectedQuestion(
                ("中华人民共和国能源法",),
                "routed_broad",
                "energy_law",
                ("中华人民共和国能源法",),
            ),
        ),
    ),
)


def _compact(value: str) -> str:
    return "".join(value.casefold().split()).replace("《", "").replace("》", "")


def _route_signature(group: QueryGroup) -> tuple[str, tuple[str, ...]]:
    return (
        _compact(group.target_docs_description or ""),
        tuple(sorted(_compact(item) for item in group.target_docs_keywords)),
    )


def evaluate_groups(case: AcceptanceCase, groups: list[QueryGroup]) -> Evaluation:
    candidates = [(group, question) for group in groups for question in group.queries]
    used: set[int] = set()
    matches: list[tuple[ExpectedQuestion, QueryGroup] | None] = []
    for expected in case.expected:
        found: tuple[int, QueryGroup] | None = None
        for index, (group, question) in enumerate(candidates):
            if index in used:
                continue
            normalized = _compact(question)
            if all(_compact(anchor) in normalized for anchor in expected.anchors):
                found = (index, group)
                break
        if found is None:
            matches.append(None)
            continue
        used.add(found[0])
        matches.append((expected, found[1]))

    retained = sum(match is not None for match in matches)
    category_correct = sum(
        match is not None and match[1].category == match[0].category for match in matches
    )
    routed_matches = [
        match for match in matches if match is not None and match[0].document_key is not None
    ]
    route_usable = 0
    for expected, group in routed_matches:
        route_text = _compact(
            " ".join([group.target_docs_description or "", *group.target_docs_keywords])
        )
        if (
            group.target_docs_description
            and group.target_docs_keywords
            and any(_compact(anchor) in route_text for anchor in expected.document_anchors)
        ):
            route_usable += 1

    cluster_correct = 0
    cluster_pairs = 0
    for left_index, left in enumerate(routed_matches):
        for right in routed_matches[left_index + 1 :]:
            cluster_pairs += 1
            expected_same = left[0].document_key == right[0].document_key
            actual_same = _route_signature(left[1]) == _route_signature(right[1])
            cluster_correct += expected_same == actual_same

    return Evaluation(
        retained=retained,
        expected=len(case.expected),
        category_correct=category_correct,
        route_usable=route_usable,
        route_expected=len(routed_matches),
        cluster_correct=cluster_correct,
        cluster_pairs=cluster_pairs,
    )


def _waiting_rounds(phases: dict[str, int]) -> int:
    if "classification" in phases:
        return 1
    rounds = int("robust_query_rewrite" in phases) + int("robust_scope_classifier" in phases)
    if any(
        phase in phases
        for phase in (
            "robust_direct_classifier",
            "robust_focused_classifier",
            "robust_target_document_grouping",
        )
    ):
        rounds += 1
    return rounds


def _percent(numerator: int, denominator: int) -> str:
    return "N/A" if denominator == 0 else f"{100 * numerator / denominator:.2f}%"


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _aggregate(records: list[RunRecord]) -> dict[str, Any]:
    evaluations = [record.evaluation for record in records]
    successful = [record for record in records if record.success]
    return {
        "runs": len(records),
        "successes": len(successful),
        "retained": sum(item.retained for item in evaluations),
        "expected": sum(item.expected for item in evaluations),
        "category_correct": sum(item.category_correct for item in evaluations),
        "route_usable": sum(item.route_usable for item in evaluations),
        "route_expected": sum(item.route_expected for item in evaluations),
        "cluster_correct": sum(item.cluster_correct for item in evaluations),
        "cluster_pairs": sum(item.cluster_pairs for item in evaluations),
        "schema_failures": sum(record.schema_failure for record in records),
        "fallbacks": sum(record.fallback_used for record in records),
        "p50_latency": int(statistics.median(record.latency_ms for record in records)),
        "p95_latency": _percentile([record.latency_ms for record in records], 0.95),
        "avg_calls": statistics.fmean(record.llm_calls for record in records),
        "p95_calls": _percentile([record.llm_calls for record in records], 0.95),
        "avg_rounds": statistics.fmean(record.waiting_rounds for record in records),
        "prompt_tokens": sum(record.prompt_tokens for record in records),
        "completion_tokens": sum(record.completion_tokens for record in records),
        "total_tokens": sum(record.total_tokens for record in records),
    }


def render_report(
    *, settings: Settings, records: list[RunRecord], repeats_per_mode: int, concurrency: int
) -> str:
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    host = urlparse(settings.ark_base_url).hostname or "unknown"
    lines = [
        "# 鲁棒 Query 分类真实豆包 API 验收报告",
        "",
        f"- 生成时间：`{generated_at}`",
        f"- 模型：`{settings.rag_llm_model}`",
        f"- API host：`{host}`",
        (
            f"- 每个案例：字符串与列表输入各 `{repeats_per_mode}` 次，"
            f"合计 `{repeats_per_mode * 2}` 次/策略"
        ),
        f"- 受控并发：`{concurrency}` 个分类请求；所有模型调用仍经过项目全局 `LLMGateway`",
        "- API key、完整 prompt 和完整模型响应未写入报告。",
        "",
        "## 总体结果",
        "",
        (
            "| 策略 | 输入 | 运行成功 | 子问题保留率 | 分类准确率 | 文档分组准确率 | "
            "路由参数可用率 | Schema 失败率 | Fallback 率 | P50/P95 耗时 | "
            "平均/P95 calls | 平均等待轮次 | Prompt/Completion/Total tokens |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    grouped: dict[tuple[str, str], list[RunRecord]] = defaultdict(list)
    for record in records:
        grouped[(record.strategy, record.mode)].append(record)
    for strategy in ("fast", "robust"):
        for mode in ("str", "list"):
            summary = _aggregate(grouped[(strategy, mode)])
            lines.append(
                "| {strategy} | {mode} | {successes}/{runs} | {retention} | {category} | "
                "{cluster} | {route} | {schema} | {fallback} | {p50}/{p95} ms | "
                "{avg_calls:.2f}/{p95_calls} | {avg_rounds:.2f} | "
                "{prompt_tokens}/{completion_tokens}/{total_tokens} |".format(
                    strategy=strategy,
                    mode=mode,
                    successes=summary["successes"],
                    runs=summary["runs"],
                    retention=_percent(summary["retained"], summary["expected"]),
                    category=_percent(summary["category_correct"], summary["expected"]),
                    cluster=_percent(summary["cluster_correct"], summary["cluster_pairs"]),
                    route=_percent(summary["route_usable"], summary["route_expected"]),
                    schema=_percent(summary["schema_failures"], summary["runs"]),
                    fallback=_percent(summary["fallbacks"], summary["runs"]),
                    p50=summary["p50_latency"],
                    p95=summary["p95_latency"],
                    avg_calls=summary["avg_calls"],
                    p95_calls=summary["p95_calls"],
                    avg_rounds=summary["avg_rounds"],
                    prompt_tokens=summary["prompt_tokens"],
                    completion_tokens=summary["completion_tokens"],
                    total_tokens=summary["total_tokens"],
                )
            )

    lines.extend(
        [
            "",
            "## 分案例结果",
            "",
            "| 策略 | 案例 | 子问题保留率 | 分类准确率 | 失败次数 | P95 耗时 | 平均 calls |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    by_case: dict[tuple[str, str], list[RunRecord]] = defaultdict(list)
    for record in records:
        by_case[(record.strategy, record.case_name)].append(record)
    for strategy in ("fast", "robust"):
        for case in CASES:
            case_records = by_case[(strategy, case.name)]
            summary = _aggregate(case_records)
            lines.append(
                f"| {strategy} | `{case.name}` | "
                f"{_percent(summary['retained'], summary['expected'])} | "
                f"{_percent(summary['category_correct'], summary['expected'])} | "
                f"{summary['runs'] - summary['successes']} | {summary['p95_latency']} ms | "
                f"{summary['avg_calls']:.2f} |"
            )

    failures = [
        record
        for record in records
        if not record.success
        or record.evaluation.retained != record.evaluation.expected
        or record.evaluation.category_correct != record.evaluation.expected
    ]
    lines.extend(["", "## 失败与误分类样本", ""])
    if not failures:
        lines.append("未发现运行失败、子问题丢失或分类错误。")
    else:
        lines.append("仅列出前 30 个样本；错误文本已限制为异常类型或本地校验消息。")
        lines.append("")
        for record in failures[:30]:
            lines.append(
                f"- `{record.strategy}/{record.mode}/{record.case_name}` 第 "
                f"{record.repetition} 次：categories={list(record.categories)}, "
                f"questions={list(record.questions)}, "
                f"retained={record.evaluation.retained}/{record.evaluation.expected}, "
                f"category_correct={record.evaluation.category_correct}/"
                f"{record.evaluation.expected}, error={record.error or '-'}"
            )

    robust = _aggregate([record for record in records if record.strategy == "robust"])
    fast = _aggregate([record for record in records if record.strategy == "fast"])
    robust_pass = (
        robust["successes"] == robust["runs"]
        and robust["retained"] == robust["expected"]
        and robust["category_correct"] == robust["expected"]
        and robust["schema_failures"] == 0
    )
    lines.extend(
        [
            "",
            "## 验收结论",
            "",
            f"- Robust 严格门槛：**{'通过' if robust_pass else '未通过'}**。门槛要求运行、"
            "子问题保留、分类和 strict schema 均无失败。",
            f"- Fast 总体子问题保留率：{_percent(fast['retained'], fast['expected'])}；"
            f"Robust：{_percent(robust['retained'], robust['expected'])}。",
            f"- Fast 总体分类准确率：{_percent(fast['category_correct'], fast['expected'])}；"
            f"Robust：{_percent(robust['category_correct'], robust['expected'])}。",
            "- 延迟是本次受控并发条件下的端到端分类节点耗时，包含网关排队和模型服务波动，"
            "不包含数据库、文档路由或 chunk 检索。",
            "",
        ]
    )
    return "\n".join(lines)


async def _run_one(
    *,
    strategy_name: StrategyName,
    strategy: Any,
    gateway: LLMGateway,
    case: AcceptanceCase,
    mode: InputMode,
    repetition: int,
) -> RunRecord:
    request_id = f"classification-acceptance-{uuid4()}"
    query: str | list[str] = case.raw_query if mode == "str" else list(case.prepared_queries)
    gateway.begin_request(request_id, debug_enabled=True)
    started = monotonic()
    groups: list[QueryGroup] = []
    error: str | None = None
    schema_failure = False
    fallback_used = False
    try:
        result = await strategy.classify(
            request_id=request_id,
            query=query,
            scope_document_count=100,
        )
        groups = result.groups
        fallback_used = bool(result.degraded or result.warnings)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
        schema_failure = getattr(exc, "error_category", None) == "invalid_response" or isinstance(
            exc, (TypeError, ValueError)
        )
    latency_ms = int((monotonic() - started) * 1000)
    stats = gateway.stats(request_id)
    if strategy_name == "fast" and fallback_used:
        schema_failure = True
    evaluation = evaluate_groups(case, groups)
    record = RunRecord(
        strategy=strategy_name,
        mode=mode,
        case_name=case.name,
        repetition=repetition,
        success=error is None,
        schema_failure=schema_failure,
        fallback_used=fallback_used,
        latency_ms=latency_ms,
        waiting_rounds=_waiting_rounds(stats.phase_counts),
        llm_calls=stats.request_count,
        prompt_tokens=stats.prompt_tokens,
        completion_tokens=stats.completion_tokens,
        total_tokens=stats.total_tokens,
        evaluation=evaluation,
        categories=tuple(group.category for group in groups),
        questions=tuple(question for group in groups for question in group.queries),
        error=error,
    )
    gateway.clear_stats(request_id)
    return record


async def _run_acceptance(
    *, settings: Settings, repeats_per_mode: int, concurrency: int
) -> list[RunRecord]:
    provider = OpenAICompatibleProvider(
        api_key=settings.ark_api_key,
        base_url=settings.ark_base_url,
        timeout_seconds=settings.rag_llm_timeout_seconds,
        max_retries=settings.rag_llm_max_retries,
    )
    gateway = LLMGateway(
        provider=provider,
        max_concurrency=min(concurrency * 3, settings.rag_llm_max_concurrency),
        max_queued_calls=settings.rag_llm_max_queued_calls,
        per_request_max_in_flight=settings.rag_llm_per_request_max_in_flight,
        circuit_breaker=LLMCircuitBreaker(
            enabled=settings.rag_llm_circuit_breaker_enabled,
            failure_threshold=settings.rag_llm_circuit_failure_threshold,
            recovery_seconds=settings.rag_llm_circuit_recovery_seconds,
        ),
    )
    strategies = {
        "fast": ClassificationService(
            primary=FastLLMClassificationStrategy(
                gateway=gateway,
                model=settings.rag_llm_model,
                max_tokens=settings.rag_llm_max_output_tokens,
            ),
            fallback=RuleClassificationStrategy(),
        ),
        "robust": RobustLLMClassificationStrategy(
            gateway=gateway,
            model=settings.rag_llm_model,
            max_tokens=settings.rag_llm_max_output_tokens,
        ),
    }
    jobs = [
        (strategy_name, case, mode, repetition)
        for strategy_name in ("fast", "robust")
        for case in CASES
        for mode in ("str", "list")
        for repetition in range(1, repeats_per_mode + 1)
    ]
    semaphore = asyncio.Semaphore(concurrency)
    completed = 0
    completed_lock = asyncio.Lock()

    async def run_job(
        strategy_name: StrategyName,
        case: AcceptanceCase,
        mode: InputMode,
        repetition: int,
    ) -> RunRecord:
        nonlocal completed
        async with semaphore:
            record = await _run_one(
                strategy_name=strategy_name,
                strategy=strategies[strategy_name],
                gateway=gateway,
                case=case,
                mode=mode,
                repetition=repetition,
            )
        async with completed_lock:
            completed += 1
            if completed % 20 == 0 or completed == len(jobs):
                print(f"completed {completed}/{len(jobs)} classification runs", flush=True)
        return record

    try:
        return await asyncio.gather(*(run_job(*job) for job in jobs))
    finally:
        await gateway.close()
        await provider.close()


async def _main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.ark_api_key.strip() or not settings.rag_llm_model.strip():
        print("ARK_API_KEY and RAG_LLM_MODEL must be configured in retrieval-service .env")
        return 2
    records = await _run_acceptance(
        settings=settings,
        repeats_per_mode=args.repeats_per_mode,
        concurrency=args.concurrency,
    )
    report = render_report(
        settings=settings,
        records=records,
        repeats_per_mode=args.repeats_per_mode,
        concurrency=args.concurrency,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(f"wrote {output}")
    robust = _aggregate([record for record in records if record.strategy == "robust"])
    return int(
        robust["successes"] != robust["runs"]
        or robust["retained"] != robust["expected"]
        or robust["category_correct"] != robust["expected"]
        or robust["schema_failures"] != 0
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repeats-per-mode",
        type=int,
        default=10,
        help="repetitions for each str/list case; default 10 gives 20 runs per boundary",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "docs" / "robust-query-classification-acceptance.md"),
    )
    args = parser.parse_args()
    if args.repeats_per_mode < 1 or args.concurrency < 1:
        parser.error("repeats-per-mode and concurrency must be positive")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
