from __future__ import annotations

import pytest

from app.db.repositories import DocumentProfile
from app.llm.gateway import LLMRequest, LLMResult, LLMToolCall, LLMUsage
from app.workflows.classification.models import QueryGroup
from app.workflows.direct_access.planner import LLMDirectPlanner, RuleDirectPlanner
from app.workflows.direct_access.service import DirectAccessService
from app.workflows.scope_access.service import ScopeAccessService


class PlannerGateway:
    def __init__(self, tool_calls: tuple[LLMToolCall, ...] = (), fail: bool = False) -> None:
        self.tool_calls = tool_calls
        self.fail = fail
        self.requests: list[LLMRequest] = []

    async def complete_json(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("planner failed")
        return LLMResult(data={}, usage=LLMUsage(total_tokens=4), tool_calls=self.tool_calls)


class RecordingExecutor:
    def get_content_of_pages(self, **kwargs: object):
        from app.core.models import CandidateChunk, ToolOutput

        return ToolOutput(
            chunks=[
                CandidateChunk(
                    chunk_id="d1:page:2",
                    document_id="d1",
                    document_ids=["d1"],
                    document_name="年报",
                    page_number=2,
                    path="document:d1:page:2",
                    content="原文",
                    source_type="page",
                    category="routed_direct",
                    group_matches={"g1": "accept"},
                    questions_by_group={"g1": ["第2页"]},
                )
            ]
        )


def _profile(index: int = 1) -> DocumentProfile:
    return DocumentProfile(
        doc_id=f"d{index}", doc_name=f"年报{index}", doc_description=f"摘要{index}", page_count=5
    )


def _group(category: str, query: str) -> QueryGroup:
    return QueryGroup(
        group_ref="g1",
        category=category,
        queries=[query],
        target_docs_description="年报",
        target_docs_keywords=["年报"],
        ordinal=1,
    )


@pytest.mark.asyncio
async def test_direct_service_executes_valid_llm_tool_plan() -> None:
    gateway = PlannerGateway(
        tool_calls=(
            LLMToolCall(
                call_id="call-1",
                name="get_content_of_pages",
                arguments={"documents": [{"doc_id": "d1", "page_numbers": [2]}]},
            ),
        )
    )
    service = DirectAccessService(
        primary=LLMDirectPlanner(gateway=gateway, model="weak", max_tokens=200),
        fallback=RuleDirectPlanner(),
        executor=RecordingExecutor(),
    )

    result = await service.run(
        request_id="r1", group=_group("routed_direct", "查看年报1第2页"), documents=[_profile()]
    )

    assert [chunk.content for chunk in result.chunks] == ["原文"]
    assert result.warnings == []
    request = gateway.requests[0]
    assert request.response_format is None
    assert request.tool_choice == "required"
    assert request.parallel_tool_calls is True
    assert all(tool["function"]["strict"] is True for tool in request.tools)


@pytest.mark.asyncio
async def test_direct_title_tree_planner_never_exposes_internal_node_ids() -> None:
    gateway = PlannerGateway(
        tool_calls=(
            LLMToolCall(
                call_id="call-tree",
                name="view_doc_title_tree",
                arguments={"doc_ids": ["d1"], "level": 3, "include_node_id": True},
            ),
        )
    )
    planner = LLMDirectPlanner(gateway=gateway, model="weak", max_tokens=200)

    plan = await planner.plan(
        request_id="r1", group_ref="g1", query="年报目录", documents=[_profile()]
    )

    assert plan.calls[0].tool_name == "view_doc_title_tree"
    assert plan.calls[0].include_node_id is False


@pytest.mark.asyncio
async def test_scope_llm_planner_uses_one_strict_function_call() -> None:
    gateway = PlannerGateway(
        tool_calls=(
            LLMToolCall(
                call_id="call-1",
                name="get_docs_metainfo",
                arguments={
                    "return_metainfo_types": ["doc_name", "page_count"],
                    "enumeration_limit": 4,
                },
            ),
        )
    )
    from app.workflows.scope_access.planner import LLMScopePlanner

    service = ScopeAccessService(
        primary=LLMScopePlanner(gateway=gateway, model="weak", max_tokens=200)
    )

    result = await service.run(
        request_id="r1",
        group=_group("scope_direct", "有哪些文档，各有几页"),
        profiles=[_profile()],
    )

    assert result.chunks
    request = gateway.requests[0]
    assert request.response_format is None
    assert request.tool_choice == "required"
    assert request.parallel_tool_calls is False
    assert all(tool["function"]["strict"] is True for tool in request.tools)
    metainfo_tool = next(
        tool for tool in request.tools if tool["function"]["name"] == "get_docs_metainfo"
    )
    assert "enumeration_limit" in metainfo_tool["function"]["parameters"]["properties"]
    assert "enumeration_limit" in metainfo_tool["function"]["parameters"]["required"]


@pytest.mark.asyncio
async def test_scope_llm_planner_honors_explicit_enumeration_limit() -> None:
    gateway = PlannerGateway(
        tool_calls=(
            LLMToolCall(
                call_id="call-1",
                name="get_docs_metainfo",
                arguments={
                    "return_metainfo_types": ["doc_name"],
                    "enumeration_limit": 10,
                },
            ),
        )
    )
    from app.workflows.scope_access.planner import LLMScopePlanner

    result = await ScopeAccessService(
        primary=LLMScopePlanner(gateway=gateway, model="weak", max_tokens=200)
    ).run(
        request_id="r1",
        group=_group("scope_direct", "列举当前请求范围内的文档"),
        profiles=[_profile(index) for index in range(1, 13)],
        original_query="你能看到哪些文档，列举10篇",
    )

    assert result.chunks[0].chunk_meta["requested_documents"] == 10
    assert result.chunks[0].chunk_meta["shown_documents"] == 10
    assert "年报10" in result.chunks[0].content
    assert "年报11" not in result.chunks[0].content
    assert (
        '"original_query": "你能看到哪些文档，列举10篇"'
        in (gateway.requests[0].messages[-1]["content"])
    )


@pytest.mark.asyncio
async def test_scope_metainfo_reports_when_requested_count_exceeds_scope() -> None:
    gateway = PlannerGateway(
        tool_calls=(
            LLMToolCall(
                call_id="call-1",
                name="get_docs_metainfo",
                arguments={
                    "return_metainfo_types": ["doc_name"],
                    "enumeration_limit": 10,
                },
            ),
        )
    )
    from app.workflows.scope_access.planner import LLMScopePlanner

    result = await ScopeAccessService(
        primary=LLMScopePlanner(gateway=gateway, model="weak", max_tokens=200)
    ).run(
        request_id="r1",
        group=_group("scope_direct", "你能看到哪些文档，列举10篇"),
        profiles=[_profile(index) for index in range(1, 8)],
    )

    assert result.chunks[0].chunk_meta["shown_documents"] == 7
    assert "当前用户只有 7 篇文档，已经列举所有文档" in result.chunks[0].hint
    assert "没有请求的" not in result.chunks[0].hint
    assert result.coverage_complete is True


def test_scope_rule_planner_uses_explicit_count_or_default_ten() -> None:
    from app.workflows.scope_access.planner import RuleScopePlanner

    planner = RuleScopePlanner()

    assert planner.plan(query="你能看到哪些文档").enumeration_limit == 10
    assert planner.plan(query="你能看到哪些文档，列举10篇").enumeration_limit == 10


@pytest.mark.asyncio
async def test_direct_service_drops_branch_when_primary_and_rule_planner_fail() -> None:
    service = DirectAccessService(
        primary=LLMDirectPlanner(gateway=PlannerGateway(fail=True), model="weak", max_tokens=200),
        fallback=RuleDirectPlanner(),
        executor=RecordingExecutor(),
    )

    result = await service.run(
        request_id="r1",
        group=_group("routed_direct", "帮我看看它"),
        documents=[_profile(), _profile(2)],
    )

    assert result.chunks == []
    assert result.warnings[0].code == "DIRECT_TOOL_PLANNER_FAILED"


@pytest.mark.asyncio
async def test_scope_service_uses_only_ten_stable_documents_for_overviews() -> None:
    service = ScopeAccessService()
    profiles = [_profile(index) for index in range(1, 13)]

    result = await service.run(group=_group("scope_direct", "总结上述所有文档"), profiles=profiles)

    assert len(result.chunks) == 10
    assert [chunk.document_id for chunk in result.chunks] == [
        f"d{index}" for index in range(1, 11)
    ]
    assert [chunk.document_ids for chunk in result.chunks] == [
        [f"d{index}"] for index in range(1, 11)
    ]
    assert result.warnings[0].code == "SCOPE_ENUMERATION_TRUNCATED"


@pytest.mark.asyncio
async def test_scope_rule_planner_treats_what_documents_are_about_as_descriptions() -> None:
    result = await ScopeAccessService().run(
        group=_group("scope_direct", "这些文档主要讲了什么"),
        profiles=[_profile(), _profile(2)],
    )

    assert [chunk.source_type for chunk in result.chunks] == [
        "document_overview",
        "document_overview",
    ]
