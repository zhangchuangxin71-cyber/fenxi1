from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from typing import Protocol

from app.api.schemas import RetrievalWarning
from app.core.models import CandidateChunk, ToolOutput
from app.db.repositories import NodeRecord, PageRecord
from app.workflows.classification.models import QueryGroup
from app.workflows.document_routing.models import DocumentRoute, RoutedDocument
from app.workflows.keyword_ranking import score_text, tokenize_query

_BOILERPLATE_TITLES = {"封面", "目录", "目次", "前言", "序言", "致谢"}


class BroadRepository(Protocol):
    def fetch_pages(
        self, *, user_id: str, kb_id: str, doc_id: str, pages: list[int] | None = None
    ) -> list[PageRecord]: ...

    def fetch_nodes(self, *, user_id: str, kb_id: str, doc_id: str) -> list[NodeRecord]: ...


def _title(value: str) -> str:
    return re.sub(r"[\s:：._\-—–()（）\[\]【】]+", "", (value or "").casefold())


class RuleBroadRetrievalStrategy:
    """Budget-aware, page-only broad coverage. This strategy never owns an LLM."""

    def __init__(
        self,
        *,
        repository: BroadRepository,
        count_tokens: Callable[[str], int],
        minimum_chunk_tokens: int = 64,
    ) -> None:
        self.repository = repository
        self.count_tokens = count_tokens
        self.minimum_chunk_tokens = max(1, int(minimum_chunk_tokens))

    def retrieve(
        self,
        *,
        user_id: str,
        kb_id: str,
        groups: list[QueryGroup],
        routes: list[DocumentRoute],
        remaining_tokens: int,
        session_id: str | None = None,
    ) -> ToolOutput:
        group_by_ref = {
            group.group_ref: group for group in groups if group.category == "routed_broad"
        }
        if remaining_tokens < self.minimum_chunk_tokens:
            refs = sorted(group_by_ref)
            return ToolOutput(
                chunks=[],
                warnings=[
                    RetrievalWarning(
                        code="BROAD_SKIPPED_NO_BUDGET",
                        message=(
                            "broad retrieval was skipped because no page can fit the return budget"
                        ),
                        affected_group_refs=refs,
                    )
                ]
                if refs
                else [],
                coverage_complete=not refs,
            )

        plans: list[deque[CandidateChunk]] = []
        optional_documents: list[tuple[QueryGroup, RoutedDocument]] = []
        degradation_candidate_by_group: dict[str, CandidateChunk] = {}
        total_candidates = 0
        affected_groups: set[str] = set()
        inspected_page_count = 0
        inspected_node_count = 0
        for route in routes:
            group = group_by_ref.get(route.group_ref)
            if group is None:
                continue
            affected_groups.add(group.group_ref)
            primary_documents = route.accept_docs or route.possible_docs
            if route.accept_docs:
                optional_documents.extend((group, routed) for routed in route.possible_docs)
            for routed in primary_documents:
                ordered, page_count, node_count = self._document_plan(
                    user_id=user_id,
                    kb_id=kb_id,
                    group=group,
                    routed=routed,
                    session_id=session_id,
                    document_budget=remaining_tokens,
                )
                inspected_page_count += page_count
                inspected_node_count += node_count
                total_candidates += len(ordered)
                if ordered:
                    degradation_candidate_by_group.setdefault(group.group_ref, ordered[0])
                    plans.append(deque(ordered))

        selected: list[CandidateChunk] = []
        degradation_groups: set[str] = set()
        selected, used = _consume_plans(
            plans=plans,
            selected=selected,
            used=0,
            limit=remaining_tokens,
            cost=self._cost,
        )
        primary_selected_count = len(selected)
        for group, routed in sorted(
            optional_documents,
            key=lambda item: (-item[1].keyword_score, item[1].document.doc_name),
        ):
            if remaining_tokens - used < self.minimum_chunk_tokens:
                break
            ordered, page_count, node_count = self._document_plan(
                user_id=user_id,
                kb_id=kb_id,
                group=group,
                routed=routed,
                session_id=session_id,
                document_budget=remaining_tokens - used,
            )
            inspected_page_count += page_count
            inspected_node_count += node_count
            selected, used = _consume_plans(
                plans=[deque(ordered)],
                selected=selected,
                used=used,
                limit=remaining_tokens,
                cost=self._cost,
            )
        covered_groups = {group_ref for chunk in selected for group_ref in chunk.group_matches}
        for group_ref, candidate in degradation_candidate_by_group.items():
            if group_ref not in covered_groups:
                selected.append(candidate)
                degradation_groups.add(group_ref)
        omitted = total_candidates - primary_selected_count
        warnings: list[RetrievalWarning] = []
        if omitted or degradation_groups:
            warnings.append(
                RetrievalWarning(
                    code="BROAD_TRUNCATED_BY_BUDGET",
                    message=(
                        f"broad retrieval omitted {omitted} page(s) or required final "
                        "continuous truncation because of the return token budget"
                    ),
                    affected_group_refs=sorted(affected_groups),
                )
            )
        return ToolOutput(
            chunks=selected,
            warnings=warnings,
            coverage_complete=(
                not omitted and not degradation_groups and bool(selected or not affected_groups)
            ),
            inspected_node_count=inspected_node_count,
            inspected_page_count=inspected_page_count,
        )

    def _document_plan(
        self,
        *,
        user_id: str,
        kb_id: str,
        group: QueryGroup,
        routed: RoutedDocument,
        session_id: str | None,
        document_budget: int,
    ) -> tuple[list[CandidateChunk], int, int]:
        doc = routed.document
        pages = self.repository.fetch_pages(
            user_id=user_id,
            kb_id=kb_id,
            doc_id=doc.doc_id,
            pages=None,
            session_id=session_id,
        )
        nodes = self.repository.fetch_nodes(
            user_id=user_id,
            kb_id=kb_id,
            doc_id=doc.doc_id,
            session_id=session_id,
        )
        excluded: set[int] = set()
        roots = {node.node_id for node in nodes if node.parent_node_id is None}
        primary_nodes = [node for node in nodes if node.parent_node_id in roots]
        for node in primary_nodes:
            if (
                _title(node.title) in _BOILERPLATE_TITLES
                and node.start_page is not None
                and node.end_page is not None
            ):
                excluded.update(range(node.start_page, node.end_page + 1))
        filtered = [page for page in pages if page.page_number not in excluded]
        if not filtered:
            filtered = pages
        by_page = {page.page_number: page for page in filtered}
        representative_numbers: list[int] = []
        for node in primary_nodes:
            candidate = next(
                (
                    page_number
                    for page_number in range(node.start_page or 0, (node.end_page or -1) + 1)
                    if page_number in by_page
                ),
                None,
            )
            if candidate is not None and candidate not in representative_numbers:
                representative_numbers.append(candidate)
        if not representative_numbers and filtered:
            representative_numbers = _uniform_representatives(
                [page.page_number for page in filtered]
            )
        query = " ".join(group.queries)
        terms = tokenize_query(query)
        estimated_full_cost = sum(
            self.count_tokens(page.content)
            + self.count_tokens(f"document:{page.doc_id}:page:{page.page_number}")
            + 16
            for page in filtered
        )
        if estimated_full_cost <= document_budget:
            ordered = sorted(filtered, key=lambda page: page.page_number)
        else:
            remaining = [
                page for page in filtered if page.page_number not in set(representative_numbers)
            ]
            remaining.sort(
                key=lambda page: (
                    -score_text(query, terms, content=page.content, metadata=doc.doc_name),
                    page.page_number,
                )
            )
            ordered = [
                by_page[number] for number in representative_numbers if number in by_page
            ] + remaining
        chunks = [
            CandidateChunk(
                chunk_id=f"{page.doc_id}:page:{page.page_number}",
                document_id=page.doc_id,
                document_ids=[page.doc_id],
                document_name=page.document_name,
                page_number=page.page_number,
                path=f"document:{page.doc_id}:page:{page.page_number}",
                content=page.content,
                source_type="page",
                category="routed_broad",
                group_matches={group.group_ref: routed.grade},
                questions_by_group={group.group_ref: group.queries},
                rule_score=score_text(query, terms, content=page.content, metadata=doc.doc_name),
                route_score=routed.keyword_score,
                chunk_meta={"broad_coverage_rank": index},
            )
            for index, page in enumerate(ordered)
        ]
        return chunks, len(pages), len(nodes)

    def _cost(self, chunk: CandidateChunk) -> int:
        return self.count_tokens(chunk.content) + self.count_tokens(chunk.path) + 16


def _uniform_representatives(page_numbers: list[int], desired: int = 4) -> list[int]:
    if len(page_numbers) <= desired:
        return list(page_numbers)
    indexes = {round(index * (len(page_numbers) - 1) / (desired - 1)) for index in range(desired)}
    return [page_numbers[index] for index in sorted(indexes)]


def _consume_plans(
    *,
    plans: list[deque[CandidateChunk]],
    selected: list[CandidateChunk],
    used: int,
    limit: int,
    cost: Callable[[CandidateChunk], int],
) -> tuple[list[CandidateChunk], int]:
    pending = [plan for plan in plans if plan]
    while pending:
        progressed = False
        next_plans: list[deque[CandidateChunk]] = []
        for plan in pending:
            chunk = plan[0]
            chunk_cost = cost(chunk)
            if used + chunk_cost <= limit:
                selected.append(plan.popleft())
                used += chunk_cost
                progressed = True
            if plan:
                next_plans.append(plan)
        if not progressed:
            break
        pending = next_plans
    return selected, used
