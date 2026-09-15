from __future__ import annotations

import pytest

from app.core.models import CandidateChunk
from app.workflows.classification.models import QueryGroup
from app.workflows.merge.service import merge_candidates


def _candidate(
    *,
    chunk_id: str,
    group_ref: str,
    category: str,
    content: str,
    page: int | None = None,
    grade: str = "accept",
    document_id: str = "doc-1",
    rule_score: float | None = None,
) -> CandidateChunk:
    return CandidateChunk(
        chunk_id=chunk_id,
        document_id=document_id if page else None,
        document_ids=[document_id] if page else [],
        document_name=document_id if page else None,
        page_number=page,
        path=f"page:{page}" if page else "request:scope",
        content=content,
        source_type="page" if page else "scope_metadata",
        category=category,
        group_matches={group_ref: grade},
        questions_by_group={group_ref: [f"question-{group_ref}"]},
        rule_score=rule_score,
    )


def _group(ref: str, category: str) -> QueryGroup:
    return QueryGroup(group_ref=ref, category=category, queries=[f"question-{ref}"], ordinal=1)


def test_merge_deduplicates_same_page_and_unions_group_matches() -> None:
    first = _candidate(
        chunk_id="doc-1:page:2",
        group_ref="g1",
        category="routed_focused",
        content="same",
        page=2,
    )
    second = first.model_copy(
        update={
            "group_matches": {"g2": "possible"},
            "questions_by_group": {"g2": ["question-g2"]},
        }
    )

    result = merge_candidates(
        candidates=[first, second],
        groups=[_group("g1", "routed_focused"), _group("g2", "routed_broad")],
        top_k=5,
        max_return_tokens=100,
        count_tokens=lambda _: 1,
    )

    assert len(result.chunks) == 1
    assert result.chunks[0].page_number == 2
    assert result.internal_chunks[0].group_matches == {"g1": "accept", "g2": "possible"}
    assert result.coverage.covered_group_refs == ["g1", "g2"]


def test_merge_deduplicates_identical_scope_content_and_combines_distinct_hints() -> None:
    first = _candidate(
        chunk_id="scope:g1:metainfo",
        group_ref="g1",
        category="scope_direct",
        content="相同的范围元信息",
    ).model_copy(update={"hint": "用于回答：能查到哪些文档？"})
    second = _candidate(
        chunk_id="scope:g2:metainfo",
        group_ref="g2",
        category="scope_direct",
        content="相同的范围元信息",
    ).model_copy(update={"hint": "用于回答：能看到哪些信息？"})

    result = merge_candidates(
        candidates=[first, second],
        groups=[_group("g1", "scope_direct"), _group("g2", "scope_direct")],
        top_k=5,
        max_return_tokens=100,
        count_tokens=lambda _: 1,
    )

    assert len(result.chunks) == 1
    assert result.chunks[0].content == "相同的范围元信息"
    assert result.chunks[0].hint == ("用于回答：能查到哪些文档？\n用于回答：能看到哪些信息？")
    assert result.internal_chunks[0].group_matches == {"g1": "accept", "g2": "accept"}
    assert result.coverage.covered_group_refs == ["g1", "g2"]


def test_merge_deduplicates_identical_content_across_page_locations() -> None:
    first = _candidate(
        chunk_id="doc-1:page:1",
        group_ref="g1",
        category="routed_focused",
        content="完全相同的页内容",
        page=1,
        document_id="doc-1",
    )
    second = _candidate(
        chunk_id="doc-2:page:8",
        group_ref="g2",
        category="routed_focused",
        content="完全相同的页内容",
        page=8,
        document_id="doc-2",
    )

    result = merge_candidates(
        candidates=[first, second],
        groups=[_group("g1", "routed_focused"), _group("g2", "routed_focused")],
        top_k=5,
        max_return_tokens=100,
        count_tokens=lambda _: 1,
    )

    assert len(result.chunks) == 1
    assert result.chunks[0].document_ids == ["doc-1", "doc-2"]
    assert result.internal_chunks[0].group_matches == {"g1": "accept", "g2": "accept"}


def test_document_coverage_accepts_multi_document_metadata_chunk() -> None:
    chunk = _candidate(
        chunk_id="direct:g1:metainfo",
        group_ref="g1",
        category="routed_direct",
        content="metadata",
    ).model_copy(update={"document_ids": ["doc-1", "doc-2"]})

    result = merge_candidates(
        candidates=[chunk],
        groups=[_group("g1", "routed_direct")],
        top_k=1,
        max_return_tokens=100,
        count_tokens=lambda _: 1,
        ensure_document_coverage=True,
        target_document_ids_by_group={"g1": ["doc-1", "doc-2"]},
    )

    assert result.coverage.complete is True
    assert not any(warning.code == "DOCUMENT_COVERAGE_INCOMPLETE" for warning in result.warnings)


def test_merge_rejects_inconsistent_sources_for_same_canonical_page() -> None:
    first = _candidate(
        chunk_id="doc-1:page:2",
        group_ref="g1",
        category="routed_focused",
        content="original page content",
        page=2,
    )
    inconsistent = first.model_copy(update={"content": "different page content"})

    with pytest.raises(ValueError, match="inconsistent canonical chunk source"):
        merge_candidates(
            candidates=[first, inconsistent],
            groups=[_group("g1", "routed_focused")],
            top_k=5,
            max_return_tokens=100,
            count_tokens=lambda _: 1,
        )


def test_merge_prefers_scope_direct_and_focused_before_broad() -> None:
    candidates = [
        _candidate(chunk_id="broad", group_ref="g3", category="routed_broad", content="b"),
        _candidate(chunk_id="focused", group_ref="g2", category="routed_focused", content="f"),
        _candidate(chunk_id="scope", group_ref="g1", category="scope_direct", content="s"),
    ]

    result = merge_candidates(
        candidates=candidates,
        groups=[
            _group("g1", "scope_direct"),
            _group("g2", "routed_focused"),
            _group("g3", "routed_broad"),
        ],
        top_k=2,
        max_return_tokens=6,
        count_tokens=lambda _: 1,
    )

    assert [chunk.chunk_id for chunk in result.chunks] == ["scope", "focused"]
    assert result.coverage.uncovered_group_refs == ["g3"]
    assert any(warning.code == "RESULT_TOKEN_BUDGET_EXCEEDED" for warning in result.warnings)


def test_merge_category_priority_wins_over_cross_category_scarcity() -> None:
    candidates = [
        _candidate(
            chunk_id="scope-primary",
            group_ref="scope",
            category="scope_direct",
            content="scope",
        ),
        _candidate(
            chunk_id="scope-secondary",
            group_ref="scope",
            category="scope_direct",
            content="scope-2",
        ),
        _candidate(
            chunk_id="focused-only",
            group_ref="focused",
            category="routed_focused",
            content="focused",
            page=1,
        ),
    ]

    result = merge_candidates(
        candidates=candidates,
        groups=[_group("scope", "scope_direct"), _group("focused", "routed_focused")],
        top_k=1,
        max_return_tokens=3,
        count_tokens=lambda _: 1,
    )

    assert [chunk.chunk_id for chunk in result.chunks] == ["scope-primary"]
    assert result.coverage.uncovered_group_refs == ["focused"]


def test_merge_reports_unavailable_evidence_as_workflow_failure_not_token_budget() -> None:
    result = merge_candidates(
        candidates=[],
        groups=[_group("g1", "routed_direct")],
        top_k=1,
        max_return_tokens=100,
        count_tokens=lambda _: 1,
    )

    assert result.coverage.complete is False
    assert result.coverage.truncated_by == "workflow_degradation"
    assert any(warning.code == "GROUP_EVIDENCE_UNAVAILABLE" for warning in result.warnings)
    assert not any(warning.code == "RESULT_TOKEN_BUDGET_EXCEEDED" for warning in result.warnings)


def test_merge_shared_chunk_covers_scarce_group_before_large_group_unique_chunks() -> None:
    shared = _candidate(
        chunk_id="shared", group_ref="large", category="routed_focused", content="shared", page=1
    ).model_copy(
        update={
            "group_matches": {"large": "accept", "small": "accept"},
            "questions_by_group": {"large": ["large"], "small": ["small"]},
        }
    )
    unique = [
        _candidate(
            chunk_id=f"unique-{page}",
            group_ref="large",
            category="routed_focused",
            content="unique",
            page=page,
        )
        for page in (2, 3)
    ]

    result = merge_candidates(
        candidates=[*unique, shared],
        groups=[_group("large", "routed_focused"), _group("small", "routed_focused")],
        top_k=1,
        max_return_tokens=3,
        count_tokens=lambda _: 1,
    )

    assert [chunk.chunk_id for chunk in result.chunks] == ["shared"]
    assert result.coverage.covered_group_refs == ["large", "small"]


def test_shared_focused_chunk_with_one_accept_grade_is_not_demoted_to_possible() -> None:
    shared = _candidate(
        chunk_id="shared",
        group_ref="g1",
        category="routed_focused",
        content="shared",
        page=1,
        rule_score=100,
    ).model_copy(
        update={
            "group_matches": {"g1": "accept", "g2": "possible"},
            "questions_by_group": {"g1": ["q1"], "g2": ["q2"]},
        }
    )
    possible = _candidate(
        chunk_id="possible",
        group_ref="g2",
        category="routed_focused",
        content="possible",
        page=2,
        grade="possible",
    )

    result = merge_candidates(
        candidates=[shared, possible],
        groups=[_group("g1", "routed_focused"), _group("g2", "routed_focused")],
        top_k=2,
        max_return_tokens=6,
        count_tokens=lambda _: 1,
    )

    assert [chunk.chunk_id for chunk in result.chunks] == ["shared", "possible"]


def test_focused_possible_soft_target_counts_only_newly_added_chunks() -> None:
    candidates = [
        _candidate(
            chunk_id=f"possible-{page}",
            group_ref="g1",
            category="routed_focused",
            content=f"possible-{page}",
            page=page,
            grade="possible",
        )
        for page in (1, 2)
    ]

    result = merge_candidates(
        candidates=candidates,
        groups=[_group("g1", "routed_focused")],
        top_k=2,
        max_return_tokens=6,
        count_tokens=lambda _: 1,
    )

    assert [chunk.chunk_id for chunk in result.chunks] == ["possible-1", "possible-2"]


def test_merge_direct_atomic_results_can_exceed_top_k() -> None:
    candidates = [
        _candidate(
            chunk_id=f"direct-{page}",
            group_ref="g1",
            category="routed_direct",
            content=f"page-{page}",
            page=page,
        )
        for page in (1, 2, 3)
    ]

    result = merge_candidates(
        candidates=candidates,
        groups=[_group("g1", "routed_direct")],
        top_k=1,
        max_return_tokens=100,
        count_tokens=lambda _: 1,
    )

    assert len(result.chunks) == 3


def test_merge_extreme_budget_returns_continuous_truncated_original_with_warning() -> None:
    candidate = _candidate(
        chunk_id="huge",
        group_ref="g1",
        category="routed_focused",
        content="0123456789" * 30,
        page=1,
    )

    result = merge_candidates(
        candidates=[candidate],
        groups=[_group("g1", "routed_focused")],
        top_k=1,
        max_return_tokens=100,
        count_tokens=len,
    )

    assert len(result.chunks) == 1
    assert result.chunks[0].content in candidate.content
    assert result.chunks[0].content != candidate.content
    assert result.chunks[0].chunk_meta["content_truncated"] is True
    assert any(warning.code == "ATOMIC_CHUNK_CONTENT_TRUNCATED" for warning in result.warnings)


def test_focused_truncation_prefers_continuous_window_around_query_hit() -> None:
    candidate = _candidate(
        chunk_id="hit",
        group_ref="g1",
        category="routed_focused",
        content=("无关" * 100) + "营业额" + ("后续" * 100),
        page=1,
    ).model_copy(update={"questions_by_group": {"g1": ["营业额"]}})

    result = merge_candidates(
        candidates=[candidate],
        groups=[
            QueryGroup(group_ref="g1", category="routed_focused", queries=["营业额"], ordinal=1)
        ],
        top_k=1,
        max_return_tokens=110,
        count_tokens=len,
    )

    assert "营业额" in result.chunks[0].content
    assert result.chunks[0].content in candidate.content


def test_ensure_document_coverage_reserves_one_focused_chunk_per_accepted_document() -> None:
    candidates = [
        _candidate(
            chunk_id="d1-p1",
            group_ref="g1",
            category="routed_focused",
            content="d1-p1",
            page=1,
            document_id="d1",
            rule_score=100,
        ),
        _candidate(
            chunk_id="d1-p2",
            group_ref="g1",
            category="routed_focused",
            content="d1-p2",
            page=2,
            document_id="d1",
            rule_score=90,
        ),
        _candidate(
            chunk_id="d2-p1",
            group_ref="g1",
            category="routed_focused",
            content="d2-p1",
            page=1,
            document_id="d2",
            rule_score=1,
        ),
    ]

    result = merge_candidates(
        candidates=candidates,
        groups=[_group("g1", "routed_focused")],
        top_k=1,
        max_return_tokens=6,
        count_tokens=lambda _: 1,
        ensure_document_coverage=True,
        target_document_ids_by_group={"g1": ["d1", "d2"]},
    )

    assert {chunk.document_id for chunk in result.chunks} == {"d1", "d2"}
    assert result.returned_tokens <= 6
    assert not any(warning.code == "DOCUMENT_COVERAGE_INCOMPLETE" for warning in result.warnings)


def test_ensure_document_coverage_warns_when_hard_budget_cannot_cover_all_documents() -> None:
    candidates = [
        _candidate(
            chunk_id=f"{document_id}-p1",
            group_ref="g1",
            category="routed_focused",
            content=document_id,
            page=1,
            document_id=document_id,
        )
        for document_id in ("d1", "d2")
    ]

    result = merge_candidates(
        candidates=candidates,
        groups=[_group("g1", "routed_focused")],
        top_k=1,
        max_return_tokens=3,
        count_tokens=lambda _: 1,
        ensure_document_coverage=True,
        target_document_ids_by_group={"g1": ["d1", "d2"]},
    )

    warning = next(
        warning for warning in result.warnings if warning.code == "DOCUMENT_COVERAGE_INCOMPLETE"
    )
    assert len(warning.affected_document_ids) == 1
    assert warning.affected_group_refs == ["g1"]


def test_broad_merge_round_robins_documents_before_second_page_of_same_document() -> None:
    candidates = [
        _candidate(
            chunk_id=f"{document_id}-p{page}",
            group_ref="g1",
            category="routed_broad",
            content=f"{document_id}-{page}",
            page=page,
            document_id=document_id,
        ).model_copy(update={"chunk_meta": {"broad_coverage_rank": page}})
        for document_id in ("d1", "d2")
        for page in (1, 2)
    ]

    result = merge_candidates(
        candidates=candidates,
        groups=[_group("g1", "routed_broad")],
        top_k=1,
        max_return_tokens=6,
        count_tokens=lambda _: 1,
    )

    assert [chunk.document_id for chunk in result.chunks] == ["d1", "d2"]
