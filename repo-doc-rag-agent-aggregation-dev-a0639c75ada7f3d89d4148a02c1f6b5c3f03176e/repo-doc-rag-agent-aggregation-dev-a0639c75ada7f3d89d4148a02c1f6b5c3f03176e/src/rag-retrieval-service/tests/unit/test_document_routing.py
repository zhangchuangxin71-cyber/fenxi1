from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.budgeting.tokens import QueryBudgetError
from app.db.repositories import DocumentProfile
from app.llm.gateway import LLMRequest, LLMResult, LLMUsage
from app.workflows.classification.models import QueryGroup
from app.workflows.document_routing.models import DocumentRouteBatchOutput
from app.workflows.document_routing.prefilter import KeywordPrefilterService, _anchor_terms
from app.workflows.document_routing.service import (
    DocumentRoutingService,
    keyword_prefilter,
)


def _group(ref: str, keywords: list[str]) -> QueryGroup:
    return QueryGroup(
        group_ref=ref,
        category="routed_focused",
        queries=["A公司去年营业额是多少"],
        target_docs_description="A公司年度报告",
        target_docs_keywords=keywords,
        ordinal=1,
    )


def _doc(doc_id: str, name: str, description: str = "") -> DocumentProfile:
    return DocumentProfile(doc_id=doc_id, doc_name=name, doc_description=description)


class RoutingGateway:
    def __init__(self, fail_batch: int | None = None, grade: str = "possible") -> None:
        self.requests: list[LLMRequest] = []
        self.fail_batch = fail_batch
        self.grade = grade

    async def complete_json(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if self.fail_batch == len(self.requests):
            raise RuntimeError("batch failed")
        pairs = json.loads(request.messages[-1]["content"])["required_pairs"]
        return LLMResult(
            data={
                "decisions": [
                    {"group_ref": group_ref, "doc_id": doc_id, "grade": self.grade}
                    for group_ref, doc_id in pairs
                ]
            },
            usage=LLMUsage(total_tokens=10),
        )


class ExtraPairGateway(RoutingGateway):
    async def complete_json(self, request: LLMRequest) -> LLMResult:
        result = await super().complete_json(request)
        decisions = [*result.data["decisions"]]
        decisions.append({"group_ref": "invented", "doc_id": "invented", "grade": "accept"})
        return LLMResult(data={"decisions": decisions}, usage=result.usage)


class SparseRoutingGateway(RoutingGateway):
    async def complete_json(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        pairs = json.loads(request.messages[-1]["content"])["required_pairs"]
        selected = [pair for pair in pairs if pair[1] == "a"]
        return LLMResult(
            data={
                "decisions": [
                    {"group_ref": group_ref, "doc_id": doc_id, "grade": "accept"}
                    for group_ref, doc_id in selected
                ]
            },
            usage=LLMUsage(total_tokens=10),
        )


def test_keyword_prefilter_hard_excludes_zero_score_without_fixed_limit() -> None:
    result = keyword_prefilter(
        groups=[_group("g0001", ["A公司", "年报"])],
        documents=[
            _doc("a", "A公司2025年报"),
            _doc("b", "B公司员工手册", "人力资源制度"),
            _doc("c", "A公司经营报告"),
        ],
        threshold=0.01,
    )

    assert [candidate.document.doc_id for candidate in result["g0001"].candidates] == ["a", "c"]
    assert result["g0001"].rejected_count == 1


def test_keyword_prefilter_keeps_the_only_scoped_document_for_pronoun_query() -> None:
    group = _group("g0001", ["这篇文档"])
    result = keyword_prefilter(
        groups=[group],
        documents=[_doc("only", "无关键词标题", "无关键词摘要")],
        threshold=0.01,
    )

    assert [candidate.document.doc_id for candidate in result["g0001"].candidates] == ["only"]


def test_keyword_prefilter_keeps_all_documents_for_explicit_whole_scope_query() -> None:
    group = _group("g0001", ["上述所有文档"])
    group = group.model_copy(
        update={
            "category": "routed_broad",
            "queries": ["详细总结上述所有文档"],
            "target_docs_description": "上述所有文档",
        }
    )

    result = keyword_prefilter(
        groups=[group],
        documents=[
            _doc("d1", "第一份材料", "无匹配词"),
            _doc("d2", "第二份材料", "同样无匹配词"),
        ],
        threshold=0.01,
    )

    assert [candidate.document.doc_id for candidate in result["g0001"].candidates] == [
        "d1",
        "d2",
    ]


def test_secondary_prefilter_uses_group_identity_anchor_and_scope_idf() -> None:
    group = _group("g0001", ["某某大学", "毕业论文"])
    group = group.model_copy(
        update={
            "queries": ["张三的毕业论文研究了什么"],
            "target_docs_description": "某某大学张三的毕业论文",
        }
    )
    service = KeywordPrefilterService(threshold=0.01, max_candidates=2, count_tokens=len)

    result = service.run(
        groups=[group],
        documents=[
            _doc("target-id", "某某大学张三毕业论文", "张三研究城市交通"),
            _doc("li-id", "某某大学李四毕业论文", "李四研究金融"),
            _doc("wang-id", "某某大学王五毕业论文", "王五研究农业"),
        ],
    )

    assert result.overflow_group_refs == []
    assert [item.document.doc_id for item in result.results["g0001"].candidates] == ["target-id"]


def test_secondary_prefilter_keeps_ambiguous_candidates_and_marks_overflow() -> None:
    group = _group("g0001", ["某某大学", "毕业论文"])
    group = group.model_copy(
        update={
            "queries": ["某某大学的毕业论文研究了什么"],
            "target_docs_description": "某某大学毕业论文",
        }
    )
    service = KeywordPrefilterService(threshold=0.01, max_candidates=2, count_tokens=len)

    result = service.run(
        groups=[group],
        documents=[
            _doc("secret-a", "某某大学张三毕业论文"),
            _doc("secret-b", "某某大学李四毕业论文"),
            _doc("secret-c", "某某大学王五毕业论文"),
        ],
    )

    assert result.overflow_group_refs == ["g0001"]
    assert len(result.results["g0001"].candidates) == 3


def test_secondary_anchor_extraction_is_bounded_for_long_query() -> None:
    group = _group("g0001", ["毕业论文"])
    group = group.model_copy(
        update={
            "queries": ["".join(chr(0x4E00 + index) for index in range(1000))],
            "target_docs_description": "",
        }
    )

    assert len(_anchor_terms(group)) <= 512


def test_prefilter_overflow_result_lists_names_without_ids_and_stays_in_budget() -> None:
    group = _group("g0001", ["毕业论文"])
    service = KeywordPrefilterService(threshold=0.01, max_candidates=2, count_tokens=len)
    run = service.run(
        groups=[group],
        documents=[
            _doc("private-id-1", "张三毕业论文"),
            _doc("private-id-2", "李四毕业论文"),
            _doc("private-id-3", "王五毕业论文"),
        ],
    )

    result = service.build_overflow_result(run=run, groups=[group], max_return_tokens=170)

    assert result.returned_tokens <= 170
    assert len(result.chunks) == 1
    assert "private-id" not in result.chunks[0].content
    assert "private-id" not in result.chunks[0].hint
    assert "请询问用户具体需要访问哪篇文档" in result.chunks[0].hint
    assert result.chunks[0].chunk_meta["shown_documents"] >= 1
    assert (
        result.chunks[0].chunk_meta["shown_documents"]
        + result.chunks[0].chunk_meta["omitted_documents"]
        == 3
    )


@pytest.mark.asyncio
async def test_router_accepts_every_document_for_explicit_whole_scope_query() -> None:
    group = _group("g0001", ["上述所有文档"])
    group = group.model_copy(
        update={
            "category": "routed_broad",
            "queries": ["详细总结上述所有文档"],
            "target_docs_description": "上述所有文档",
        }
    )
    service = DocumentRoutingService(
        gateway=RoutingGateway(grade="reject"),
        model="weak",
        context_window=1200,
        output_tokens=100,
        safety_margin=50,
        count_tokens=len,
        prefilter_threshold=0.01,
    )

    routes, _ = await service.route(
        request_id="request-1",
        groups=[group],
        documents=[_doc("d1", "第一份材料"), _doc("d2", "第二份材料")],
    )

    assert [item.document.doc_id for item in routes[0].accept_docs] == ["d1", "d2"]


@pytest.mark.asyncio
async def test_router_keeps_only_scoped_document_for_scope_relative_query_even_if_llm_rejects() -> (
    None
):
    service = DocumentRoutingService(
        gateway=RoutingGateway(grade="reject"),
        model="weak",
        context_window=1200,
        output_tokens=100,
        safety_margin=50,
        count_tokens=len,
        prefilter_threshold=0.01,
    )

    routes, _ = await service.route(
        request_id="request-1",
        groups=[_group("g0001", ["这篇文档"])],
        documents=[_doc("only", "无关键词标题", "无关键词摘要")],
    )

    assert [item.document.doc_id for item in routes[0].accept_docs] == ["only"]
    assert routes[0].possible_docs == []


@pytest.mark.asyncio
async def test_router_lpt_processes_every_candidate_even_when_batches_exceed_concurrency() -> None:
    gateway = RoutingGateway()
    documents = [_doc(str(index), f"A公司年报{index}", "经营数据 " * 12) for index in range(8)]
    service = DocumentRoutingService(
        gateway=gateway,
        model="weak",
        context_window=1200,
        output_tokens=100,
        safety_margin=50,
        count_tokens=len,
        prefilter_threshold=0.01,
    )

    routes, warnings = await service.route(
        request_id="request-1", groups=[_group("g0001", ["A公司", "年报"])], documents=documents
    )

    routed_ids = {item.document.doc_id for item in routes[0].possible_docs}
    assert routed_ids == {str(index) for index in range(8)}
    assert len(gateway.requests) > 1
    assert warnings == []


@pytest.mark.asyncio
async def test_router_falls_back_only_for_failed_lpt_batch() -> None:
    gateway = RoutingGateway(fail_batch=1)
    documents = [_doc(str(index), f"A公司年报{index}", "经营数据 " * 12) for index in range(8)]
    service = DocumentRoutingService(
        gateway=gateway,
        model="weak",
        context_window=1200,
        output_tokens=100,
        safety_margin=50,
        count_tokens=len,
        prefilter_threshold=0.01,
    )

    routes, warnings = await service.route(
        request_id="request-1", groups=[_group("g0001", ["A公司", "年报"])], documents=documents
    )

    assert len(routes[0].accept_docs) + len(routes[0].possible_docs) == 8
    assert routes[0].degraded is True
    assert any(warning.code == "DOCUMENT_ROUTING_BATCH_RULE_FALLBACK" for warning in warnings)


@pytest.mark.asyncio
async def test_router_treats_omitted_sparse_selections_as_reject_without_fallback() -> None:
    gateway = SparseRoutingGateway()
    service = DocumentRoutingService(
        gateway=gateway,
        model="weak",
        context_window=1200,
        output_tokens=100,
        safety_margin=50,
        count_tokens=len,
        prefilter_threshold=0.01,
    )

    routes, warnings = await service.route(
        request_id="request-1",
        groups=[_group("g0001", ["A公司", "年报"])],
        documents=[_doc("a", "A公司年报"), _doc("b", "A公司年报补充材料")],
    )

    assert [item.document.doc_id for item in routes[0].accept_docs] == ["a"]
    assert routes[0].possible_docs == []
    assert routes[0].rejected_document_count == 1
    assert routes[0].degraded is False
    assert not any(warning.code == "DOCUMENT_ROUTING_BATCH_RULE_FALLBACK" for warning in warnings)


@pytest.mark.asyncio
async def test_router_rule_fallback_replaces_batch_that_invents_a_pair() -> None:
    service = DocumentRoutingService(
        gateway=ExtraPairGateway(),
        model="weak",
        context_window=1200,
        output_tokens=100,
        safety_margin=50,
        count_tokens=len,
        prefilter_threshold=0.01,
    )

    routes, warnings = await service.route(
        request_id="request-1",
        groups=[_group("g0001", ["A公司", "年报"])],
        documents=[_doc("a", "A公司年报")],
    )

    assert routes[0].accept_docs[0].decision_source == "rule_fallback"
    assert any(warning.code == "DOCUMENT_ROUTING_BATCH_RULE_FALLBACK" for warning in warnings)


@pytest.mark.asyncio
async def test_router_rejects_groups_that_do_not_fit_fixed_context() -> None:
    service = DocumentRoutingService(
        gateway=RoutingGateway(),
        model="weak",
        context_window=120,
        output_tokens=50,
        safety_margin=50,
        count_tokens=len,
        prefilter_threshold=0.01,
    )
    huge = _group("g0001", ["A公司"])
    huge = huge.model_copy(update={"queries": ["问题" * 100]})

    with pytest.raises(QueryBudgetError):
        await service.route(request_id="request-1", groups=[huge], documents=[_doc("a", "A公司")])


@pytest.mark.asyncio
async def test_router_truncates_only_oversized_description_view_without_dropping_document() -> None:
    gateway = RoutingGateway()
    document = _doc("a", "A公司年报", "经营信息" * 2000)
    service = DocumentRoutingService(
        gateway=gateway,
        model="weak",
        context_window=1200,
        output_tokens=100,
        safety_margin=50,
        count_tokens=len,
        prefilter_threshold=0.01,
    )

    routes, warnings = await service.route(
        request_id="request-1",
        groups=[_group("g0001", ["A公司", "年报"])],
        documents=[document],
    )

    assert routes[0].possible_docs[0].document.doc_description == document.doc_description
    assert any(warning.code == "DOCUMENT_DESCRIPTION_TRUNCATED_FOR_ROUTING" for warning in warnings)


def test_route_batch_contract_rejects_model_generated_pair() -> None:
    output = DocumentRouteBatchOutput.model_validate(
        {"decisions": [{"group_ref": "g0001", "doc_id": "d1", "grade": "accept"}]}
    )
    assert output.decisions[0].grade == "accept"


def test_route_batch_contract_does_not_allow_explicit_reject_output() -> None:
    with pytest.raises(ValidationError):
        DocumentRouteBatchOutput.model_validate(
            {"decisions": [{"group_ref": "g0001", "doc_id": "d1", "grade": "reject"}]}
        )
