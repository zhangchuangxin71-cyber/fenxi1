from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.db.repositories import NodeRecord, PageRecord
from app.llm.gateway import LLMRequest, LLMResult, LLMUsage
from app.tools.node_tree import NodeTreeScanner
from app.workflows.classification.models import QueryGroup
from app.workflows.focused_search.models import PageDecisionBatch
from app.workflows.focused_search.page_inspector import PageInspector


class FocusedRepository:
    def __init__(self) -> None:
        self.nodes = [
            NodeRecord(
                doc_id="d1",
                node_id="root",
                parent_node_id=None,
                sibling_order=0,
                level=1,
                title="根",
                summary="",
                start_page=1,
                end_page=6,
                child_count=3,
            ),
            *[
                NodeRecord(
                    doc_id="d1",
                    node_id=f"n{index}",
                    parent_node_id="root",
                    sibling_order=index,
                    level=2,
                    title=f"章节{index}",
                    summary=f"主题{index}",
                    start_page=index * 2 - 1,
                    end_page=index * 2,
                    child_count=0,
                )
                for index in range(1, 4)
            ],
        ]
        self.pages = [
            PageRecord(
                doc_id="d1",
                document_name="测试文档",
                page_number=index,
                content=f"第{index}页 营业额",
            )
            for index in range(1, 7)
        ]

    def fetch_nodes(self, **_: object) -> list[NodeRecord]:
        return self.nodes

    def fetch_pages(self, *, pages: list[int] | None = None, **_: object) -> list[PageRecord]:
        return [page for page in self.pages if pages is None or page.page_number in pages]


class PageGateway:
    def __init__(self, omit_last: bool = False, add_unknown: bool = False) -> None:
        self.requests: list[LLMRequest] = []
        self.omit_last = omit_last
        self.add_unknown = add_unknown

    async def complete_json(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        payload = json.loads(request.messages[-1]["content"])
        pages = [item["page_number"] for item in payload["pages"]]
        if self.omit_last:
            pages = [page for page in pages if page != 2]
        if self.add_unknown:
            pages.append(999999)
        return LLMResult(
            data={"decisions": [{"page_number": page, "grade": "accept"} for page in pages]},
            usage=LLMUsage(total_tokens=10),
        )


def _group() -> QueryGroup:
    return QueryGroup(
        group_ref="g1",
        category="routed_focused",
        queries=["营业额是多少"],
        target_docs_description="测试文档",
        target_docs_keywords=["测试"],
        ordinal=1,
    )


def test_scan_node_tree_returns_complete_flat_level_or_explicit_too_large() -> None:
    scanner = NodeTreeScanner(
        repository=FocusedRepository(),
        user_id="u",
        kb_id="kb",
        count_tokens=len,
        max_result_tokens=1000,
    )
    complete = scanner.scan(doc_id="d1", root_node_id="root", level=1)
    tiny = NodeTreeScanner(
        repository=FocusedRepository(),
        user_id="u",
        kb_id="kb",
        count_tokens=len,
        max_result_tokens=10,
    ).scan(doc_id="d1", root_node_id="root", level=1)

    assert [node.node_id for node in complete.nodes] == ["n1", "n2", "n3"]
    assert complete.result_complete is True
    assert tiny.status == "too_large"
    assert tiny.nodes == []
    assert tiny.total_node_count == 3


@pytest.mark.asyncio
async def test_inspect_pages_reads_all_non_contiguous_pages_and_returns_raw_pages() -> None:
    gateway = PageGateway()
    inspector = PageInspector(
        repository=FocusedRepository(),
        gateway=gateway,
        model="weak",
        context_window=800,
        output_tokens=50,
        safety_margin=30,
        count_tokens=len,
    )

    result = await inspector.inspect(
        request_id="r1", user_id="u", kb_id="kb", group=_group(), doc_id="d1", pages=[6, 2, 4, 2]
    )

    assert result.evaluated_pages == [2, 4, 6]
    assert [chunk.page_number for chunk in result.chunks] == [2, 4, 6]
    assert [chunk.content for chunk in result.chunks] == [
        "第2页 营业额",
        "第4页 营业额",
        "第6页 营业额",
    ]
    assert all(chunk.source_type == "page" for chunk in result.chunks)


@pytest.mark.asyncio
async def test_omitted_sparse_page_selection_is_implicit_reject_without_fallback() -> None:
    inspector = PageInspector(
        repository=FocusedRepository(),
        gateway=PageGateway(omit_last=True),
        model="weak",
        context_window=800,
        output_tokens=50,
        safety_margin=30,
        count_tokens=len,
    )

    result = await inspector.inspect(
        request_id="r1", user_id="u", kb_id="kb", group=_group(), doc_id="d1", pages=[1, 2]
    )

    assert result.evaluated_pages == [1, 2]
    assert result.decision_source == "llm"
    assert {decision.page_number: decision.grade for decision in result.decisions} == {
        1: "accept",
        2: "reject",
    }
    assert [chunk.page_number for chunk in result.chunks] == [1]


@pytest.mark.asyncio
async def test_invented_page_invalidates_only_its_batch_and_uses_rule_fallback() -> None:
    inspector = PageInspector(
        repository=FocusedRepository(),
        gateway=PageGateway(add_unknown=True),
        model="weak",
        context_window=800,
        output_tokens=50,
        safety_margin=30,
        count_tokens=len,
    )

    result = await inspector.inspect(
        request_id="r1", user_id="u", kb_id="kb", group=_group(), doc_id="d1", pages=[1, 2]
    )

    assert result.decision_source == "rule_fallback"
    assert {decision.page_number for decision in result.decisions} == {1, 2}


@pytest.mark.asyncio
async def test_oversized_page_is_windowed_for_judgment_but_returned_in_full() -> None:
    repository = FocusedRepository()
    original = "营业额" + ("很长的原始页面内容" * 100)
    repository.pages[0] = repository.pages[0].model_copy(update={"content": original})
    gateway = PageGateway()
    inspector = PageInspector(
        repository=repository,
        gateway=gateway,
        model="weak",
        context_window=800,
        output_tokens=50,
        safety_margin=30,
        count_tokens=len,
    )

    result = await inspector.inspect(
        request_id="r1", user_id="u", kb_id="kb", group=_group(), doc_id="d1", pages=[1]
    )

    assert len(gateway.requests) > 1
    assert result.chunks[0].content == original


def test_page_batch_contract_does_not_allow_explicit_reject_output() -> None:
    with pytest.raises(ValidationError):
        PageDecisionBatch.model_validate(
            {"decisions": [{"page_number": 1, "grade": "reject"}]}
        )
