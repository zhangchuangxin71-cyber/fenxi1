from __future__ import annotations

import asyncio

import pytest

from app.api.schemas import RetrievalWarning, RetrieveOptions, RetrieveRequest
from app.config.settings import Settings
from app.core.errors import ApiError
from app.core.models import CandidateChunk
from app.db.repositories import DocumentProfile
from app.llm.gateway import GatewayCallRecord, GatewayRequestStats
from app.service import RetrievalEngine
from app.workflows.classification.models import QueryGroup
from app.workflows.merge.service import merge_candidates


class Gateway:
    def __init__(
        self,
        stats: GatewayRequestStats | None = None,
        records: tuple[GatewayCallRecord, ...] = (),
    ) -> None:
        self.cleared: list[str] = []
        self._stats = stats or GatewayRequestStats(request_count=0)
        self._records = records
        self.started: list[tuple[str, bool]] = []

    def begin_request(self, request_id: str, *, debug_enabled: bool) -> None:
        self.started.append((request_id, debug_enabled))

    def stats(self, _: str) -> GatewayRequestStats:
        return self._stats

    def clear_stats(self, request_id: str) -> None:
        self.cleared.append(request_id)

    def call_records(self, _: str) -> tuple[GatewayCallRecord, ...]:
        return self._records


class NoDebugGateway(Gateway):
    def call_records(self, _: str) -> tuple[GatewayCallRecord, ...]:
        raise AssertionError("debug-disabled requests must not read LLM call records")


class NeverGraph:
    def __init__(self) -> None:
        self.calls = 0

    async def astream(self, *_: object, **__: object):
        self.calls += 1
        if False:
            yield {}


class PartialGraph:
    async def astream(self, state: dict, **_: object):
        collector = state.get("trace_collector")
        if collector is not None:
            collector.record_detail(
                node="focused_search",
                kind="tool_result",
                payload={"tool": "scan_node_tree", "nodes": [{"title": "财务报告"}]},
            )
        yield {
            **state,
            "scope_documents": [
                DocumentProfile(
                    doc_id="d1",
                    doc_name="文档",
                    doc_type="pdf",
                    doc_description="数据库摘要",
                    page_count=1,
                    node_count=2,
                )
            ],
            "groups": [
                QueryGroup(group_ref="g1", category="routed_focused", queries=["问题"], ordinal=1)
            ],
            "classification_trace": [
                {
                    "phase": "scope_decision",
                    "decisions": {"q_001": False},
                    "scope_query_refs": [],
                    "routed_query_refs": ["q_001"],
                }
            ],
            "candidate_chunks": [
                CandidateChunk(
                    chunk_id="d1:page:1",
                    document_id="d1",
                    document_ids=["d1"],
                    document_name="文档",
                    page_number=1,
                    path="document:d1:page:1",
                    content="原文",
                    source_type="page",
                    category="routed_focused",
                    group_matches={"g1": "accept"},
                    questions_by_group={"g1": ["问题"]},
                )
            ],
            "stats": {"inspected_node_count": 7, "inspected_page_count": 8},
        }
        await asyncio.sleep(1)


class EmptySlowGraph:
    async def astream(self, state: dict, **_: object):
        del state
        await asyncio.sleep(1)
        if False:
            yield {}


class MissingDirectResourceGraph:
    async def astream(self, state: dict, **_: object):
        group = QueryGroup(group_ref="g1", category="routed_direct", queries=["第12页"], ordinal=1)
        yield {
            **state,
            "groups": [group],
            "candidate_chunks": [],
            "warnings": [
                RetrievalWarning(
                    code="DIRECT_RESOURCE_NOT_FOUND",
                    message="missing page",
                    affected_group_refs=["g1"],
                    affected_document_ids=["d1"],
                )
            ],
            "merge_result": merge_candidates(
                candidates=[],
                groups=[group],
                top_k=1,
                max_return_tokens=128,
                count_tokens=len,
            ),
        }


def _settings(**updates: object) -> Settings:
    values = {
        "_env_file": None,
        "request_soft_deadline_seconds": 0.05,
        "request_hard_deadline_seconds": 0.1,
        "request_finalization_reserve_seconds": 0.02,
        **updates,
    }
    return Settings(**values)


@pytest.mark.asyncio
async def test_keyword_mode_is_explicitly_unimplemented_without_entering_graph() -> None:
    graph = NeverGraph()
    gateway = Gateway()
    engine = RetrievalEngine(
        graph=graph,
        gateway=gateway,
        settings=_settings(),
        count_tokens=len,
        tokenizer_name="test",
        merge=merge_candidates,
    )

    with pytest.raises(ApiError) as error:
        await engine.retrieve(
            RetrieveRequest(user_id="u", kb_id="kb", query="问题", search_mode="keyword")
        )

    assert error.value.code == "MODE_NOT_IMPLEMENTED"
    assert graph.calls == 0


@pytest.mark.asyncio
async def test_return_token_budget_uses_configured_service_upper_bound() -> None:
    graph = NeverGraph()
    engine = RetrievalEngine(
        graph=graph,
        gateway=Gateway(),
        settings=_settings(rag_max_return_tokens=180_000),
        count_tokens=len,
        tokenizer_name="test",
        merge=merge_candidates,
    )

    with pytest.raises(ApiError) as error:
        await engine.retrieve(
            RetrieveRequest(
                user_id="u",
                kb_id="kb",
                query="问题",
                search_mode="semantic",
                max_return_tokens=180_001,
            )
        )

    assert error.value.code == "INVALID_RETURN_TOKEN_BUDGET"
    assert graph.calls == 0


@pytest.mark.asyncio
async def test_hard_timeout_returns_partial_result_and_warning_when_evidence_exists() -> None:
    gateway = Gateway()
    engine = RetrievalEngine(
        graph=PartialGraph(),
        gateway=gateway,
        settings=_settings(),
        count_tokens=len,
        tokenizer_name="test",
        merge=merge_candidates,
    )

    response = await engine.retrieve(
        RetrieveRequest(
            user_id="u", kb_id="kb", query="问题", search_mode="semantic", max_return_tokens=128
        )
    )

    assert response.chunks[0].content == "原文"
    assert response.chunks[0].document_meta.doc_type == "pdf"
    assert response.usage.inspected_node_count == 7
    assert response.usage.inspected_page_count == 8
    assert any(warning.code == "REQUEST_HARD_DEADLINE_REACHED" for warning in response.warnings)
    assert response.coverage.complete is False


@pytest.mark.asyncio
async def test_hard_timeout_without_complete_evidence_returns_504() -> None:
    engine = RetrievalEngine(
        graph=EmptySlowGraph(),
        gateway=Gateway(),
        settings=_settings(),
        count_tokens=len,
        tokenizer_name="test",
        merge=merge_candidates,
    )

    with pytest.raises(ApiError) as error:
        await engine.retrieve(
            RetrieveRequest(user_id="u", kb_id="kb", query="问题", search_mode="semantic")
        )

    assert error.value.status_code == 504
    assert error.value.code == "REQUEST_TIMEOUT"


@pytest.mark.asyncio
async def test_missing_direct_resource_without_other_evidence_returns_404() -> None:
    engine = RetrievalEngine(
        graph=MissingDirectResourceGraph(),
        gateway=Gateway(),
        settings=_settings(),
        count_tokens=len,
        tokenizer_name="test",
        merge=merge_candidates,
    )

    with pytest.raises(ApiError) as error:
        await engine.retrieve(
            RetrieveRequest(user_id="u", kb_id="kb", query="第12页", search_mode="semantic")
        )

    assert error.value.status_code == 404
    assert error.value.code == "DIRECT_RESOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_debug_disabled_does_not_collect_or_read_trace_details() -> None:
    gateway = NoDebugGateway(GatewayRequestStats(request_count=2, total_tokens=999))
    engine = RetrievalEngine(
        graph=PartialGraph(),
        gateway=gateway,
        settings=_settings(rag_debug_enabled=False),
        count_tokens=len,
        tokenizer_name="test",
        merge=merge_candidates,
    )

    response = await engine.retrieve(
        RetrieveRequest(
            user_id="u",
            kb_id="kb",
            query="问题",
            search_mode="semantic",
            max_return_tokens=128,
            options=RetrieveOptions(include_debug=True),
        )
    )

    assert response.debug is None
    assert gateway.started and gateway.started[0][1] is False


@pytest.mark.asyncio
async def test_debug_contains_input_state_candidates_and_full_llm_calls() -> None:
    gateway = Gateway(
        GatewayRequestStats(
            request_count=2,
            prompt_tokens=120,
            completion_tokens=20,
            total_tokens=140,
            queue_wait_ms=7,
            provider_duration_ms=15,
            phase_counts={"page_inspection": 2},
        ),
        records=(
            GatewayCallRecord(
                model="weak-model",
                phase="page_inspection:1/2",
                group_index=1,
                group_count=2,
                queue_wait_ms=3,
                provider_duration_ms=5,
                prompt_tokens=60,
                completion_tokens=10,
                provider_request_id="safe-provider-id",
                request={
                    "model": "weak-model",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": "逐页判断原文"}],
                },
                response={
                    "data": {"decisions": [{"page": 1, "grade": "accept"}]},
                    "tool_calls": [],
                },
            ),
        ),
    )
    engine = RetrievalEngine(
        graph=PartialGraph(),
        gateway=gateway,
        settings=_settings(rag_debug_enabled=True),
        count_tokens=len,
        tokenizer_name="test",
        merge=merge_candidates,
    )

    response = await engine.retrieve(
        RetrieveRequest(
            user_id="u",
            kb_id="kb",
            query="问题",
            search_mode="semantic",
            max_return_tokens=128,
            options=RetrieveOptions(include_debug=True),
        )
    )

    assert response.debug is not None
    assert response.debug.state_summary["inspected_node_count"] == 7
    assert response.debug.state_summary["inspected_page_count"] == 8
    assert response.debug.state_summary["llm_request_count"] == 2
    assert response.debug.state_summary["llm_prompt_tokens"] == 120
    assert response.debug.state_summary["llm_completion_tokens"] == 20
    assert response.debug.state_summary["llm_total_tokens"] == 140
    assert response.debug.state_summary["llm_phase_counts"] == {"page_inspection": 2}
    llm_event = next(event for event in response.debug.trace if event.llm is not None)
    assert llm_event.llm.phase == "page_inspection:1/2"
    assert llm_event.llm.group_index == 1
    assert llm_event.llm.group_count == 2
    assert llm_event.llm.queue_wait_ms == 3
    assert llm_event.llm.provider_duration_ms == 5
    assert response.debug.input["query"] == "问题"
    assert response.debug.groups[0]["category"] == "routed_focused"
    assert response.debug.classification_trace[0]["phase"] == "scope_decision"
    assert response.debug.classification_trace[0]["routed_query_refs"] == ["q_001"]
    assert response.debug.candidates[0]["content"] == "原文"
    assert response.debug.llm_calls[0]["request"]["messages"][0]["content"] == "逐页判断原文"
    assert response.debug.llm_calls[0]["response"]["data"]["decisions"][0]["grade"] == "accept"
    assert response.debug.tool_events[0]["kind"] == "tool_result"
    assert response.debug.tool_events[0]["payload"]["tool"] == "scan_node_tree"
