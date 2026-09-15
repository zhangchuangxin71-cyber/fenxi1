from __future__ import annotations

import asyncio

import pytest

from app.api.schemas import RetrievalWarning, RetrieveOptions, RetrieveRequest
from app.core.models import CandidateChunk, ToolOutput
from app.db.repositories import DocumentProfile
from app.graph.builder import RetrievalGraphServices, build_retrieval_graph
from app.llm.gateway import GatewayRequestStats
from app.observability.trace import TraceCollector
from app.workflows.classification.models import QueryGroup
from app.workflows.classification.strategies import ClassificationRun
from app.workflows.document_routing.models import DocumentRoute, RoutedDocument
from app.workflows.document_routing.prefilter import KeywordPrefilterService
from app.workflows.merge.service import merge_candidates


class Repository:
    def fetch_scope(self, **_: object):
        return [
            DocumentProfile(doc_id="d1", doc_name="年报", doc_description="摘要", page_count=1)
        ], []


class Classifier:
    async def classify(self, **_: object) -> ClassificationRun:
        return ClassificationRun(
            groups=[
                QueryGroup(
                    group_ref="g0001", category="scope_direct", queries=["有哪些文档"], ordinal=1
                ),
                QueryGroup(
                    group_ref="g0002",
                    category="routed_focused",
                    queries=["营业额"],
                    target_docs_description="年报",
                    target_docs_keywords=["年报"],
                    ordinal=2,
                ),
            ],
            warnings=[],
        )


class Router:
    def __init__(self) -> None:
        self.calls = 0

    async def route(self, **_: object):
        self.calls += 1
        return [
            DocumentRoute(
                group_ref="g0002",
                accept_docs=[],
                possible_docs=[],
                rejected_document_count=1,
                prefilter_candidate_count=0,
                prefilter_rejected_count=1,
            )
        ], []


class Scope:
    def run(
        self,
        *,
        group: QueryGroup,
        profiles: list[DocumentProfile],
        request_id: str = "",
        original_query: str = "",
    ):
        assert isinstance(original_query, str)
        return ToolOutput(
            chunks=[
                CandidateChunk(
                    chunk_id="scope",
                    path="request:scope",
                    content="可访问年报",
                    source_type="scope_metadata",
                    category="scope_direct",
                    group_matches={group.group_ref: "accept"},
                    questions_by_group={group.group_ref: group.queries},
                )
            ],
            warnings=[
                RetrievalWarning(
                    code="SCOPE_TOOL_PLANNER_RULE_FALLBACK",
                    message="rule fallback",
                    affected_group_refs=[group.group_ref],
                )
            ],
            coverage_complete=False,
        )


class NeverLLMPostCheckpoint:
    def __init__(self, name: str, order: list[str]) -> None:
        self.name = name
        self.order = order

    def retrieve(self, **_: object):
        self.order.append(self.name)
        return ToolOutput(chunks=[])


class Gateway:
    def stats(self, _: str) -> GatewayRequestStats:
        return GatewayRequestStats(request_count=3, total_tokens=30)


class NeverKeywordPrefilter:
    def run(self, **_: object):
        raise AssertionError("single-document requests must bypass keyword prefilter")


class BroadClassifier:
    async def classify(self, **_: object) -> ClassificationRun:
        return ClassificationRun(
            groups=[
                QueryGroup(
                    group_ref="g0001",
                    category="routed_broad",
                    queries=["总结当前请求范围内唯一文档的内容"],
                    target_docs_description="当前请求范围内唯一文档",
                    target_docs_keywords=["唯一文档"],
                    ordinal=1,
                )
            ],
            warnings=[],
        )


class CaptureBroadRetrieval:
    def __init__(self) -> None:
        self.routes: list[DocumentRoute] = []

    def retrieve(self, *, routes: list[DocumentRoute], **_: object) -> ToolOutput:
        self.routes = routes
        return ToolOutput(
            chunks=[
                CandidateChunk(
                    chunk_id="d1:page:1",
                    document_id="d1",
                    document_ids=["d1"],
                    document_name="年报",
                    page_number=1,
                    path="document:d1:page:1",
                    content="唯一文档原文",
                    source_type="page",
                    category="routed_broad",
                    group_matches={"g0001": "accept"},
                    questions_by_group={"g0001": ["总结当前请求范围内唯一文档的内容"]},
                )
            ]
        )


class CoreFanoutClassifier:
    async def classify(self, **_: object) -> ClassificationRun:
        groups = [
            QueryGroup(group_ref="g0001", category="scope_direct", queries=["列举文档"], ordinal=1),
            QueryGroup(group_ref="g0002", category="scope_direct", queries=["概览文档"], ordinal=2),
            QueryGroup(
                group_ref="g0003",
                category="routed_direct",
                queries=["年报有几页"],
                target_docs_description="年报",
                target_docs_keywords=["年报"],
                ordinal=3,
            ),
            QueryGroup(
                group_ref="g0004",
                category="routed_direct",
                queries=["年报目录"],
                target_docs_description="年报",
                target_docs_keywords=["年报"],
                ordinal=4,
            ),
        ]
        return ClassificationRun(groups=groups, warnings=[])


class RouteAllDirectGroups:
    async def route(
        self, *, groups: list[QueryGroup], documents: list[DocumentProfile], **_: object
    ):
        document = documents[0]
        return [
            DocumentRoute(
                group_ref=group.group_ref,
                accept_docs=[
                    RoutedDocument(
                        document=document,
                        grade="accept",
                        keyword_score=1.0,
                        decision_source="rule_fallback",
                    )
                ],
                possible_docs=[],
                rejected_document_count=0,
                prefilter_candidate_count=1,
                prefilter_rejected_count=0,
            )
            for group in groups
        ], []


class CoreAccessBarrier:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.started: list[str] = []
        self.all_started = asyncio.Event()

    async def enter(self, group_ref: str) -> None:
        self.started.append(group_ref)
        if len(self.started) == self.expected:
            self.all_started.set()
        await asyncio.wait_for(self.all_started.wait(), timeout=0.5)


def _core_chunk(group: QueryGroup) -> CandidateChunk:
    return CandidateChunk(
        chunk_id=f"chunk-{group.group_ref}",
        document_id="d1" if group.category == "routed_direct" else None,
        document_name="年报" if group.category == "routed_direct" else None,
        path=f"test:{group.group_ref}",
        content=f"content-{group.group_ref}",
        source_type=(
            "document_overview" if group.category == "routed_direct" else "scope_metadata"
        ),
        category=group.category,
        group_matches={group.group_ref: "accept"},
        questions_by_group={group.group_ref: group.queries},
    )


class ConcurrentScopeAccess:
    def __init__(self, barrier: CoreAccessBarrier, *, failing_refs: set[str] | None = None) -> None:
        self.barrier = barrier
        self.failing_refs = failing_refs or set()

    async def run(self, *, group: QueryGroup, **_: object) -> ToolOutput:
        await self.barrier.enter(group.group_ref)
        if group.group_ref in self.failing_refs:
            raise RuntimeError("scope failed")
        return ToolOutput(chunks=[_core_chunk(group)])


class ConcurrentDirectAccess:
    def __init__(self, barrier: CoreAccessBarrier, *, failing_refs: set[str] | None = None) -> None:
        self.barrier = barrier
        self.failing_refs = failing_refs or set()

    async def run(self, *, group: QueryGroup, **_: object) -> ToolOutput:
        await self.barrier.enter(group.group_ref)
        if group.group_ref in self.failing_refs:
            raise RuntimeError("direct failed")
        return ToolOutput(chunks=[_core_chunk(group)])


def _core_fanout_graph(*, scope_access: object, direct_access: object):
    return build_retrieval_graph(
        RetrievalGraphServices(
            repository=Repository(),
            classifier=CoreFanoutClassifier(),
            router=RouteAllDirectGroups(),
            keyword_prefilter=KeywordPrefilterService(
                threshold=0.01, max_candidates=32, count_tokens=len
            ),
            scope_access=scope_access,
            direct_access=direct_access,
            focused_search=None,
            optional_possible=NeverLLMPostCheckpoint("possible", []),
            broad_retrieval=NeverLLMPostCheckpoint("broad", []),
            gateway=Gateway(),
            merge=merge_candidates,
            count_tokens=len,
            default_return_tokens=1000,
        )
    )


def _core_fanout_request() -> RetrieveRequest:
    return RetrieveRequest(
        user_id="u",
        kb_id="kb",
        query=["列举文档", "概览文档", "年报有几页", "年报目录"],
        search_mode="semantic",
        top_k=10,
    )


@pytest.mark.asyncio
async def test_langgraph_runs_checkpoint_extensions_in_order_and_returns_state() -> None:
    order: list[str] = []
    services = RetrievalGraphServices(
        repository=Repository(),
        classifier=Classifier(),
        router=Router(),
        keyword_prefilter=KeywordPrefilterService(
            threshold=0.01, max_candidates=32, count_tokens=len
        ),
        scope_access=Scope(),
        direct_access=None,
        focused_search=None,
        optional_possible=NeverLLMPostCheckpoint("possible", order),
        broad_retrieval=NeverLLMPostCheckpoint("broad", order),
        gateway=Gateway(),
        merge=merge_candidates,
        count_tokens=len,
        default_return_tokens=1000,
    )
    graph = build_retrieval_graph(services)
    graph_view = graph.get_graph()
    assert set(graph_view.nodes) == {
        "__start__",
        "validate_and_load_scope",
        "classify_query",
        "keyword_prefilter",
        "multi_document_route",
        "core_access",
        "core_budget_checkpoint",
        "optional_focused_possible",
        "broad_retrieval",
        "merge",
        "__end__",
    }
    assert {(edge.source, edge.target) for edge in graph_view.edges} == {
        ("__start__", "validate_and_load_scope"),
        ("validate_and_load_scope", "classify_query"),
        ("classify_query", "keyword_prefilter"),
        ("classify_query", "core_access"),
        ("keyword_prefilter", "multi_document_route"),
        ("keyword_prefilter", "__end__"),
        ("multi_document_route", "core_access"),
        ("core_access", "core_budget_checkpoint"),
        ("core_budget_checkpoint", "optional_focused_possible"),
        ("optional_focused_possible", "broad_retrieval"),
        ("broad_retrieval", "merge"),
        ("merge", "__end__"),
    }
    request = RetrieveRequest(
        user_id="u",
        kb_id="kb",
        query=["有哪些文档", "营业额"],
        search_mode="semantic",
        options=RetrieveOptions(include_debug=True),
    )
    collector = TraceCollector(enabled=True, request_id="r1")

    result = await graph.ainvoke(
        {"request": request, "request_id": "r1", "trace_collector": collector}
    )

    assert order == ["possible", "broad"]
    assert result["merge_result"].chunks[0].content == "可访问年报"
    assert result["llm_request_count_at_checkpoint"] == 3
    assert result["llm_request_count_after_extensions"] == 3
    assert result["merge_result"].coverage.degraded_group_refs == ["g0001"]
    core_event = next(event for event in collector.events if event.node == "core_access")
    assert core_event.status == "degraded"
    assert core_event.fallback_used is True


@pytest.mark.asyncio
async def test_single_document_bypasses_prefilter_and_router_for_routed_group() -> None:
    router = Router()
    broad = CaptureBroadRetrieval()
    graph = build_retrieval_graph(
        RetrievalGraphServices(
            repository=Repository(),
            classifier=BroadClassifier(),
            router=router,
            keyword_prefilter=NeverKeywordPrefilter(),
            scope_access=Scope(),
            direct_access=None,
            focused_search=None,
            optional_possible=NeverLLMPostCheckpoint("possible", []),
            broad_retrieval=broad,
            gateway=Gateway(),
            merge=merge_candidates,
            count_tokens=len,
            default_return_tokens=1000,
        )
    )

    result = await graph.ainvoke(
        {
            "request": RetrieveRequest(
                user_id="u",
                kb_id="kb",
                query="总结当前请求范围内唯一文档的内容",
                doc_ids=["d1"],
                search_mode="semantic",
            ),
            "request_id": "r1",
            "trace_collector": TraceCollector(enabled=False, request_id="r1"),
        }
    )

    assert router.calls == 0
    assert broad.routes[0].accept_docs[0].document.doc_id == "d1"
    assert broad.routes[0].accept_docs[0].decision_source == "rule_fallback"
    assert result["merge_result"].chunks[0].content == "唯一文档原文"
    assert not any(warning.code == "KEYWORD_PREFILTER_EMPTY" for warning in result["warnings"])


@pytest.mark.asyncio
async def test_soft_deadline_skips_all_post_checkpoint_optional_work() -> None:
    order: list[str] = []
    services = RetrievalGraphServices(
        repository=Repository(),
        classifier=Classifier(),
        router=Router(),
        keyword_prefilter=KeywordPrefilterService(
            threshold=0.01, max_candidates=32, count_tokens=len
        ),
        scope_access=Scope(),
        direct_access=None,
        focused_search=None,
        optional_possible=NeverLLMPostCheckpoint("possible", order),
        broad_retrieval=NeverLLMPostCheckpoint("broad", order),
        gateway=Gateway(),
        merge=merge_candidates,
        count_tokens=len,
        default_return_tokens=1000,
    )
    graph = build_retrieval_graph(services)
    request = RetrieveRequest(
        user_id="u", kb_id="kb", query="有哪些文档；营业额", search_mode="semantic"
    )

    result = await graph.ainvoke(
        {
            "request": request,
            "request_id": "r1",
            "soft_deadline_at": 0,
            "trace_collector": TraceCollector(enabled=False, request_id="r1"),
        }
    )

    assert order == []
    assert result["merge_result"].chunks
    assert any(warning.code == "REQUEST_SOFT_DEADLINE_REACHED" for warning in result["warnings"])


@pytest.mark.asyncio
async def test_keyword_overflow_returns_budgeted_result_without_calling_router() -> None:
    router = Router()
    keyword = KeywordPrefilterService(threshold=0.01, max_candidates=2, count_tokens=len)
    repository = Repository()
    repository.fetch_scope = lambda **_: (
        [
            DocumentProfile(doc_id="private-1", doc_name="年报甲", doc_description="年报"),
            DocumentProfile(doc_id="private-2", doc_name="年报乙", doc_description="年报"),
            DocumentProfile(doc_id="private-3", doc_name="年报丙", doc_description="年报"),
        ],
        [],
    )

    class FocusedClassifier:
        async def classify(self, **_: object) -> ClassificationRun:
            return ClassificationRun(
                groups=[
                    QueryGroup(
                        group_ref="g0001",
                        category="routed_focused",
                        queries=["年报中的营业额是多少"],
                        target_docs_description="年报",
                        target_docs_keywords=["年报"],
                        ordinal=1,
                    )
                ],
                warnings=[],
            )

    services = RetrievalGraphServices(
        repository=repository,
        classifier=FocusedClassifier(),
        keyword_prefilter=keyword,
        router=router,
        scope_access=Scope(),
        direct_access=None,
        focused_search=None,
        optional_possible=NeverLLMPostCheckpoint("possible", []),
        broad_retrieval=NeverLLMPostCheckpoint("broad", []),
        gateway=Gateway(),
        merge=merge_candidates,
        count_tokens=len,
        default_return_tokens=1000,
    )
    graph = build_retrieval_graph(services)

    result = await graph.ainvoke(
        {
            "request": RetrieveRequest(
                user_id="u",
                kb_id="kb",
                query="年报中的营业额是多少",
                search_mode="semantic",
                max_return_tokens=180,
            ),
            "request_id": "r1",
            "trace_collector": TraceCollector(enabled=False, request_id="r1"),
        }
    )

    assert router.calls == 0
    assert result["merge_result"].returned_tokens <= 180
    assert "请询问用户具体需要访问哪篇文档" in result["merge_result"].chunks[0].hint
    assert "llm_request_count_at_checkpoint" not in result


@pytest.mark.asyncio
async def test_core_access_fans_out_scope_and_direct_groups_together() -> None:
    barrier = CoreAccessBarrier(expected=4)
    graph = _core_fanout_graph(
        scope_access=ConcurrentScopeAccess(barrier),
        direct_access=ConcurrentDirectAccess(barrier),
    )

    result = await graph.ainvoke(
        {
            "request": _core_fanout_request(),
            "request_id": "r1",
            "trace_collector": TraceCollector(enabled=False, request_id="r1"),
        }
    )

    assert set(barrier.started) == {"g0001", "g0002", "g0003", "g0004"}
    assert [chunk.content for chunk in result["merge_result"].chunks] == [
        "content-g0001",
        "content-g0002",
        "content-g0003",
        "content-g0004",
    ]


@pytest.mark.asyncio
async def test_core_access_discards_only_failed_scope_and_direct_groups() -> None:
    barrier = CoreAccessBarrier(expected=4)
    graph = _core_fanout_graph(
        scope_access=ConcurrentScopeAccess(barrier, failing_refs={"g0001"}),
        direct_access=ConcurrentDirectAccess(barrier, failing_refs={"g0003"}),
    )

    result = await graph.ainvoke(
        {
            "request": _core_fanout_request(),
            "request_id": "r1",
            "trace_collector": TraceCollector(enabled=False, request_id="r1"),
        }
    )

    assert [chunk.content for chunk in result["merge_result"].chunks] == [
        "content-g0002",
        "content-g0004",
    ]
    failures = {
        warning.code: warning.affected_group_refs
        for warning in result["warnings"]
        if warning.code in {"SCOPE_GROUP_FAILED", "DIRECT_GROUP_FAILED"}
    }
    assert failures == {
        "SCOPE_GROUP_FAILED": ["g0001"],
        "DIRECT_GROUP_FAILED": ["g0003"],
    }
