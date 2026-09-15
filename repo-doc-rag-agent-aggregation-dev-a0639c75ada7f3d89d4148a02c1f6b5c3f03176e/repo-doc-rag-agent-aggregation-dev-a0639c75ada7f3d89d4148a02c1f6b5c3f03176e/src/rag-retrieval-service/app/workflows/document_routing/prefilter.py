from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from app.api.schemas import CoverageSummary, RetrievalWarning, RetrievedChunk
from app.db.repositories import DocumentProfile
from app.workflows.classification.models import QueryGroup
from app.workflows.document_routing.models import PrefilterCandidate, PrefilterResult
from app.workflows.merge.service import MergeResult

_SCOPE_RELATIVE_MARKERS = (
    "这篇文档",
    "这个文档",
    "该文档",
    "本文",
    "当前文档",
    "上述文档",
    "这些文档",
    "这几个文档",
)
_WHOLE_SCOPE_MARKERS = ("所有文档", "全部文档", "上述文档", "这些文档", "这几个文档")
_CJK_BLOCK = re.compile(r"[\u4e00-\u9fff]{2,}")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,}")
_QUOTED = re.compile(r"[《\"“']([^》\"”']{2,})[》\"”']")
_GENERIC_ANCHORS = {
    "什么",
    "怎么",
    "如何",
    "多少",
    "哪些",
    "文档",
    "文件",
    "内容",
    "研究",
    "总结",
    "详细",
    "主要",
    "介绍",
    "这个",
    "这篇",
    "上述",
    "其中",
}
_MAX_IDENTITY_ANCHORS = 512
_OVERFLOW_PATH = "request:document-prefilter-overflow"


def _normalize(value: str) -> str:
    return re.sub(r"[\s._\-—–()（）\[\]【】]+", "", (value or "").casefold())


def _uses_scope_relative_reference(group: QueryGroup) -> bool:
    text = " ".join([*group.queries, group.target_docs_description, *group.target_docs_keywords])
    normalized = _normalize(text)
    return any(_normalize(marker) in normalized for marker in _SCOPE_RELATIVE_MARKERS)


def _uses_whole_scope_reference(group: QueryGroup) -> bool:
    text = " ".join([*group.queries, group.target_docs_description, *group.target_docs_keywords])
    normalized = _normalize(text)
    return any(_normalize(marker) in normalized for marker in _WHOLE_SCOPE_MARKERS)


def _prefilter_score(
    group: QueryGroup, document: DocumentProfile
) -> tuple[float, dict[str, float]]:
    name = _normalize(document.doc_name)
    description = _normalize(document.doc_description)
    components = {
        "name_phrase": 0.0,
        "name_coverage": 0.0,
        "description_phrase": 0.0,
        "description_coverage": 0.0,
    }
    keywords = [
        _normalize(keyword) for keyword in group.target_docs_keywords if _normalize(keyword)
    ]
    if not keywords:
        fallback = _normalize(group.target_docs_description or "")
        keywords = [fallback] if fallback else []
    if not keywords:
        return 0.0, components
    name_hits = sum(keyword in name for keyword in keywords)
    description_hits = sum(keyword in description for keyword in keywords)
    components["name_phrase"] = (
        0.45 if any(len(keyword) >= 3 and keyword in name for keyword in keywords) else 0.0
    )
    components["name_coverage"] = 0.30 * name_hits / len(keywords)
    components["description_phrase"] = (
        0.15 if any(len(keyword) >= 3 and keyword in description for keyword in keywords) else 0.0
    )
    components["description_coverage"] = 0.10 * description_hits / len(keywords)
    return min(1.0, sum(components.values())), components


def keyword_prefilter(
    *, groups: list[QueryGroup], documents: list[DocumentProfile], threshold: float
) -> dict[str, PrefilterResult]:
    """The original high-recall stage-one prefilter, preserved without fixed truncation."""
    output: dict[str, PrefilterResult] = {}
    for group in groups:
        candidates: list[PrefilterCandidate] = []
        for document in documents:
            score, components = _prefilter_score(group, document)
            if _uses_whole_scope_reference(group):
                score = 1.0
                components = {**components, "whole_request_scope": 1.0}
            elif (
                len(documents) == 1 and score < threshold and _uses_scope_relative_reference(group)
            ):
                score = 1.0
                components = {**components, "single_document_scope": 1.0}
            if score >= threshold:
                candidates.append(
                    PrefilterCandidate(
                        document=document, keyword_score=score, components=components
                    )
                )
        candidates.sort(
            key=lambda item: (-item.keyword_score, item.document.doc_name, item.document.doc_id)
        )
        output[group.group_ref] = PrefilterResult(
            group_ref=group.group_ref,
            candidates=candidates,
            rejected_count=len(documents) - len(candidates),
        )
    return output


def _anchor_terms(group: QueryGroup) -> set[str]:
    text = " ".join([*group.queries, group.target_docs_description])
    terms: set[str] = set()

    def add_term(value: str) -> bool:
        term = _normalize(value)
        if len(term) >= 2 and term not in _GENERIC_ANCHORS:
            terms.add(term)
        return len(terms) >= _MAX_IDENTITY_ANCHORS

    for value in _QUOTED.findall(text):
        if add_term(value):
            return terms
    for value in _WORD.findall(text):
        if add_term(value):
            return terms
    for block in _CJK_BLOCK.findall(text):
        normalized = _normalize(block)
        for size in range(min(10, len(normalized)), 1, -1):
            for index in range(len(normalized) - size + 1):
                if add_term(normalized[index : index + size]):
                    return terms
    return terms


def _secondary_prefilter(group: QueryGroup, result: PrefilterResult) -> PrefilterResult:
    candidates = result.candidates
    if len(candidates) < 2:
        return result
    searchable = {
        item.document.doc_id: (
            _normalize(item.document.doc_name),
            _normalize(item.document.doc_description),
        )
        for item in candidates
    }
    document_count = len(candidates)
    informative: dict[str, float] = {}
    for term in _anchor_terms(group):
        frequency = sum(
            term in name or term in description for name, description in searchable.values()
        )
        if frequency == 0 or frequency == document_count:
            continue
        normalized_idf = math.log((document_count + 1) / (frequency + 1)) / math.log(
            document_count + 1
        )
        if normalized_idf >= 0.35:
            informative[term] = normalized_idf
    if not informative:
        return result

    scored: list[tuple[float, PrefilterCandidate]] = []
    for item in candidates:
        name, description = searchable[item.document.doc_id]
        identity_score = 0.0
        for term, idf in informative.items():
            length_weight = min(1.75, max(0.5, len(term) / 4))
            if term in name:
                identity_score += 1.0 * idf * length_weight
            elif term in description:
                identity_score += 0.45 * idf * length_weight
        combined = identity_score + 0.15 * item.keyword_score
        scored.append((combined, item))
    best = max(score for score, _ in scored)
    if best <= 0:
        return result
    cutoff = best * 0.60
    retained = [
        item.model_copy(
            update={
                "keyword_score": score,
                "components": {
                    **item.components,
                    "secondary_identity_score": max(0.0, score - 0.15 * item.keyword_score),
                },
            }
        )
        for score, item in scored
        if score >= cutoff
    ]
    retained.sort(
        key=lambda item: (-item.keyword_score, item.document.doc_name, item.document.doc_id)
    )
    return PrefilterResult(
        group_ref=result.group_ref,
        candidates=retained,
        rejected_count=result.rejected_count + len(candidates) - len(retained),
    )


@dataclass(frozen=True, slots=True)
class KeywordPrefilterRun:
    results: dict[str, PrefilterResult]
    overflow_group_refs: list[str]


class KeywordPrefilterService:
    def __init__(
        self,
        *,
        threshold: float,
        max_candidates: int,
        count_tokens: Callable[[str], int],
    ) -> None:
        self.threshold = max(0.0, float(threshold))
        self.max_candidates = max(1, int(max_candidates))
        self.count_tokens = count_tokens

    def run(
        self, *, groups: list[QueryGroup], documents: list[DocumentProfile]
    ) -> KeywordPrefilterRun:
        results = keyword_prefilter(groups=groups, documents=documents, threshold=self.threshold)
        group_by_ref = {group.group_ref: group for group in groups}
        for group_ref, result in list(results.items()):
            if len(result.candidates) > self.max_candidates:
                results[group_ref] = _secondary_prefilter(group_by_ref[group_ref], result)
        overflow = sorted(
            group_ref
            for group_ref, result in results.items()
            if len(result.candidates) > self.max_candidates
        )
        return KeywordPrefilterRun(results=results, overflow_group_refs=overflow)

    def build_overflow_result(
        self,
        *,
        run: KeywordPrefilterRun,
        groups: list[QueryGroup],
        max_return_tokens: int,
    ) -> MergeResult:
        overflow_refs = set(run.overflow_group_refs)
        names: list[str] = []
        for group in sorted(groups, key=lambda item: item.ordinal):
            if group.group_ref not in overflow_refs:
                continue
            names.extend(
                candidate.document.doc_name for candidate in run.results[group.group_ref].candidates
            )
        names = list(dict.fromkeys(names))
        total = len(names)

        def hint(shown: int) -> str:
            omitted = total - shown
            return (
                "用户本次请求检索到大量相关文档，超出系统处理能力。"
                f"这里列举{shown}篇相似文档的信息，有{omitted}篇相似文档因篇幅大小限制未能列举。"
                "请询问用户具体需要访问哪篇文档，或者提示用户补充对目标文档的语义描述。"
            )

        shown_names: list[str] = []
        for name in names:
            tentative = [*shown_names, name]
            content = "相似文档名：" + json.dumps(tentative, ensure_ascii=False)
            token_cost = (
                self.count_tokens(content)
                + self.count_tokens(hint(len(tentative)))
                + self.count_tokens(_OVERFLOW_PATH)
            )
            if token_cost <= max_return_tokens:
                shown_names = tentative
        content = "相似文档名：" + json.dumps(shown_names, ensure_ascii=False)
        rendered_hint = hint(len(shown_names))
        returned_tokens = (
            self.count_tokens(content)
            + self.count_tokens(rendered_hint)
            + self.count_tokens(_OVERFLOW_PATH)
        )
        if returned_tokens > max_return_tokens:
            raise ValueError("max_return_tokens cannot fit the mandatory prefilter overflow hint")
        omitted = total - len(shown_names)
        chunk = RetrievedChunk(
            chunk_id="prefilter:ambiguous-documents",
            document_id=None,
            document_ids=[],
            document_name=None,
            path=_OVERFLOW_PATH,
            content=content,
            hint=rendered_hint,
            score=None,
            source_type="scope_metadata",
            document_meta=None,
            chunk_meta={
                "candidate_documents": total,
                "shown_documents": len(shown_names),
                "omitted_documents": omitted,
                "prefilter_overflow": True,
            },
        )
        warning = RetrievalWarning(
            code="DOCUMENT_PREFILTER_AMBIGUOUS_FALLBACK",
            message=(
                "keyword prefilter retained too many similar documents; document routing was "
                "skipped and document names were returned for clarification"
            ),
            affected_group_refs=sorted(overflow_refs),
        )
        return MergeResult(
            chunks=[chunk],
            internal_chunks=[],
            warnings=[warning],
            coverage=CoverageSummary(
                complete=False,
                truncated=omitted > 0,
                truncated_by="max_return_tokens" if omitted else None,
                covered_group_refs=sorted(overflow_refs),
                uncovered_group_refs=[],
                degraded_group_refs=sorted(overflow_refs),
            ),
            returned_tokens=returned_tokens,
        )
