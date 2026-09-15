from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.api.schemas import CoverageSummary, RetrievalWarning, RetrievedChunk
from app.core.models import CandidateChunk
from app.workflows.classification.models import QueryGroup
from app.workflows.keyword_ranking import tokenize_query

_PRIORITY = {
    "scope_direct": 0,
    "routed_direct": 1,
    "routed_focused": 2,
    "routed_broad": 3,
}
_GRADE = {"accept": 0, "possible": 1}


@dataclass(frozen=True, slots=True)
class MergeResult:
    chunks: list[RetrievedChunk]
    internal_chunks: list[CandidateChunk]
    warnings: list[RetrievalWarning]
    coverage: CoverageSummary
    returned_tokens: int


def _canonical_key(chunk: CandidateChunk) -> tuple[object, ...]:
    return ("content", chunk.content)


def _representative_key(chunk: CandidateChunk) -> tuple[object, ...]:
    return (
        _PRIORITY[chunk.category],
        chunk.document_id or "",
        chunk.page_number or 0,
        chunk.path,
        chunk.chunk_id,
    )


def _page_identity(chunk: CandidateChunk) -> tuple[str, int] | None:
    if chunk.source_type != "page" or chunk.document_id is None or chunk.page_number is None:
        return None
    return (chunk.document_id, chunk.page_number)


def _merge_duplicate(existing: CandidateChunk, incoming: CandidateChunk) -> CandidateChunk:
    if existing.content != incoming.content:
        raise ValueError("duplicate chunks must have identical content")
    representative, other = sorted((existing, incoming), key=_representative_key)
    matches = dict(existing.group_matches)
    for group_ref, grade in incoming.group_matches.items():
        if group_ref not in matches or _GRADE[grade] < _GRADE[matches[group_ref]]:
            matches[group_ref] = grade
    questions = {key: list(value) for key, value in existing.questions_by_group.items()}
    for group_ref, values in incoming.questions_by_group.items():
        questions[group_ref] = list(dict.fromkeys([*questions.get(group_ref, []), *values]))
    category = min((existing.category, incoming.category), key=lambda value: _PRIORITY[value])
    document_ids = list(
        dict.fromkeys(
            [
                *representative.document_ids,
                *([representative.document_id] if representative.document_id else []),
                *other.document_ids,
                *([other.document_id] if other.document_id else []),
            ]
        )
    )
    hints = list(
        dict.fromkeys(hint for hint in (representative.hint.strip(), other.hint.strip()) if hint)
    )
    return representative.model_copy(
        update={
            "category": category,
            "document_ids": document_ids,
            "hint": "\n".join(hints),
            "group_matches": matches,
            "questions_by_group": questions,
            "rule_score": max(existing.rule_score or 0.0, incoming.rule_score or 0.0),
            "route_score": max(existing.route_score or 0.0, incoming.route_score or 0.0),
        }
    )


def _render_hint(chunk: CandidateChunk) -> str:
    hint = chunk.hint
    if not hint:
        questions = [
            question
            for group_ref in chunk.group_matches
            for question in chunk.questions_by_group.get(group_ref, [])
        ]
        source = f"来源：《{chunk.document_name}》"
        if chunk.page_number is not None:
            source += f"第 {chunk.page_number} 页"
        hint = f"本 chunk 可能用于回答：{'；'.join(dict.fromkeys(questions))}。{source}"
    if chunk.chunk_meta.get("content_truncated"):
        hint += " content_truncated=true；此处为连续原文片段。"
    return hint


def _cost(chunk: CandidateChunk, hint: str, count_tokens: Callable[[str], int]) -> int:
    source = f"{chunk.document_name or ''} {chunk.path} {chunk.page_number or ''}"
    return count_tokens(chunk.content) + count_tokens(hint) + count_tokens(source)


def _group_candidates(
    canonical: list[CandidateChunk], group_ref: str, *, category: str | None = None
) -> list[CandidateChunk]:
    return [
        chunk
        for chunk in canonical
        if group_ref in chunk.group_matches and (category is None or chunk.category == category)
    ]


def _sort_for_group(
    candidates: list[CandidateChunk], group_ref: str, scarcity: dict[str, float]
) -> list[CandidateChunk]:
    def key(chunk: CandidateChunk) -> tuple[object, ...]:
        grade = _GRADE[chunk.group_matches.get(group_ref, "possible")]
        gain = sum(scarcity.get(ref, 0.0) for ref in chunk.group_matches)
        return (
            grade,
            -gain,
            -(chunk.route_score or 0.0),
            -(chunk.rule_score or 0.0),
            chunk.page_number or 0,
            chunk.chunk_id,
        )

    return sorted(candidates, key=key)


def _round_robin(candidates: list[CandidateChunk]) -> list[CandidateChunk]:
    by_group_document: dict[tuple[str, str], list[CandidateChunk]] = {}
    for chunk in candidates:
        group_ref = sorted(chunk.group_matches)[0] if chunk.group_matches else ""
        key = (group_ref, chunk.document_id or "")
        by_group_document.setdefault(key, []).append(chunk)
    for values in by_group_document.values():
        values.sort(
            key=lambda chunk: (
                int(chunk.chunk_meta.get("broad_coverage_rank", 0)),
                chunk.page_number or 0,
                chunk.chunk_id,
            )
        )
    output: list[CandidateChunk] = []
    while by_group_document:
        for key in sorted(list(by_group_document)):
            values = by_group_document[key]
            if values:
                output.append(values.pop(0))
            if not values:
                by_group_document.pop(key, None)
    return output


def _truncate_content(
    chunk: CandidateChunk, *, available_tokens: int, count_tokens: Callable[[str], int]
) -> CandidateChunk | None:
    placeholder = chunk.model_copy(
        update={"chunk_meta": {**chunk.chunk_meta, "content_truncated": True}}
    )
    hint = _render_hint(placeholder)
    source = f"{chunk.document_name or ''} {chunk.path} {chunk.page_number or ''}"
    content_budget = available_tokens - count_tokens(hint) - count_tokens(source)
    if content_budget <= 0:
        return None
    content = chunk.content
    original_tokens = count_tokens(content)
    if original_tokens <= content_budget:
        return chunk
    questions = [
        question
        for group_ref in chunk.group_matches
        for question in chunk.questions_by_group.get(group_ref, [])
    ]
    terms = sorted(
        {term for question in questions for term in tokenize_query(question)},
        key=lambda term: (-len(term), term),
    )
    normalized_content = content.casefold()
    hit = next(
        (
            (normalized_content.find(term.casefold()), len(term))
            for term in terms
            if normalized_content.find(term.casefold()) >= 0
        ),
        None,
    )
    anchor = (hit[0] + hit[1] // 2) if hit else 0
    low, high = 1, len(content)
    best = ""
    best_start = 0
    while low <= high:
        middle = (low + high) // 2
        start = max(0, min(len(content) - middle, anchor - middle // 2)) if hit else 0
        value = content[start : start + middle]
        if count_tokens(value) <= content_budget:
            best = value
            best_start = start
            low = middle + 1
        else:
            high = middle - 1
    if not best:
        return None
    return placeholder.model_copy(
        update={
            "content": best,
            "chunk_meta": {
                **placeholder.chunk_meta,
                "content_truncated": True,
                "original_content_tokens": original_tokens,
                "returned_content_tokens": count_tokens(best),
                "original_chunk_id": chunk.chunk_id,
                "char_start": best_start,
                "char_end": best_start + len(best),
            },
        }
    )


def merge_candidates(
    *,
    candidates: list[CandidateChunk],
    groups: list[QueryGroup],
    top_k: int,
    max_return_tokens: int,
    count_tokens: Callable[[str], int],
    degraded_group_refs: list[str] | None = None,
    ensure_document_coverage: bool = False,
    target_document_ids_by_group: dict[str, list[str]] | None = None,
) -> MergeResult:
    by_key: dict[tuple[object, ...], CandidateChunk] = {}
    page_contents: dict[tuple[str, int], str] = {}
    for candidate in candidates:
        page_identity = _page_identity(candidate)
        if page_identity is not None:
            if page_identity in page_contents and page_contents[page_identity] != candidate.content:
                raise ValueError("inconsistent canonical chunk source")
            page_contents[page_identity] = candidate.content
        key = _canonical_key(candidate)
        by_key[key] = _merge_duplicate(by_key[key], candidate) if key in by_key else candidate
    canonical = list(by_key.values())
    candidate_count_by_group = {
        group.group_ref: sum(group.group_ref in chunk.group_matches for chunk in canonical)
        for group in groups
    }
    scarcity = {
        group_ref: 1.0 / max(1, count) for group_ref, count in candidate_count_by_group.items()
    }
    selected: list[CandidateChunk] = []
    selected_keys: set[tuple[object, ...]] = set()
    omitted_group_refs: set[str] = set()
    unavailable_group_refs: set[str] = set()
    omitted_any = False
    used = 0

    def try_add(chunk: CandidateChunk) -> bool:
        nonlocal used, omitted_any
        key = _canonical_key(chunk)
        if key in selected_keys:
            return True
        hint = _render_hint(chunk)
        cost = _cost(chunk, hint, count_tokens)
        if used + cost > max_return_tokens:
            omitted_any = True
            omitted_group_refs.update(chunk.group_matches)
            return False
        selected.append(chunk)
        selected_keys.add(key)
        used += cost
        return True

    group_order = sorted(
        groups,
        key=lambda group: (
            _PRIORITY[group.category],
            candidate_count_by_group.get(group.group_ref, 0),
            group.ordinal,
        ),
    )
    # First reserve one best candidate for every non-broad group. This is the
    # coverage safeguard and lets a shared page satisfy a scarce group.
    for group in group_order:
        if group.category == "routed_broad":
            continue
        options = _sort_for_group(
            _group_candidates(canonical, group.group_ref), group.group_ref, scarcity
        )
        if not options:
            unavailable_group_refs.add(group.group_ref)
            continue
        if not any(_canonical_key(option) in selected_keys for option in options):
            if not try_add(options[0]):
                omitted_group_refs.add(group.group_ref)

    target_docs = {
        group_ref: list(dict.fromkeys(document_ids))
        for group_ref, document_ids in (target_document_ids_by_group or {}).items()
    }

    def reserve_document_coverage(category: str) -> None:
        if not ensure_document_coverage:
            return
        for group in group_order:
            if group.category != category:
                continue
            for document_id in target_docs.get(group.group_ref, []):
                if any(
                    _chunk_covers_document(chunk, document_id)
                    and group.group_ref in chunk.group_matches
                    for chunk in selected
                ):
                    continue
                options = _sort_for_group(
                    [
                        chunk
                        for chunk in canonical
                        if chunk.category == category
                        and _chunk_covers_document(chunk, document_id)
                        and group.group_ref in chunk.group_matches
                    ],
                    group.group_ref,
                    scarcity,
                )
                if options:
                    try_add(options[0])

    # Atomic scope/direct evidence is necessary and does not obey top_k.
    for category in ("scope_direct", "routed_direct"):
        reserve_document_coverage(category)
        for chunk in sorted(
            (chunk for chunk in canonical if chunk.category == category),
            key=lambda item: (_PRIORITY[item.category], item.chunk_id),
        ):
            try_add(chunk)

    # Focused accept results are preferred; possible results only fill the
    # caller's soft target after every focused group has a chance.
    focused_accept = [
        chunk
        for chunk in canonical
        if chunk.category == "routed_focused"
        and any(grade == "accept" for grade in chunk.group_matches.values())
    ]
    reserve_document_coverage("routed_focused")
    for chunk in sorted(focused_accept, key=lambda item: _sort_key(item, scarcity)):
        try_add(chunk)
    focused_count = sum(1 for chunk in selected if chunk.category == "routed_focused")
    if focused_count < max(1, int(top_k)):
        focused_possible = [
            chunk
            for chunk in canonical
            if chunk.category == "routed_focused" and chunk not in focused_accept
        ]
        for chunk in sorted(focused_possible, key=lambda item: _sort_key(item, scarcity)):
            if focused_count >= int(top_k):
                break
            selected_count = len(selected)
            if try_add(chunk) and len(selected) > selected_count:
                focused_count += 1

    # Broad is last and uses every token left; its ordering already contains
    # document/section coverage ranks, then we round-robin by group and document.
    reserve_document_coverage("routed_broad")
    for chunk in _round_robin([chunk for chunk in canonical if chunk.category == "routed_broad"]):
        try_add(chunk)

    # If a mandatory group still has no result, use the remaining budget for a
    # continuous original excerpt. It is the final degradation, never a rewrite.
    covered_refs = {ref for chunk in selected for ref in chunk.group_matches}
    truncated_refs: set[str] = set()
    for group in group_order:
        if group.group_ref in covered_refs:
            continue
        options = _sort_for_group(
            _group_candidates(canonical, group.group_ref), group.group_ref, scarcity
        )
        for option in options:
            truncated = _truncate_content(
                option, available_tokens=max_return_tokens - used, count_tokens=count_tokens
            )
            if truncated is not None and try_add(truncated):
                covered_refs.update(option.group_matches)
                truncated_refs.update(option.group_matches)
                break
        if group.group_ref not in covered_refs:
            if options:
                omitted_group_refs.add(group.group_ref)
            else:
                unavailable_group_refs.add(group.group_ref)

    rendered: list[RetrievedChunk] = []
    actual_used = 0
    for chunk in selected:
        hint = _render_hint(chunk)
        actual_used += _cost(chunk, hint, count_tokens)
        rendered.append(
            RetrievedChunk(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                document_ids=chunk.document_ids,
                document_name=chunk.document_name,
                page_number=chunk.page_number,
                path=chunk.path,
                content=chunk.content,
                hint=hint,
                score=chunk.rule_score,
                source_type=chunk.source_type,
                document_meta=chunk.document_meta,
                chunk_meta=chunk.chunk_meta,
            )
        )
    warnings: list[RetrievalWarning] = []
    if omitted_any or omitted_group_refs:
        warnings.append(
            RetrievalWarning(
                code="RESULT_TOKEN_BUDGET_EXCEEDED",
                message=(
                    "return token budget caused candidate chunks to be omitted or coverage "
                    "to be reduced"
                ),
                affected_group_refs=sorted(omitted_group_refs),
            )
        )
    if unavailable_group_refs:
        warnings.append(
            RetrievalWarning(
                code="GROUP_EVIDENCE_UNAVAILABLE",
                message="one or more query groups produced no retrievable evidence",
                affected_group_refs=sorted(unavailable_group_refs),
                retryable=True,
            )
        )
    if truncated_refs:
        warnings.append(
            RetrievalWarning(
                code="ATOMIC_CHUNK_CONTENT_TRUNCATED",
                message="an original chunk was returned as a continuous token-budget excerpt",
                affected_group_refs=sorted(truncated_refs),
            )
        )
    uncovered_document_pairs = [
        (group_ref, document_id)
        for group_ref, document_ids in target_docs.items()
        for document_id in document_ids
        if not any(
            _chunk_covers_document(chunk, document_id) and group_ref in chunk.group_matches
            for chunk in selected
        )
    ]
    if ensure_document_coverage and uncovered_document_pairs:
        warnings.append(
            RetrievalWarning(
                code="DOCUMENT_COVERAGE_INCOMPLETE",
                message=(
                    "the hard return budget or unavailable evidence prevented one chunk "
                    "from being retained for every accepted document"
                ),
                affected_group_refs=sorted({pair[0] for pair in uncovered_document_pairs}),
                affected_document_ids=sorted({pair[1] for pair in uncovered_document_pairs}),
            )
        )
    uncovered = [group.group_ref for group in groups if group.group_ref not in covered_refs]
    covered = [group.group_ref for group in groups if group.group_ref in covered_refs]
    degraded = sorted(set(degraded_group_refs or []))
    document_coverage_incomplete = ensure_document_coverage and bool(uncovered_document_pairs)
    max_token_degradation = bool(omitted_any or omitted_group_refs or truncated_refs)
    workflow_degradation = bool(degraded or unavailable_group_refs)
    return MergeResult(
        chunks=rendered,
        internal_chunks=selected,
        warnings=warnings,
        coverage=CoverageSummary(
            complete=(
                not uncovered
                and not omitted_any
                and not degraded
                and not document_coverage_incomplete
            ),
            truncated=(
                max_token_degradation or workflow_degradation or document_coverage_incomplete
            ),
            truncated_by=(
                "max_return_tokens"
                if max_token_degradation
                else (
                    "workflow_degradation"
                    if workflow_degradation
                    else ("document_coverage" if document_coverage_incomplete else None)
                )
            ),
            covered_group_refs=covered,
            uncovered_group_refs=uncovered,
            degraded_group_refs=degraded,
        ),
        returned_tokens=actual_used,
    )


def _sort_key(chunk: CandidateChunk, scarcity: dict[str, float]) -> tuple[object, ...]:
    return (
        min((_GRADE[value] for value in chunk.group_matches.values()), default=1),
        -sum(scarcity.get(ref, 0.0) for ref in chunk.group_matches),
        -(chunk.route_score or 0.0),
        -(chunk.rule_score or 0.0),
        chunk.page_number or 0,
        chunk.chunk_id,
    )


def _chunk_covers_document(chunk: CandidateChunk, document_id: str) -> bool:
    return chunk.document_id == document_id or document_id in chunk.document_ids
