from __future__ import annotations

import json

import pytest

from app.db.repositories import DocumentProfile, NodeRecord, PageRecord
from app.llm.gateway import LLMRequest, LLMResult, LLMToolCall, LLMUsage
from app.observability.trace import TraceCollector
from app.tools.node_tree import NodeTreeScanner
from app.workflows.classification.models import QueryGroup
from app.workflows.document_routing.models import RoutedDocument
from app.workflows.focused_search.page_inspector import PageInspector
from app.workflows.focused_search.service import FocusedSearchService


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


class FocusedGateway:
    def __init__(self, *, fail_tree: bool = False) -> None:
        self.fail_tree = fail_tree
        self.requests: list[LLMRequest] = []
        self.tree_call_count = 0

    async def complete_json(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if request.phase.startswith("tree_navigation"):
            if self.fail_tree:
                raise RuntimeError("tree model failed")
            self.tree_call_count += 1
            if self.tree_call_count == 1:
                return LLMResult(
                    data={},
                    usage=LLMUsage(total_tokens=5),
                    tool_calls=(
                        LLMToolCall(
                            call_id="scan-1",
                            name="scan_node_tree",
                            arguments={"root_node_id": "n2", "level": 1},
                        ),
                    ),
                )
            return LLMResult(
                data={},
                usage=LLMUsage(total_tokens=5),
                tool_calls=(
                    LLMToolCall(
                        call_id="finish-1",
                        name="finish_tree_navigation",
                        arguments={"decision": "finished"},
                    ),
                ),
            )
        payload = json.loads(request.messages[-1]["content"])
        return LLMResult(
            data={
                "decisions": [
                    {"page_number": page["page_number"], "grade": "accept"}
                    for page in payload["pages"]
                ]
            },
            usage=LLMUsage(total_tokens=5),
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


def _routed() -> RoutedDocument:
    return RoutedDocument(
        document=DocumentProfile(doc_id="d1", doc_name="测试文档", page_count=6, node_count=4),
        grade="accept",
        keyword_score=0.8,
        decision_source="llm",
    )


def _service(gateway: FocusedGateway) -> FocusedSearchService:
    repository = FocusedRepository()
    scanner = NodeTreeScanner(
        repository=repository, user_id="u", kb_id="kb", count_tokens=len, max_result_tokens=1000
    )
    inspector = PageInspector(
        repository=repository,
        gateway=gateway,
        model="weak",
        context_window=800,
        output_tokens=80,
        safety_margin=30,
        count_tokens=len,
    )
    return FocusedSearchService(
        repository=repository,
        scanner=scanner,
        page_inspector=inspector,
        gateway=gateway,
        model="weak",
        max_tree_steps=3,
        tree_output_tokens=80,
    )


@pytest.mark.asyncio
async def test_focused_search_uses_initial_level_one_nodes_then_returns_selected_raw_pages() -> (
    None
):
    gateway = FocusedGateway()

    result = await _service(gateway).search(
        request_id="r1", user_id="u", kb_id="kb", group=_group(), routed_document=_routed()
    )

    tree_request = next(
        request for request in gateway.requests if request.phase.startswith("tree_navigation")
    )
    assert all(node_id in str(tree_request.messages) for node_id in ("n1", "n2", "n3"))
    assert tree_request.response_format is None
    assert all(tool["function"]["strict"] is True for tool in tree_request.tools)
    second_tree_request = [
        request for request in gateway.requests if request.phase.startswith("tree_navigation")
    ][1]
    assert any(message["role"] == "tool" for message in second_tree_request.messages)
    assert all("page_numbers" not in str(tool) for tool in tree_request.tools)
    assert [chunk.page_number for chunk in result.chunks] == [3, 4]
    assert all(chunk.source_type == "page" for chunk in result.chunks)


@pytest.mark.asyncio
async def test_tree_model_failure_falls_back_to_full_document_page_inspection() -> None:
    trace = TraceCollector(enabled=True, request_id="r1")
    result = await _service(FocusedGateway(fail_tree=True)).search(
        request_id="r1",
        user_id="u",
        kb_id="kb",
        group=_group(),
        routed_document=_routed(),
        trace_collector=trace,
    )

    assert [chunk.page_number for chunk in result.chunks] == [1, 2, 3, 4, 5, 6]
    assert any(warning.code == "TREE_NAVIGATION_RULE_FALLBACK" for warning in result.warnings)
    assert any(
        event["kind"] == "fallback"
        and event["payload"]["error_message"] == "tree model failed"
        for event in trace.details
    )
