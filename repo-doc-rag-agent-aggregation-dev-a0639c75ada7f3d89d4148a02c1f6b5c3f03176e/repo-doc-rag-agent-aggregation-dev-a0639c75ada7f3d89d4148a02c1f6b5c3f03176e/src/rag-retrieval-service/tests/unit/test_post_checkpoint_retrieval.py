from __future__ import annotations

from app.db.repositories import DocumentProfile, NodeRecord, PageRecord
from app.workflows.broad_retrieval.service import RuleBroadRetrievalStrategy
from app.workflows.classification.models import QueryGroup
from app.workflows.document_routing.models import DocumentRoute, RoutedDocument
from app.workflows.focused_search.optional_possible import OptionalFocusedPossibleService


class PostCheckpointRepository:
    def __init__(self) -> None:
        self.pages = [
            PageRecord(doc_id="d1", document_name="年报", page_number=1, content="目录内容"),
            PageRecord(doc_id="d1", document_name="年报", page_number=2, content="公司概况"),
            PageRecord(doc_id="d1", document_name="年报", page_number=3, content="经营情况"),
            PageRecord(doc_id="d1", document_name="年报", page_number=4, content="营业额为100亿元"),
        ]
        self.nodes = [
            NodeRecord(
                doc_id="d1",
                node_id="root",
                parent_node_id=None,
                sibling_order=0,
                level=1,
                title="年报",
                summary="",
                start_page=1,
                end_page=4,
                child_count=3,
            ),
            NodeRecord(
                doc_id="d1",
                node_id="toc",
                parent_node_id="root",
                sibling_order=0,
                level=2,
                title=" 目 录 ",
                summary="",
                start_page=1,
                end_page=1,
                child_count=0,
            ),
            NodeRecord(
                doc_id="d1",
                node_id="c1",
                parent_node_id="root",
                sibling_order=1,
                level=2,
                title="公司概况",
                summary="",
                start_page=2,
                end_page=2,
                child_count=0,
            ),
            NodeRecord(
                doc_id="d1",
                node_id="c2",
                parent_node_id="root",
                sibling_order=2,
                level=2,
                title="经营数据",
                summary="",
                start_page=3,
                end_page=4,
                child_count=0,
            ),
        ]

    def fetch_pages(self, **_: object) -> list[PageRecord]:
        return self.pages

    def fetch_nodes(self, **_: object) -> list[NodeRecord]:
        return self.nodes


def _group(category: str) -> QueryGroup:
    return QueryGroup(
        group_ref="g1",
        category=category,
        queries=["营业额是多少"],
        target_docs_description="年报",
        target_docs_keywords=["年报"],
        ordinal=1,
    )


def _route(grade: str) -> DocumentRoute:
    routed = RoutedDocument(
        document=DocumentProfile(doc_id="d1", doc_name="年报", page_count=4, node_count=4),
        grade=grade,
        keyword_score=0.8,
        decision_source="llm",
    )
    return DocumentRoute(
        group_ref="g1",
        accept_docs=[routed] if grade == "accept" else [],
        possible_docs=[routed] if grade == "possible" else [],
        rejected_document_count=0,
        prefilter_candidate_count=1,
        prefilter_rejected_count=0,
    )


def test_rule_broad_filters_exact_boilerplate_and_returns_near_full_document() -> None:
    strategy = RuleBroadRetrievalStrategy(
        repository=PostCheckpointRepository(), count_tokens=len, minimum_chunk_tokens=1
    )

    result = strategy.retrieve(
        user_id="u",
        kb_id="kb",
        groups=[_group("routed_broad")],
        routes=[_route("accept")],
        remaining_tokens=1000,
    )

    assert [chunk.page_number for chunk in result.chunks] == [2, 3, 4]
    assert all(chunk.content != "目录内容" for chunk in result.chunks)
    assert result.inspected_page_count == 4
    assert result.inspected_node_count == 4


def test_rule_broad_preserves_page_order_when_filtered_document_fits_budget() -> None:
    repository = PostCheckpointRepository()
    repository.nodes[-1] = repository.nodes[-1].model_copy(update={"start_page": 4, "end_page": 4})
    strategy = RuleBroadRetrievalStrategy(
        repository=repository, count_tokens=len, minimum_chunk_tokens=1
    )

    result = strategy.retrieve(
        user_id="u",
        kb_id="kb",
        groups=[_group("routed_broad")],
        routes=[_route("accept")],
        remaining_tokens=1000,
    )

    assert [chunk.page_number for chunk in result.chunks] == [2, 3, 4]


def test_rule_broad_keeps_one_degradation_candidate_when_no_complete_page_fits() -> None:
    repository = PostCheckpointRepository()
    repository.pages = [
        PageRecord(
            doc_id="d1",
            document_name="年报",
            page_number=1,
            content="需要连续截取的正文" * 100,
        )
    ]
    repository.nodes = []
    strategy = RuleBroadRetrievalStrategy(
        repository=repository, count_tokens=len, minimum_chunk_tokens=1
    )

    result = strategy.retrieve(
        user_id="u",
        kb_id="kb",
        groups=[_group("routed_broad")],
        routes=[_route("accept")],
        remaining_tokens=100,
    )

    assert [chunk.page_number for chunk in result.chunks] == [1]
    assert result.warnings[0].code == "BROAD_TRUNCATED_BY_BUDGET"


def test_rule_broad_covers_each_document_before_adding_second_page() -> None:
    class TwoDocumentRepository:
        def fetch_pages(self, *, doc_id: str, **_: object) -> list[PageRecord]:
            return [
                PageRecord(
                    doc_id=doc_id,
                    document_name=f"年报{doc_id}",
                    page_number=page_number,
                    content=f"{doc_id}正文{page_number}",
                )
                for page_number in (1, 2)
            ]

        def fetch_nodes(self, **_: object) -> list[NodeRecord]:
            return []

    routed = [
        RoutedDocument(
            document=DocumentProfile(doc_id=doc_id, doc_name=f"年报{doc_id}", page_count=2),
            grade="accept",
            keyword_score=0.8,
            decision_source="llm",
        )
        for doc_id in ("d1", "d2")
    ]
    route = DocumentRoute(
        group_ref="g1",
        accept_docs=routed,
        possible_docs=[],
        rejected_document_count=0,
        prefilter_candidate_count=2,
        prefilter_rejected_count=0,
    )
    strategy = RuleBroadRetrievalStrategy(
        repository=TwoDocumentRepository(), count_tokens=len, minimum_chunk_tokens=1
    )

    result = strategy.retrieve(
        user_id="u",
        kb_id="kb",
        groups=[_group("routed_broad")],
        routes=[route],
        remaining_tokens=100,
    )

    assert [chunk.document_id for chunk in result.chunks[:2]] == ["d1", "d2"]
    assert result.inspected_page_count == 4


def test_rule_broad_uses_possible_documents_when_no_accept_document_exists() -> None:
    strategy = RuleBroadRetrievalStrategy(
        repository=PostCheckpointRepository(), count_tokens=len, minimum_chunk_tokens=1
    )

    result = strategy.retrieve(
        user_id="u",
        kb_id="kb",
        groups=[_group("routed_broad")],
        routes=[_route("possible")],
        remaining_tokens=1000,
    )

    assert [chunk.page_number for chunk in result.chunks] == [2, 3, 4]
    assert all(chunk.group_matches == {"g1": "possible"} for chunk in result.chunks)


def test_rule_broad_adds_possible_document_only_from_remaining_budget() -> None:
    class TwoDocumentRepository(PostCheckpointRepository):
        def fetch_pages(self, *, doc_id: str, **_: object) -> list[PageRecord]:
            return [
                PageRecord(
                    doc_id=doc_id,
                    document_name=f"年报{doc_id}",
                    page_number=1,
                    content=f"{doc_id}正文",
                )
            ]

        def fetch_nodes(self, **_: object) -> list[NodeRecord]:
            return []

    accept = RoutedDocument(
        document=DocumentProfile(doc_id="d1", doc_name="年报d1", page_count=1),
        grade="accept",
        keyword_score=0.9,
        decision_source="llm",
    )
    possible = RoutedDocument(
        document=DocumentProfile(doc_id="d2", doc_name="年报d2", page_count=1),
        grade="possible",
        keyword_score=0.8,
        decision_source="llm",
    )
    route = DocumentRoute(
        group_ref="g1",
        accept_docs=[accept],
        possible_docs=[possible],
        rejected_document_count=0,
        prefilter_candidate_count=2,
        prefilter_rejected_count=0,
    )
    strategy = RuleBroadRetrievalStrategy(
        repository=TwoDocumentRepository(), count_tokens=len, minimum_chunk_tokens=1
    )

    result = strategy.retrieve(
        user_id="u",
        kb_id="kb",
        groups=[_group("routed_broad")],
        routes=[route],
        remaining_tokens=100,
    )

    assert [chunk.document_id for chunk in result.chunks] == ["d1", "d2"]
    assert result.chunks[0].group_matches == {"g1": "accept"}
    assert result.chunks[1].group_matches == {"g1": "possible"}


def test_optional_possible_scores_every_page_and_can_select_last_page() -> None:
    service = OptionalFocusedPossibleService(
        repository=PostCheckpointRepository(), count_tokens=len, minimum_page_tokens=1
    )

    result = service.retrieve(
        user_id="u",
        kb_id="kb",
        groups=[_group("routed_focused")],
        routes=[_route("possible")],
        existing_candidates=[],
        top_k=1,
        remaining_tokens=100,
    )

    assert result.chunks[0].page_number == 4
    assert result.chunks[0].rule_score > 0
    assert result.inspected_page_count == 4


def test_optional_possible_skips_oversized_document_and_checks_later_candidates() -> None:
    class UnevenRepository:
        def fetch_pages(self, *, doc_id: str, **_: object) -> list[PageRecord]:
            content = "无关正文" * 300 if doc_id == "d1" else "营业额为10亿元"
            return [
                PageRecord(
                    doc_id=doc_id,
                    document_name=f"年报{doc_id}",
                    page_number=1,
                    content=content,
                )
            ]

    possible_docs = [
        RoutedDocument(
            document=DocumentProfile(doc_id=doc_id, doc_name=f"年报{doc_id}", page_count=1),
            grade="possible",
            keyword_score=score,
            decision_source="llm",
        )
        for doc_id, score in (("d1", 0.9), ("d2", 0.8))
    ]
    route = DocumentRoute(
        group_ref="g1",
        accept_docs=[],
        possible_docs=possible_docs,
        rejected_document_count=0,
        prefilter_candidate_count=2,
        prefilter_rejected_count=0,
    )
    service = OptionalFocusedPossibleService(
        repository=UnevenRepository(), count_tokens=len, minimum_page_tokens=1
    )

    result = service.retrieve(
        user_id="u",
        kb_id="kb",
        groups=[_group("routed_focused")],
        routes=[route],
        existing_candidates=[],
        top_k=1,
        remaining_tokens=80,
    )

    assert [chunk.document_id for chunk in result.chunks] == ["d2"]
    assert result.inspected_page_count == 2
