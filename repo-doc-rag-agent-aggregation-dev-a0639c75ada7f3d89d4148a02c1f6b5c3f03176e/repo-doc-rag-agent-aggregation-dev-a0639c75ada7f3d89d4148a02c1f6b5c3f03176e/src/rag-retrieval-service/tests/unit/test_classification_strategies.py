from __future__ import annotations

import pytest

from app.llm.gateway import LLMCallError, LLMRequest, LLMResult, LLMUsage
from app.workflows.classification.strategies import (
    CLASSIFICATION_SYSTEM_PROMPT,
    ClassificationService,
    FastLLMClassificationStrategy,
    LLMClassificationStrategy,
    RuleClassificationStrategy,
)


class FakeGateway:
    def __init__(self, data: dict | None = None, error: Exception | None = None) -> None:
        self.data = data
        self.error = error
        self.requests: list[LLMRequest] = []

    async def complete_json(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if self.error:
            raise self.error
        return LLMResult(data=self.data or {}, usage=LLMUsage(total_tokens=12))


def test_classifier_prompt_has_the_required_independent_query_boundaries() -> None:
    compact_prompt = "".join(CLASSIFICATION_SYSTEM_PROMPT.split())
    assert "# 角色与任务" in CLASSIFICATION_SYSTEM_PROMPT
    assert "# 分类标准" in CLASSIFICATION_SYSTEM_PROMPT
    assert "# 示例" in CLASSIFICATION_SYSTEM_PROMPT
    assert "# 输出约束" in CLASSIFICATION_SYSTEM_PROMPT
    assert "必须是从原 query 拆分并改写后的独立问题" in CLASSIFICATION_SYSTEM_PROMPT
    assert "不得新增用户未提出的问题" in CLASSIFICATION_SYSTEM_PROMPT
    assert "不得回答问题" in CLASSIFICATION_SYSTEM_PROMPT
    assert "详细总结" in CLASSIFICATION_SYSTEM_PROMPT
    assert "routed_broad" in CLASSIFICATION_SYSTEM_PROMPT
    assert "少量原文片段" in CLASSIFICATION_SYSTEM_PROMPT
    assert "集合级" in CLASSIFICATION_SYSTEM_PROMPT
    assert "不同下游类别的问题绝对不得放进同一 group" in CLASSIFICATION_SYSTEM_PROMPT
    assert "数量约束" in CLASSIFICATION_SYSTEM_PROMPT
    assert "少量明确目标文档" in CLASSIFICATION_SYSTEM_PROMPT
    assert "数据库已有文档摘要对整个集合做简要概览" in CLASSIFICATION_SYSTEM_PROMPT
    assert "这些文档中哪些公司的营业收入超过一亿元" in compact_prompt


@pytest.mark.asyncio
async def test_llm_classifier_makes_one_schema_constrained_call() -> None:
    gateway = FakeGateway(
        {
            "routed_focused": [
                {
                    "queries": ["A公司去年的营业额是多少"],
                    "target_docs_description": "A公司去年的年度报告",
                    "target_docs_keywords": ["A公司", "年报"],
                }
            ],
            "routed_broad": [],
            "routed_direct": [],
            "scope_direct": [],
        }
    )
    strategy = LLMClassificationStrategy(gateway=gateway, model="weak", max_tokens=800)

    result = await strategy.classify(
        request_id="request-1",
        query="A公司去年的营业额是多少",
        scope_document_count=7,
    )

    assert len(gateway.requests) == 1
    assert gateway.requests[0].response_format["type"] == "json_schema"
    assert "group_ref" not in str(gateway.requests[0].response_format)
    assert "当前用户请求范围包含 7 篇文档" in gateway.requests[0].messages[-1]["content"]
    assert result.routed_focused[0].queries == ["A公司去年的营业额是多少"]


@pytest.mark.asyncio
async def test_fast_classifier_joins_preprocessed_queries_for_one_call() -> None:
    gateway = FakeGateway(
        {
            "routed_focused": [],
            "routed_broad": [],
            "routed_direct": [],
            "scope_direct": [{"queries": ["你能看到哪些文档", "这些文档主要讲什么"]}],
        }
    )
    strategy = FastLLMClassificationStrategy(gateway=gateway, model="weak", max_tokens=800)

    await strategy.classify(
        request_id="request-1",
        query=["你能看到哪些文档", "这些文档主要讲什么"],
        scope_document_count=7,
    )

    assert len(gateway.requests) == 1
    assert (
        "用户原始问题：你能看到哪些文档；这些文档主要讲什么"
        in gateway.requests[0].messages[-1]["content"]
    )


def test_rule_classifier_preserves_each_composite_query_part() -> None:
    output = RuleClassificationStrategy().classify_sync(
        query="你能看到哪些文档？查看A公司年报第12页；详细总结B公司年报"
    )

    assert output.scope_direct[0].queries == ["你能看到哪些文档"]
    assert output.routed_direct[0].queries == ["查看A公司年报第12页"]
    assert output.routed_broad[0].queries == ["详细总结B公司年报"]


@pytest.mark.parametrize(
    ("query", "expected_category"),
    [
        ("简要总结这几个文档内容", "scope_direct"),
        ("这些文档主要讲了什么", "scope_direct"),
        ("你能看到哪些文档", "scope_direct"),
        ("这个文档有几页", "routed_direct"),
        ("查看某文档的章节结构", "routed_direct"),
        ("这篇文档的第三章在讲什么", "routed_direct"),
        ("帮我看看第12页有什么", "routed_direct"),
        ("这篇文档的目录是什么", "routed_direct"),
        ("公司去年的营业额是多少", "routed_focused"),
        ("XXX的流程是什么", "routed_focused"),
        ("X的具体数值、日期或比例是多少", "routed_focused"),
        ("第一季度营业额是多少", "routed_focused"),
        ("A公司与B公司去年营业额谁多", "routed_focused"),
        ("帮我对比文档一和文档二的差别", "routed_broad"),
        ("详细总结文档一的内容", "routed_broad"),
        ("详细总结上述所有文档", "routed_broad"),
        ("帮我解释这篇论文", "routed_broad"),
    ],
)
def test_rule_classifier_covers_supported_query_categories(
    query: str, expected_category: str
) -> None:
    output = RuleClassificationStrategy().classify_sync(query=query)

    categories = [
        category
        for category in ("routed_focused", "routed_broad", "routed_direct", "scope_direct")
        if getattr(output, category)
    ]
    assert categories == [expected_category]


@pytest.mark.asyncio
async def test_classification_service_falls_back_and_marks_groups_degraded() -> None:
    gateway = FakeGateway(error=LLMCallError("down", error_category="connect"))
    service = ClassificationService(
        primary=LLMClassificationStrategy(gateway=gateway, model="weak", max_tokens=800),
        fallback=RuleClassificationStrategy(),
    )

    result = await service.classify(request_id="request-1", query="公司去年的营业额是多少")

    assert result.degraded is True
    assert result.groups[0].degraded is True
    assert result.groups[0].category == "routed_focused"
    assert result.warnings[0].code == "CLASSIFICATION_RULE_FALLBACK"


@pytest.mark.asyncio
async def test_classification_service_rejects_unrelated_or_duplicate_llm_questions() -> None:
    gateway = FakeGateway(
        {
            "routed_focused": [
                {
                    "queries": ["完全无关的新增问题"],
                    "target_docs_description": "文档",
                    "target_docs_keywords": ["文档"],
                }
            ],
            "routed_broad": [],
            "routed_direct": [],
            "scope_direct": [],
        }
    )
    service = ClassificationService(
        primary=LLMClassificationStrategy(gateway=gateway, model="weak", max_tokens=800),
        fallback=RuleClassificationStrategy(),
    )

    result = await service.classify(request_id="request-1", query="公司去年的营业额是多少")

    assert result.degraded is True
    assert result.warnings[0].code == "CLASSIFICATION_RULE_FALLBACK"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "wrong_category", "expected_category"),
    [
        ("这些文档主要讲了什么", "routed_broad", "scope_direct"),
        ("详细总结某文档", "routed_direct", "routed_broad"),
        (
            "中国铁物2023年半年度报告文件有多少页?",
            "routed_focused",
            "routed_direct",
        ),
    ],
)
async def test_classification_service_corrects_unambiguous_llm_category_errors(
    query: str, wrong_category: str, expected_category: str
) -> None:
    routed = {
        "queries": [query],
        "target_docs_description": "某文档",
        "target_docs_keywords": ["某文档"],
    }
    data = {
        "routed_focused": [],
        "routed_broad": [],
        "routed_direct": [],
        "scope_direct": [],
    }
    data[wrong_category] = [routed]
    service = ClassificationService(
        primary=LLMClassificationStrategy(gateway=FakeGateway(data), model="weak", max_tokens=800),
        fallback=RuleClassificationStrategy(),
    )

    result = await service.classify(request_id="request-1", query=query)

    assert [group.category for group in result.groups] == [expected_category]
    assert result.groups[0].queries == [query]
