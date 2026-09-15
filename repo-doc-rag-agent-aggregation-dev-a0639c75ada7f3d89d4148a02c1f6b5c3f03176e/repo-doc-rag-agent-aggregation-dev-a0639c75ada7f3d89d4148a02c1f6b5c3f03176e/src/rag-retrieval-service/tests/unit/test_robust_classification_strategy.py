from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.llm.gateway import LLMCallError, LLMRequest, LLMResult, LLMUsage
from app.workflows.classification.prompts import (
    DIRECT_CLASSIFIER_PROMPT,
    FOCUSED_CLASSIFIER_PROMPT,
    QUERY_REWRITE_PROMPT,
    SCOPE_CLASSIFIER_PROMPT,
    TARGET_DOCUMENT_GROUPING_PROMPT,
)
from app.workflows.classification.robust_strategy import (
    RobustLLMClassificationStrategy,
    _category,
)


class ScriptedGateway:
    def __init__(
        self,
        responses: dict[str, dict[str, Any] | Exception],
        *,
        synchronize_non_scope: bool = False,
    ) -> None:
        self.responses = responses
        self.requests: list[LLMRequest] = []
        self.synchronize_non_scope = synchronize_non_scope
        self.non_scope_started: set[str] = set()
        self._all_non_scope_started = asyncio.Event()

    async def complete_json(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if self.synchronize_non_scope and request.phase in {
            "robust_direct_classifier",
            "robust_focused_classifier",
            "robust_target_document_grouping",
        }:
            self.non_scope_started.add(request.phase)
            if len(self.non_scope_started) == 3:
                self._all_non_scope_started.set()
            await self._all_non_scope_started.wait()
        response = self.responses[request.phase]
        if isinstance(response, Exception):
            raise response
        return LLMResult(data=response, usage=LLMUsage(total_tokens=10))


def _strategy(gateway: ScriptedGateway) -> RobustLLMClassificationStrategy:
    return RobustLLMClassificationStrategy(gateway=gateway, model="doubao", max_tokens=1000)


@pytest.mark.parametrize(
    ("is_scope", "is_direct", "is_focused", "expected"),
    [
        (False, False, False, "routed_broad"),
        (False, False, True, "routed_focused"),
        (False, True, False, "routed_direct"),
        (False, True, True, "routed_direct"),
        (True, False, False, "scope_direct"),
        (True, False, True, "scope_direct"),
        (True, True, False, "scope_direct"),
        (True, True, True, "scope_direct"),
    ],
)
def test_fixed_decision_tree_covers_all_boolean_combinations(
    is_scope: bool,
    is_direct: bool,
    is_focused: bool,
    expected: str,
) -> None:
    assert (
        _category(
            is_scope=is_scope,
            is_direct=is_direct,
            is_focused=is_focused,
        )
        == expected
    )


def test_robust_prompts_use_explicit_sections_and_boundaries() -> None:
    for prompt in (
        QUERY_REWRITE_PROMPT,
        SCOPE_CLASSIFIER_PROMPT,
        DIRECT_CLASSIFIER_PROMPT,
        FOCUSED_CLASSIFIER_PROMPT,
        TARGET_DOCUMENT_GROUPING_PROMPT,
    ):
        assert "# 角色与任务" in prompt
        assert "# 禁止事项" in prompt
        assert "# 输出约束" in prompt
    for prompt in (
        SCOPE_CLASSIFIER_PROMPT,
        DIRECT_CLASSIFIER_PROMPT,
        FOCUSED_CLASSIFIER_PROMPT,
    ):
        assert "# 唯一分类标准" in prompt
        assert "# 易混淆情况" in prompt
        assert "例如" in prompt
    assert "每项必须是从原 query 拆分并改写出的独立问题" in QUERY_REWRITE_PROMPT
    assert "跨文档" in QUERY_REWRITE_PROMPT
    assert "不能丢弃" in QUERY_REWRITE_PROMPT
    assert "当前请求范围" in SCOPE_CLASSIFIER_PROMPT
    assert "少量具体文档" in SCOPE_CLASSIFIER_PROMPT
    assert "无需语义搜索" in DIRECT_CLASSIFIER_PROMPT
    assert "少量、位置未知" in FOCUSED_CLASSIFIER_PROMPT
    assert "忽略问题的检索类别" in TARGET_DOCUMENT_GROUPING_PROMPT
    assert "不得输出 group_id" in TARGET_DOCUMENT_GROUPING_PROMPT


@pytest.mark.asyncio
async def test_scope_first_short_circuits_all_non_scope_calls_for_query_list() -> None:
    gateway = ScriptedGateway(
        {"robust_scope_classifier": {"decisions": {"q_001": True, "q_002": True}}}
    )

    result = await _strategy(gateway).classify(
        request_id="r1",
        query=["你能看到哪些文档？", "这些文档主要讲了什么？"],
        scope_document_count=20,
        debug_enabled=True,
    )

    assert [request.phase for request in gateway.requests] == ["robust_scope_classifier"]
    assert [group.category for group in result.groups] == ["scope_direct", "scope_direct"]
    assert [group.queries for group in result.groups] == [
        ["你能看到哪些文档？"],
        ["这些文档主要讲了什么？"],
    ]
    assert all(group.target_docs_description is None for group in result.groups)
    assert [item["phase"] for item in result.classification_trace] == [
        "normalized_queries",
        "scope_decision",
        "final_groups",
    ]
    assert result.classification_trace[0]["input_mode"] == "list"
    assert result.classification_trace[1]["scope_query_refs"] == ["q_001", "q_002"]


@pytest.mark.asyncio
async def test_scope_guard_overrides_model_for_counted_collection_summary() -> None:
    gateway = ScriptedGateway({"robust_scope_classifier": {"decisions": {"q_001": False}}})

    result = await _strategy(gateway).classify(
        request_id="r1",
        query=["总结当前的4篇文档"],
        scope_document_count=4,
        debug_enabled=True,
    )

    assert [request.phase for request in gateway.requests] == ["robust_scope_classifier"]
    assert result.groups[0].category == "scope_direct"
    assert result.classification_trace[1]["model_decisions"] == {"q_001": False}
    assert result.classification_trace[1]["decisions"] == {"q_001": True}
    assert result.classification_trace[1]["guard_overrides"] == {"q_001": "scope_direct"}


@pytest.mark.asyncio
async def test_non_scope_guards_never_demote_positive_scope_model_decision() -> None:
    gateway = ScriptedGateway(
        {"robust_scope_classifier": {"decisions": {"q_001": True, "q_002": True}}}
    )

    result = await _strategy(gateway).classify(
        request_id="r1",
        query=[
            "当前用户请求的4篇文档主要内容是什么？",
            "这些文档各有多少页？",
        ],
        scope_document_count=4,
        debug_enabled=True,
    )

    assert [request.phase for request in gateway.requests] == ["robust_scope_classifier"]
    assert [group.category for group in result.groups] == ["scope_direct", "scope_direct"]
    assert result.classification_trace[1]["decisions"] == {
        "q_001": True,
        "q_002": True,
    }


@pytest.mark.asyncio
async def test_scope_guard_contains_synthetic_ordinal_summary_rewrite() -> None:
    gateway = ScriptedGateway(
        {
            "robust_query_rewrite": {
                "queries": [
                    "总结当前的第1篇文档",
                    "总结当前的第2篇文档",
                    "总结当前的第3篇文档",
                    "总结当前的第4篇文档",
                ]
            },
            "robust_scope_classifier": {
                "decisions": {
                    "q_001": False,
                    "q_002": False,
                    "q_003": False,
                    "q_004": False,
                }
            },
        }
    )

    result = await _strategy(gateway).classify(
        request_id="r1",
        query="总结当前的4篇文档",
        scope_document_count=4,
        debug_enabled=True,
    )

    assert [request.phase for request in gateway.requests] == [
        "robust_query_rewrite",
        "robust_scope_classifier",
    ]
    assert [group.category for group in result.groups] == ["scope_direct"] * 4
    assert result.classification_trace[1]["guard_overrides"] == {
        "q_001": "scope_direct",
        "q_002": "scope_direct",
        "q_003": "scope_direct",
        "q_004": "scope_direct",
    }


@pytest.mark.asyncio
async def test_direct_and_broad_guards_override_conflicting_binary_results() -> None:
    gateway = ScriptedGateway(
        {
            "robust_scope_classifier": {"decisions": {"q_001": False, "q_002": False}},
            "robust_direct_classifier": {"decisions": {"q_001": False, "q_002": True}},
            "robust_focused_classifier": {"decisions": {"q_001": True, "q_002": True}},
            "robust_target_document_grouping": {
                "document_groups": [
                    {
                        "query_refs": ["q_001"],
                        "target_docs_description": "湖南海利2023年半年度报告",
                        "target_docs_keywords": ["湖南海利", "2023年半年度报告"],
                    },
                    {
                        "query_refs": ["q_002"],
                        "target_docs_description": "中华人民共和国能源法",
                        "target_docs_keywords": ["中华人民共和国能源法"],
                    },
                ]
            },
        }
    )

    result = await _strategy(gateway).classify(
        request_id="r1",
        query=[
            "湖南海利2023年半年度报告有多少章节，章节结构是什么？",
            "详细总结《中华人民共和国能源法》的整体内容",
        ],
        scope_document_count=4,
        debug_enabled=True,
    )

    assert {group.category for group in result.groups} == {
        "routed_direct",
        "routed_broad",
    }
    decisions = result.classification_trace[2]
    assert decisions["category_by_query_ref"] == {
        "q_001": "routed_direct",
        "q_002": "routed_broad",
    }
    assert decisions["guard_overrides"] == {
        "q_001": "routed_direct",
        "q_002": "routed_broad",
    }


@pytest.mark.asyncio
async def test_robust_classification_does_not_build_internal_trace_when_debug_is_off() -> None:
    gateway = ScriptedGateway({"robust_scope_classifier": {"decisions": {"q_001": True}}})

    result = await _strategy(gateway).classify(
        request_id="r1",
        query=["你能看到哪些文档？"],
        scope_document_count=20,
        debug_enabled=False,
    )

    assert result.classification_trace == ()


@pytest.mark.asyncio
async def test_string_rewrite_then_non_scope_classifiers_and_grouping_run_concurrently() -> None:
    gateway = ScriptedGateway(
        {
            "robust_query_rewrite": {
                "queries": [
                    "你能看到哪些文档？",
                    "A 公司营业额是多少？",
                    "A 公司年报第 12 页是什么？",
                ]
            },
            "robust_scope_classifier": {
                "decisions": {"q_001": True, "q_002": False, "q_003": False}
            },
            "robust_direct_classifier": {"decisions": {"q_002": False, "q_003": True}},
            "robust_focused_classifier": {"decisions": {"q_002": True, "q_003": False}},
            "robust_target_document_grouping": {
                "document_groups": [
                    {
                        "query_refs": ["q_002", "q_003"],
                        "target_docs_description": "A 公司年度报告",
                        "target_docs_keywords": ["A 公司", "年度报告", "年报"],
                    }
                ]
            },
        },
        synchronize_non_scope=True,
    )

    result = await asyncio.wait_for(
        _strategy(gateway).classify(
            request_id="r1",
            query="你能看到哪些文档？A 公司营业额是多少？A 公司年报第 12 页是什么？",
            scope_document_count=8,
            debug_enabled=True,
        ),
        timeout=1,
    )

    assert [request.phase for request in gateway.requests[:2]] == [
        "robust_query_rewrite",
        "robust_scope_classifier",
    ]
    assert gateway.non_scope_started == {
        "robust_direct_classifier",
        "robust_focused_classifier",
        "robust_target_document_grouping",
    }
    groups = {group.category: group for group in result.groups}
    assert groups["routed_focused"].queries == ["A 公司营业额是多少？"]
    assert groups["routed_direct"].queries == ["A 公司年报第 12 页是什么？"]
    assert groups["scope_direct"].queries == ["你能看到哪些文档？"]
    assert groups["routed_focused"].target_docs_description == "A 公司年度报告"
    assert groups["routed_direct"].target_docs_keywords == ["A 公司", "年度报告", "年报"]
    assert [item["phase"] for item in result.classification_trace] == [
        "normalized_queries",
        "scope_decision",
        "non_scope_decision",
        "document_clusters",
        "final_groups",
    ]
    assert result.classification_trace[2]["category_by_query_ref"] == {
        "q_002": "routed_focused",
        "q_003": "routed_direct",
    }


@pytest.mark.asyncio
async def test_non_scope_false_false_falls_back_to_broad() -> None:
    gateway = ScriptedGateway(
        {
            "robust_scope_classifier": {"decisions": {"q_001": False}},
            "robust_direct_classifier": {"decisions": {"q_001": False}},
            "robust_focused_classifier": {"decisions": {"q_001": False}},
            "robust_target_document_grouping": {
                "document_groups": [
                    {
                        "query_refs": ["q_001"],
                        "target_docs_description": "A 公司文档",
                        "target_docs_keywords": ["A 公司"],
                    }
                ]
            },
        }
    )

    result = await _strategy(gateway).classify(
        request_id="r1", query=["帮我看看 A 公司文档讲了什么"], scope_document_count=3
    )

    assert result.groups[0].category == "routed_broad"


@pytest.mark.asyncio
async def test_same_document_and_category_queries_share_one_final_group() -> None:
    gateway = ScriptedGateway(
        {
            "robust_scope_classifier": {"decisions": {"q_001": False, "q_002": False}},
            "robust_direct_classifier": {"decisions": {"q_001": False, "q_002": False}},
            "robust_focused_classifier": {"decisions": {"q_001": True, "q_002": True}},
            "robust_target_document_grouping": {
                "document_groups": [
                    {
                        "query_refs": ["q_001", "q_002"],
                        "target_docs_description": "中华人民共和国能源法",
                        "target_docs_keywords": ["中华人民共和国能源法", "能源法"],
                    }
                ]
            },
        }
    )

    result = await _strategy(gateway).classify(
        request_id="r1",
        query=[
            "中华人民共和国能源法规定的能源规划类型有哪些？",
            "中华人民共和国能源法规定了哪些能源处罚？",
        ],
        scope_document_count=3,
    )

    assert len(result.groups) == 1
    assert result.groups[0].category == "routed_focused"
    assert result.groups[0].queries == [
        "中华人民共和国能源法规定的能源规划类型有哪些？",
        "中华人民共和国能源法规定了哪些能源处罚？",
    ]


@pytest.mark.asyncio
async def test_direct_and_focused_failures_degrade_to_broad_with_warnings() -> None:
    failure = LLMCallError("failed", error_category="invalid_response")
    gateway = ScriptedGateway(
        {
            "robust_scope_classifier": {"decisions": {"q_001": False}},
            "robust_direct_classifier": failure,
            "robust_focused_classifier": failure,
            "robust_target_document_grouping": {
                "document_groups": [
                    {
                        "query_refs": ["q_001"],
                        "target_docs_description": "A 公司文档",
                        "target_docs_keywords": ["A 公司"],
                    }
                ]
            },
        }
    )

    result = await _strategy(gateway).classify(
        request_id="r1", query=["A 公司文档讲了什么"], scope_document_count=3
    )

    assert result.degraded is True
    assert result.groups[0].category == "routed_broad"
    assert {warning.code for warning in result.warnings} == {
        "DIRECT_CLASSIFIER_BROAD_FALLBACK",
        "FOCUSED_CLASSIFIER_BROAD_FALLBACK",
    }


@pytest.mark.asyncio
async def test_scope_failure_is_critical_and_does_not_switch_to_fast_or_rules() -> None:
    gateway = ScriptedGateway(
        {"robust_scope_classifier": LLMCallError("failed", error_category="invalid_response")}
    )

    with pytest.raises(LLMCallError):
        await _strategy(gateway).classify(
            request_id="r1", query=["你能看到哪些文档？"], scope_document_count=3
        )

    assert [request.phase for request in gateway.requests] == ["robust_scope_classifier"]


@pytest.mark.asyncio
async def test_invalid_document_partition_degrades_to_one_cluster_per_query() -> None:
    gateway = ScriptedGateway(
        {
            "robust_scope_classifier": {"decisions": {"q_001": False, "q_002": False}},
            "robust_direct_classifier": {"decisions": {"q_001": False, "q_002": False}},
            "robust_focused_classifier": {"decisions": {"q_001": True, "q_002": True}},
            "robust_target_document_grouping": {
                "document_groups": [
                    {
                        "query_refs": ["q_001"],
                        "target_docs_description": "A 公司",
                        "target_docs_keywords": ["A 公司"],
                    }
                ]
            },
        }
    )

    result = await _strategy(gateway).classify(
        request_id="r1",
        query=["A 公司营业额是多少", "B 公司营业额是多少"],
        scope_document_count=3,
    )

    assert len(result.groups) == 2
    assert [group.queries for group in result.groups] == [
        ["A 公司营业额是多少"],
        ["B 公司营业额是多少"],
    ]
    assert result.degraded is True
    assert result.warnings[0].code == "TARGET_DOCUMENT_GROUPING_RULE_FALLBACK"
