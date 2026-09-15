from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.core.models import CandidateChunk, ToolOutput
from app.db.repositories import PageRecord
from app.workflows.classification.models import QueryGroup
from app.workflows.document_routing.models import DocumentRoute, RoutedDocument
from app.workflows.keyword_ranking import score_text, tokenize_query


class PossiblePageRepository(Protocol):
    def fetch_pages(
        self, *, user_id: str, kb_id: str, doc_id: str, pages: list[int] | None = None
    ) -> list[PageRecord]: ...


class OptionalFocusedPossibleService:
    """Post-checkpoint lexical expansion. It intentionally has no LLM dependency."""

    def __init__(
        self,
        *,
        repository: PossiblePageRepository,
        count_tokens: Callable[[str], int],
        minimum_page_tokens: int = 64,
        possible_score: float = 1.0,
    ) -> None:
        self.repository = repository
        self.count_tokens = count_tokens
        self.minimum_page_tokens = max(1, int(minimum_page_tokens))
        self.possible_score = possible_score

    def retrieve(
        self,
        *,
        user_id: str,
        kb_id: str,
        groups: list[QueryGroup],
        routes: list[DocumentRoute],
        existing_candidates: list[CandidateChunk],
        top_k: int,
        remaining_tokens: int,
        session_id: str | None = None,
    ) -> ToolOutput:
        focused = {group.group_ref: group for group in groups if group.category == "routed_focused"}
        covered = {
            group_ref
            for chunk in existing_candidates
            for group_ref in chunk.group_matches
            if group_ref in focused
        }
        high_priority_count = len(
            {
                (chunk.source_type, chunk.document_id, chunk.page_number, chunk.chunk_id)
                for chunk in existing_candidates
                if chunk.category != "routed_broad"
            }
        )
        soft_gap = max(0, int(top_k) - high_priority_count)
        uncovered = set(focused) - covered
        if not uncovered and soft_gap == 0:
            return ToolOutput(chunks=[])
        if remaining_tokens < self.minimum_page_tokens:
            return ToolOutput(chunks=[], coverage_complete=not uncovered)

        work: list[tuple[int, float, str, QueryGroup, RoutedDocument]] = []
        for route in routes:
            group = focused.get(route.group_ref)
            if group is None:
                continue
            for routed in route.possible_docs:
                work.append(
                    (
                        0 if group.group_ref in uncovered else 1,
                        -routed.keyword_score,
                        routed.document.doc_id,
                        group,
                        routed,
                    )
                )
        work.sort(key=lambda item: item[:3])
        chunks: list[CandidateChunk] = []
        used = 0
        inspected_page_count = 0
        for _, _, _, group, routed in work:
            if not uncovered and len(chunks) >= soft_gap:
                break
            pages = self.repository.fetch_pages(
                user_id=user_id,
                kb_id=kb_id,
                doc_id=routed.document.doc_id,
                pages=None,
                session_id=session_id,
            )
            inspected_page_count += len(pages)
            if not pages:
                continue
            page_costs = [self.count_tokens(page.content) + 16 for page in pages]
            if remaining_tokens - used < min(page_costs or [self.minimum_page_tokens]):
                continue
            query = " ".join(group.queries)
            terms = tokenize_query(query)
            ranked = sorted(
                (
                    (
                        score_text(
                            query,
                            terms,
                            content=page.content,
                            metadata=routed.document.doc_name,
                        ),
                        page,
                    )
                    for page in pages
                ),
                key=lambda item: (-item[0], item[1].page_number),
            )
            for score, page in ranked:
                if score < self.possible_score:
                    continue
                cost = self.count_tokens(page.content) + self.count_tokens(page.document_name) + 16
                if used + cost > remaining_tokens:
                    continue
                chunks.append(
                    CandidateChunk(
                        chunk_id=f"{page.doc_id}:page:{page.page_number}",
                        document_id=page.doc_id,
                        document_ids=[page.doc_id],
                        document_name=page.document_name,
                        page_number=page.page_number,
                        path=f"document:{page.doc_id}:page:{page.page_number}",
                        content=page.content,
                        source_type="page",
                        category="routed_focused",
                        group_matches={group.group_ref: "possible"},
                        questions_by_group={group.group_ref: group.queries},
                        rule_score=score,
                        route_score=routed.keyword_score,
                    )
                )
                used += cost
                covered.add(group.group_ref)
                uncovered.discard(group.group_ref)
                if not uncovered and len(chunks) >= soft_gap:
                    break
        return ToolOutput(
            chunks=chunks,
            coverage_complete=not uncovered,
            inspected_page_count=inspected_page_count,
        )
