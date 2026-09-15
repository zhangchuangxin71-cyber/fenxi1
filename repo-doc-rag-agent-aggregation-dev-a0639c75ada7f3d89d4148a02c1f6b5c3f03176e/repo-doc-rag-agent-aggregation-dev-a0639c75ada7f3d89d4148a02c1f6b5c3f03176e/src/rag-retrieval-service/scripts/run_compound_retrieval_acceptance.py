#!/usr/bin/env python3
"""Compare fast and robust classification through the full retrieval HTTP API."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlparse

Category = Literal["scope_direct", "routed_direct", "routed_focused", "routed_broad"]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

HUANGSHAN = "f988c3ca-288e-5e3f-b4bc-b4c3039bacf5"
METROLOGY = "b2a72911-c98e-5160-8bd7-760ed7132868"
ENERGY = "b962e80e-6cf4-519d-ac36-c8b75ddc58c9"
SCONE = "e9be3e60-19df-5ed8-b61c-d6dd7dab2e38"
SCHISTOSOMIASIS = "76022cd1-39e0-5f96-b6d8-657c79c21c0f"
DOCUMENT_SCOPE = (HUANGSHAN, METROLOGY, ENERGY, SCONE, SCHISTOSOMIASIS)


@dataclass(frozen=True, slots=True)
class ExpectedQuestion:
    anchors: tuple[str, ...]
    category: Category
    document_id: str | None = None
    content_anchors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompoundCase:
    name: str
    query: str
    expected: tuple[ExpectedQuestion, ...]


@dataclass(frozen=True, slots=True)
class CaseResult:
    strategy: str
    case_name: str
    ok: bool
    latency_ms: int
    expected: int
    retained: int
    category_correct: int
    group_covered: int
    routed_document_hit: int
    routed_document_expected: int
    focused_content_hit: int
    focused_content_expected: int
    returned_chunks: int
    llm_calls: int
    warning_codes: tuple[str, ...]
    classifications: tuple[str, ...]
    robust_phase_count: int
    error: str | None = None


@dataclass(slots=True)
class ManagedRetrievalService:
    strategy: str
    base_url: str
    process: Any | None = None
    log_handle: Any | None = None
    log_path: Path | None = None

    @property
    def owned(self) -> bool:
        return self.process is not None

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log_handle is not None:
            self.log_handle.close()


def _service_ready(base_url: str, timeout: float = 2.0) -> bool:
    request = urllib.request.Request(base_url.rstrip("/") + "/readyz", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
            return response.status == 200 and payload.get("ok") is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def ensure_retrieval_service(
    *,
    strategy: str,
    base_url: str,
    startup_timeout: float,
    ready_probe: Any = _service_ready,
    process_factory: Any = subprocess.Popen,
    sleeper: Any = time.sleep,
) -> ManagedRetrievalService:
    if ready_probe(base_url, 2.0):
        print(f"[{strategy}] using ready service at {base_url}", flush=True)
        return ManagedRetrievalService(strategy=strategy, base_url=base_url)

    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"{strategy} service is not ready at {base_url}; automatic startup only "
            "auto-starts loopback HTTP URLs"
        )
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise RuntimeError(f"{strategy} base URL must not contain a path, query, or fragment")
    port = parsed.port
    if port is None:
        raise RuntimeError(f"{strategy} base URL must include an explicit port")

    log_path = Path(tempfile.gettempdir()) / f"rag-retrieval-acceptance-{strategy}.log"
    log_handle = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "APP_PORT": str(port),
            "RAG_QUERY_CLASSIFICATION_STRATEGY": strategy,
            "RAG_DEBUG_ENABLED": "true",
        }
    )
    command = [
        str(PROJECT_ROOT / ".venv/bin/uvicorn"),
        "app.api.app:app",
        "--host",
        parsed.hostname,
        "--port",
        str(port),
        "--workers",
        "1",
    ]
    process = process_factory(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    managed = ManagedRetrievalService(
        strategy=strategy,
        base_url=base_url,
        process=process,
        log_handle=log_handle,
        log_path=log_path,
    )
    deadline = monotonic() + max(1.0, startup_timeout)
    while monotonic() < deadline:
        if process.poll() is not None:
            break
        if ready_probe(base_url, 2.0):
            print(
                f"[{strategy}] started managed service at {base_url}; log={log_path}",
                flush=True,
            )
            return managed
        sleeper(0.2)

    managed.close()
    detail = ""
    try:
        detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    except OSError:
        pass
    raise RuntimeError(
        f"{strategy} service did not become ready at {base_url} within "
        f"{startup_timeout:g}s; log={log_path}\n{detail}"
    )


CASES: tuple[CompoundCase, ...] = (
    CompoundCase(
        "mixed_five_way_colloquial",
        "你能看到什么？黄山旅游2022年年度报告中，董事会、监事会及董事、监事、高级管理人员"
        "对年度报告内容有何声明？制造、修理计量器具的企业事业单位需要具备哪些条件？"
        "黄山旅游2022年年度报告有几章？再详细总结一下黄山旅游2022年年度报告。",
        (
            ExpectedQuestion(("看到",), "scope_direct"),
            ExpectedQuestion(("黄山旅游", "声明"), "routed_focused", HUANGSHAN, ("真实性",)),
            ExpectedQuestion(("计量器具", "条件"), "routed_focused", METROLOGY, ("设施",)),
            ExpectedQuestion(("黄山旅游", "几章"), "routed_direct", HUANGSHAN),
            ExpectedQuestion(("黄山旅游", "总结"), "routed_broad", HUANGSHAN),
        ),
    ),
    CompoundCase(
        "scope_direct_focused_broad",
        "你这边一共能查到几份材料？《中华人民共和国能源法》有多少章？"
        "《中华人民共和国能源法》规定的能源规划分哪几种？"
        "顺便全面讲讲《中华人民共和国计量法》都管了些什么。",
        (
            ExpectedQuestion(("几份", "材料"), "scope_direct"),
            ExpectedQuestion(("能源法", "多少章"), "routed_direct", ENERGY),
            ExpectedQuestion(("能源规划", "几种"), "routed_focused", ENERGY, ("全国综合能源规划",)),
            ExpectedQuestion(("计量法", "讲讲"), "routed_broad", METROLOGY),
        ),
    ),
    CompoundCase(
        "scone_colloquial_mix",
        "先说说目前到底有啥文档可用。一份英式司康够几个人吃？"
        "英式司康制作文档的章节结构是什么？再把英式司康的做法从头到尾详细解释一下。",
        (
            ExpectedQuestion(("文档", "可用"), "scope_direct"),
            ExpectedQuestion(("司康", "几个人"), "routed_focused", SCONE, ("4-6",)),
            ExpectedQuestion(("司康", "章节结构"), "routed_direct", SCONE),
            ExpectedQuestion(("司康", "详细"), "routed_broad", SCONE),
        ),
    ),
    CompoundCase(
        "health_article_mix",
        "这批材料大致都聊些什么？全国血防宣传周那篇文章里，人们常说的吸血虫通常指什么？"
        "全国血防宣传周文章一共有多少页？",
        (
            ExpectedQuestion(("材料", "聊"), "scope_direct"),
            ExpectedQuestion(("吸血虫", "指"), "routed_focused", SCHISTOSOMIASIS, ("水蛭",)),
            ExpectedQuestion(("血防宣传周", "多少页"), "routed_direct", SCHISTOSOMIASIS),
        ),
    ),
    CompoundCase(
        "two_laws_broad_and_direct",
        "请深入解释《中华人民共和国能源法》的整体内容，也完整梳理《中华人民共和国计量法》"
        "的主要内容；另外把《中华人民共和国能源法》的章节结构列出来。",
        (
            ExpectedQuestion(("能源法", "解释"), "routed_broad", ENERGY),
            ExpectedQuestion(("计量法", "内容"), "routed_broad", METROLOGY),
            ExpectedQuestion(("能源法", "章节结构"), "routed_direct", ENERGY),
        ),
    ),
    CompoundCase(
        "focused_only_from_dataset",
        "黄山旅游2022年年度报告中提到的旅游行业存在哪些风险因素？"
        "《中华人民共和国能源法》中规定的可再生能源电力消纳责任由哪些主体承担？"
        "制作英式司康时，蛋奶混合液的调配方法是什么？",
        (
            ExpectedQuestion(("旅游行业", "风险"), "routed_focused", HUANGSHAN, ("环境",)),
            ExpectedQuestion(("消纳责任", "主体"), "routed_focused", ENERGY, ("供电企业",)),
            ExpectedQuestion(("司康", "蛋奶"), "routed_focused", SCONE, ("淡奶油",)),
        ),
    ),
    CompoundCase(
        "direct_only_resources",
        "黄山旅游2022年年度报告有多少页？《中华人民共和国计量法》的目录是什么？"
        "英式司康制作文档的第一章讲了什么？",
        (
            ExpectedQuestion(("黄山旅游", "多少页"), "routed_direct", HUANGSHAN),
            ExpectedQuestion(("计量法", "目录"), "routed_direct", METROLOGY),
            ExpectedQuestion(("司康", "第一章"), "routed_direct", SCONE),
        ),
    ),
    CompoundCase(
        "broad_only_ambiguous_wording",
        "黄山旅游2022年年度报告整体看下来到底在说啥？《中华人民共和国能源法》这份材料"
        "究竟是干什么的？全国血防宣传周文章从头到尾详细讲了哪些内容？",
        (
            ExpectedQuestion(("黄山旅游", "说啥"), "routed_broad", HUANGSHAN),
            ExpectedQuestion(("能源法", "干什么"), "routed_broad", ENERGY),
            ExpectedQuestion(("血防宣传周", "详细"), "routed_broad", SCHISTOSOMIASIS),
        ),
    ),
    CompoundCase(
        "scope_wording_variants",
        "你手头现在都掌握了哪些材料？你总共能翻到多少份文件？这些文件大概分别在说什么？",
        (
            ExpectedQuestion(("哪些", "材料"), "scope_direct"),
            ExpectedQuestion(("多少份", "文件"), "scope_direct"),
            ExpectedQuestion(("文件", "说什么"), "scope_direct"),
        ),
    ),
    CompoundCase(
        "same_document_three_workflows",
        "《中华人民共和国能源法》中能源储备遵循什么原则？《中华人民共和国能源法》有几章？"
        "请详细解释《中华人民共和国能源法》的完整制度框架。",
        (
            ExpectedQuestion(("能源储备", "原则"), "routed_focused", ENERGY, ("政府主导",)),
            ExpectedQuestion(("能源法", "几章"), "routed_direct", ENERGY),
            ExpectedQuestion(("能源法", "制度框架"), "routed_broad", ENERGY),
        ),
    ),
    CompoundCase(
        "two_documents_four_workflows",
        "当前有哪些资料能用？黄山旅游2022年年度报告中的固定资产确认条件有哪些？"
        "详细概括黄山旅游2022年年度报告。《中华人民共和国计量法》的章节结构是什么？"
        "制造、修理计量器具的企业事业单位需要具备哪些条件？",
        (
            ExpectedQuestion(("哪些", "资料"), "scope_direct"),
            ExpectedQuestion(("固定资产", "条件"), "routed_focused", HUANGSHAN, ("可靠",)),
            ExpectedQuestion(("黄山旅游", "概括"), "routed_broad", HUANGSHAN),
            ExpectedQuestion(("计量法", "章节结构"), "routed_direct", METROLOGY),
            ExpectedQuestion(("计量器具", "条件"), "routed_focused", METROLOGY, ("检定",)),
        ),
    ),
    CompoundCase(
        "messy_but_self_contained",
        "我也不太确定该怎么问：你这里到底能查些什么资料？黄山旅游2022年年度报告的长期应付款"
        "期末余额是多少？英式司康制作文档到底讲了个啥？《中华人民共和国能源法》的目录给我看下。",
        (
            ExpectedQuestion(("查", "资料"), "scope_direct"),
            ExpectedQuestion(
                ("长期应付款", "期末余额"), "routed_focused", HUANGSHAN, ("41,953,066.18",)
            ),
            ExpectedQuestion(("司康", "讲"), "routed_broad", SCONE),
            ExpectedQuestion(("能源法", "目录"), "routed_direct", ENERGY),
        ),
    ),
)


def _compact(value: str) -> str:
    return "".join(value.casefold().split()).replace("《", "").replace("》", "")


def _match_expected(expected: ExpectedQuestion, groups: list[dict[str, Any]], used: set[str]):
    for group in groups:
        for question in group.get("queries", []):
            key = f"{group.get('group_ref')}\0{question}"
            if key in used:
                continue
            text = _compact(str(question))
            if all(_compact(anchor) in text for anchor in expected.anchors):
                used.add(key)
                return group
    return None


def evaluate_response(strategy: str, case: CompoundCase, body: dict[str, Any], latency_ms: int):
    debug = body.get("debug") or {}
    groups = debug.get("groups") or []
    chunks = body.get("chunks") or []
    covered = set((body.get("coverage") or {}).get("covered_group_refs") or [])
    used: set[str] = set()
    retained = category_correct = group_covered = 0
    routed_document_hit = focused_content_hit = 0
    routed_document_expected = focused_content_expected = 0
    classifications: list[str] = []
    for expected in case.expected:
        group = _match_expected(expected, groups, used)
        if group is None:
            classifications.append(f"missing:{expected.category}")
            continue
        retained += 1
        actual_category = str(group.get("category"))
        classifications.append(actual_category)
        category_correct += actual_category == expected.category
        group_covered += group.get("group_ref") in covered
        if expected.document_id:
            routed_document_expected += 1
            matching_chunks = [
                chunk
                for chunk in chunks
                if expected.document_id == chunk.get("document_id")
                or expected.document_id in (chunk.get("document_ids") or [])
            ]
            routed_document_hit += bool(matching_chunks)
            if expected.content_anchors:
                focused_content_expected += 1
                combined = _compact(
                    "\n".join(str(chunk.get("content", "")) for chunk in matching_chunks)
                )
                focused_content_hit += any(
                    _compact(anchor) in combined for anchor in expected.content_anchors
                )
    usage = body.get("usage") or {}
    warning_codes = tuple(str(item.get("code")) for item in body.get("warnings") or [])
    return CaseResult(
        strategy=strategy,
        case_name=case.name,
        ok=True,
        latency_ms=latency_ms,
        expected=len(case.expected),
        retained=retained,
        category_correct=category_correct,
        group_covered=group_covered,
        routed_document_hit=routed_document_hit,
        routed_document_expected=routed_document_expected,
        focused_content_hit=focused_content_hit,
        focused_content_expected=focused_content_expected,
        returned_chunks=int(usage.get("returned_count", len(chunks))),
        llm_calls=int(usage.get("llm_request_count", 0)),
        warning_codes=warning_codes,
        classifications=tuple(classifications),
        robust_phase_count=len(debug.get("classification_trace") or []),
    )


def _post(url: str, payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], int]:
    request = urllib.request.Request(
        url.rstrip("/") + "/rag/v1/retrieve",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read()), int((monotonic() - started) * 1000)


def run_case(strategy: str, base_url: str, case: CompoundCase, timeout: float) -> CaseResult:
    payload = {
        "user_id": "robust-compound-acceptance",
        "kb_id": "eval-rag-kb",
        "query": case.query,
        "doc_ids": list(DOCUMENT_SCOPE),
        "temp_doc_ids": [],
        "top_k": 12,
        "max_return_tokens": 16384,
        "search_mode": "semantic",
        "options": {"include_document_meta": True, "include_debug": True},
    }
    started = monotonic()
    try:
        body, latency_ms = _post(base_url, payload, timeout)
        return evaluate_response(strategy, case, body, latency_ms)
    except Exception as exc:
        detail = str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            detail = f"HTTP {exc.code}: {exc.read(500).decode('utf-8', errors='replace')}"
        return CaseResult(
            strategy=strategy,
            case_name=case.name,
            ok=False,
            latency_ms=int((monotonic() - started) * 1000),
            expected=len(case.expected),
            retained=0,
            category_correct=0,
            group_covered=0,
            routed_document_hit=0,
            routed_document_expected=sum(item.document_id is not None for item in case.expected),
            focused_content_hit=0,
            focused_content_expected=sum(bool(item.content_anchors) for item in case.expected),
            returned_chunks=0,
            llm_calls=0,
            warning_codes=(),
            classifications=(),
            robust_phase_count=0,
            error=f"{type(exc).__name__}: {detail[:500]}",
        )


def _ratio(numerator: int, denominator: int) -> str:
    return "N/A" if not denominator else f"{100 * numerator / denominator:.2f}%"


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    values = sorted(values)
    return values[max(0, (95 * len(values) + 99) // 100 - 1)]


def render_report(results: list[CaseResult], *, model: str) -> str:
    lines = [
        "# Fast 与 Robust 复合 Query 全链路验收报告",
        "",
        f"- 生成时间：`{datetime.now(UTC).isoformat(timespec='seconds')}`",
        f"- 模型：`{model or '由服务端配置决定'}`",
        f"- 复合问题：`{len(CASES)}` 个；fast/robust 使用相同字符串 query 与相同 5 篇文档范围。",
        "- 调用入口：真实 `POST /rag/v1/retrieve`，不是分类类的直接调用。",
        "- focused 问题来自离线标注或按其原文改写；scope/direct/broad 使用口语化、模糊表达构造。",
        "- 用例不包含依赖历史但未消解的指代。每次请求启用 debug，仅用于核验分类与路由。",
        "",
        "## 总体结果",
        "",
        "| 策略 | HTTP 成功 | 子问题保留率 | 分类准确率 | Group 有结果率 | "
        "目标文档命中率 | Focused 原文锚点命中率 | P50/P95 耗时 | "
        "平均 LLM calls | Robust trace |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy in ("fast", "robust"):
        rows = [item for item in results if item.strategy == strategy]
        expected = sum(item.expected for item in rows)
        latencies = [item.latency_ms for item in rows]
        routed_hits = sum(item.routed_document_hit for item in rows)
        routed_expected = sum(item.routed_document_expected for item in rows)
        focused_hits = sum(item.focused_content_hit for item in rows)
        focused_expected = sum(item.focused_content_expected for item in rows)
        lines.append(
            f"| {strategy} | {sum(item.ok for item in rows)}/{len(rows)} | "
            f"{_ratio(sum(item.retained for item in rows), expected)} | "
            f"{_ratio(sum(item.category_correct for item in rows), expected)} | "
            f"{_ratio(sum(item.group_covered for item in rows), expected)} | "
            f"{_ratio(routed_hits, routed_expected)} | "
            f"{_ratio(focused_hits, focused_expected)} | "
            f"{int(statistics.median(latencies))}/{_p95(latencies)} ms | "
            f"{statistics.fmean(item.llm_calls for item in rows):.2f} | "
            f"{sum(item.robust_phase_count > 0 for item in rows)}/{len(rows)} |"
        )
    lines.extend(
        [
            "",
            "## 分案例结果",
            "",
            "| 策略 | 案例 | 成功 | 保留/分类/覆盖 | 文档命中 | "
            "Focused 原文 | chunks | calls | 耗时 | warnings |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for result in results:
        lines.append(
            f"| {result.strategy} | `{result.case_name}` | {'是' if result.ok else '否'} | "
            f"{result.retained}/{result.category_correct}/{result.group_covered} "
            f"of {result.expected} | "
            f"{result.routed_document_hit}/{result.routed_document_expected} | "
            f"{result.focused_content_hit}/{result.focused_content_expected} | "
            f"{result.returned_chunks} | {result.llm_calls} | {result.latency_ms} ms | "
            f"{', '.join(result.warning_codes) or '-'} |"
        )
    failures = [
        item
        for item in results
        if not item.ok or item.retained < item.expected or item.category_correct < item.expected
    ]
    lines.extend(["", "## 失败与误分类详情", ""])
    if not failures:
        lines.append("未发现 HTTP 失败、子问题丢失或分类错误。")
    else:
        for item in failures:
            lines.append(
                f"- `{item.strategy}/{item.case_name}`："
                f"classifications={list(item.classifications)}；"
                f"保留={item.retained}/{item.expected}；分类={item.category_correct}/{item.expected}；"
                f"error={item.error or '-'}"
            )
    lines.extend(
        [
            "",
            "## 用例说明",
            "",
            "离线评测集主要提供 focused 原文事实及 doc_id 标注，无法单独覆盖四类分类边界。"
            "本报告保留这些可验证的 focused 事实，同时补入人工构造的口语化 scope、"
            "direct 和 broad 子问题，"
            "并随机式混合为复合请求。该报告衡量的是真实检索接口的工程表现，不代替大规模离线召回评测。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast-url", required=True)
    parser.add_argument("--robust-url", required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--startup-timeout", type=float, default=60)
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("docs/robust-compound-retrieval-acceptance.md"),
    )
    args = parser.parse_args()
    results: list[CaseResult] = []
    for strategy, base_url in (("fast", args.fast_url), ("robust", args.robust_url)):
        managed = ensure_retrieval_service(
            strategy=strategy,
            base_url=base_url,
            startup_timeout=args.startup_timeout,
        )
        try:
            for index, case in enumerate(CASES, 1):
                print(f"[{strategy}] {index}/{len(CASES)} {case.name}", flush=True)
                result = run_case(strategy, base_url, case, args.timeout)
                results.append(result)
                print(
                    f"  ok={result.ok} retained={result.retained}/{result.expected} "
                    f"category={result.category_correct}/{result.expected} "
                    f"latency={result.latency_ms}ms"
                    f"{f' error={result.error}' if result.error else ''}",
                    flush=True,
                )
        finally:
            managed.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(results, model=args.model), encoding="utf-8")
    print(f"report written to {args.report}")
    return 0 if all(item.ok for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
