from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.db.repositories import DocumentProfile, NodeRecord, PageRecord
from app.tools.direct import DirectToolExecutor
from app.workflows.direct_access.models import DirectPlan
from app.workflows.direct_access.planner import RuleDirectPlanner


class FakeRepository:
    def __init__(self) -> None:
        self.pages = [
            PageRecord(
                doc_id="d1", document_name="年报", page_number=index, content=f"第{index}页原文"
            )
            for index in range(1, 6)
        ]
        self.nodes = [
            NodeRecord(
                doc_id="d1",
                node_id="root",
                parent_node_id=None,
                sibling_order=0,
                level=0,
                title="年报",
                summary="",
                start_page=1,
                end_page=5,
                child_count=2,
            ),
            NodeRecord(
                doc_id="d1",
                node_id="n1",
                parent_node_id="root",
                sibling_order=0,
                level=1,
                title="第一章",
                summary="第一章摘要",
                start_page=2,
                end_page=3,
                child_count=0,
            ),
            NodeRecord(
                doc_id="d1",
                node_id="n2",
                parent_node_id="root",
                sibling_order=1,
                level=1,
                title="第二章",
                summary="第二章摘要",
                start_page=4,
                end_page=5,
                child_count=0,
            ),
        ]

    def fetch_pages(
        self, *, doc_id: str, pages: list[int] | None = None, **_: object
    ) -> list[PageRecord]:
        return [page for page in self.pages if pages is None or page.page_number in pages]

    def fetch_nodes(self, **_: object) -> list[NodeRecord]:
        return self.nodes


def _profile() -> DocumentProfile:
    return DocumentProfile(
        doc_id="d1", doc_name="年报", doc_description="简要摘要", page_count=5, node_count=3
    )


def test_page_and_chapter_tools_return_only_exact_page_chunks() -> None:
    executor = DirectToolExecutor(repository=FakeRepository(), user_id="u", kb_id="kb")
    page_output = executor.get_content_of_pages(
        group_ref="g1", questions=["第3页"], pages_by_doc={"d1": [3]}
    )
    chapter_output = executor.get_content_of_chapters(
        group_ref="g2", questions=["第二章"], node_ids_by_doc={"d1": ["n2"]}
    )

    assert [(chunk.page_number, chunk.content) for chunk in page_output.chunks] == [
        (3, "第3页原文")
    ]
    assert page_output.inspected_page_count == 1
    assert [chunk.page_number for chunk in chapter_output.chunks] == [4, 5]
    assert chapter_output.inspected_page_count == 2
    assert chapter_output.inspected_node_count == 3
    assert all(chunk.source_type == "page" for chunk in chapter_output.chunks)


def test_direct_page_tool_reports_explicit_missing_resource() -> None:
    executor = DirectToolExecutor(repository=FakeRepository(), user_id="u", kb_id="kb")

    output = executor.get_content_of_pages(
        group_ref="g1", questions=["第12页"], pages_by_doc={"d1": [12]}
    )

    assert output.chunks == []
    assert output.coverage_complete is False
    assert output.warnings[0].code == "DIRECT_RESOURCE_NOT_FOUND"
    assert output.warnings[0].affected_document_ids == ["d1"]


def test_title_tree_is_strict_json_and_node_ids_are_optional() -> None:
    executor = DirectToolExecutor(repository=FakeRepository(), user_id="u", kb_id="kb")

    output = executor.view_doc_title_tree(
        group_ref="g1",
        questions=["目录"],
        profiles=[_profile()],
        doc_ids=["d1"],
        level=1,
        include_node_id=False,
    )

    assert output.chunk.source_type == "title_tree"
    assert '"title": "第一章"' in output.chunk.content
    assert '"node_id"' not in output.chunk.content


def test_direct_metainfo_content_does_not_expose_document_id() -> None:
    executor = DirectToolExecutor(repository=FakeRepository(), user_id="u", kb_id="kb")

    output = executor.get_docs_metainfo(
        group_ref="g1",
        questions=["年报有几页"],
        profiles=[_profile()],
        doc_ids=["d1"],
        fields=["doc_name", "page_count"],
    )

    assert '"doc_id"' not in output.chunk.content
    assert '"doc_name": "年报"' in output.chunk.content


def test_rule_direct_planner_parses_page_chapter_and_metadata_without_guessing_ids() -> None:
    planner = RuleDirectPlanner()
    documents = [_profile()]

    pages = planner.plan(query="查看年报第3页", documents=documents)
    chapter = planner.plan(query="年报第二章讲什么", documents=documents)
    metainfo = planner.plan(query="年报一共有几页", documents=documents)

    assert pages.calls[0].tool_name == "get_content_of_pages"
    assert pages.calls[0].page_numbers_by_doc == {"d1": [3]}
    assert chapter.calls[0].chapter_numbers_by_doc == {"d1": [2]}
    assert metainfo.calls[0].tool_name == "get_docs_metainfo"


@pytest.mark.parametrize(
    "call",
    [
        {"tool_name": "get_content_of_pages", "page_numbers_by_doc": {}},
        {"tool_name": "get_content_of_pages", "page_numbers_by_doc": {"d1": [0]}},
        {"tool_name": "get_content_of_chapters", "chapter_numbers_by_doc": {}},
        {"tool_name": "get_docs_description", "doc_ids": []},
        {"tool_name": "view_doc_title_tree", "doc_ids": []},
    ],
)
def test_direct_plan_rejects_tool_calls_without_usable_resource_parameters(call: dict) -> None:
    with pytest.raises(ValidationError):
        DirectPlan.model_validate({"calls": [call]})


def test_direct_plan_rejects_empty_call_list() -> None:
    with pytest.raises(ValidationError):
        DirectPlan.model_validate({"calls": []})
