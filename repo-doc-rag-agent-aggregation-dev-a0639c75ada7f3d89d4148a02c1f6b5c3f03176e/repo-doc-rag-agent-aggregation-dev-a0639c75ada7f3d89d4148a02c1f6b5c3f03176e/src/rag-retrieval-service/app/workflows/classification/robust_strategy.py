from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from app.api.schemas import RetrievalCategory, RetrievalWarning
from app.llm.gateway import LLMGateway, LLMRequest
from app.workflows.classification.contracts import (
    binary_decision_schema,
    document_grouping_schema,
    query_rewrite_schema,
    validate_binary_decisions,
    validate_document_groups,
    validate_rewritten_queries,
)
from app.workflows.classification.guards import guard_category
from app.workflows.classification.models import (
    DocumentCluster,
    QueryClassificationOutput,
    QueryGroup,
    ReferencedQuery,
    RoutedQueryGroupDraft,
    ScopeDirectGroupDraft,
    materialize_groups,
)
from app.workflows.classification.prompts import (
    DIRECT_CLASSIFIER_PROMPT,
    FOCUSED_CLASSIFIER_PROMPT,
    QUERY_REWRITE_PROMPT,
    SCOPE_CLASSIFIER_PROMPT,
    TARGET_DOCUMENT_GROUPING_PROMPT,
)
from app.workflows.classification.strategies import ClassificationRun, _keywords


class RobustLLMClassificationStrategy:
    def __init__(self, *, gateway: LLMGateway, model: str, max_tokens: int) -> None:
        self.gateway = gateway
        self.model = model
        self.max_tokens = max(1, int(max_tokens))

    async def classify(
        self,
        *,
        request_id: str,
        query: str | list[str],
        scope_document_count: int = 0,
        debug_enabled: bool = False,
    ) -> ClassificationRun:
        input_mode = "string" if isinstance(query, str) else "list"
        questions = (
            await self._rewrite(
                request_id=request_id,
                query=query,
                scope_document_count=scope_document_count,
            )
            if isinstance(query, str)
            else _normalize_queries(query)
        )
        referenced = [
            ReferencedQuery(query_ref=f"q_{index:03d}", question=question)
            for index, question in enumerate(questions, 1)
        ]
        classification_trace: list[dict[str, Any]] = []
        if debug_enabled:
            classification_trace.append(
                {
                    "phase": "normalized_queries",
                    "input_mode": input_mode,
                    "rewrite_used": input_mode == "string",
                    "queries": _serialize_referenced_queries(referenced),
                }
            )
        guard_categories = {
            item.query_ref: category
            for item in referenced
            if (category := guard_category(item.question)) is not None
        }
        model_scope_decisions = await self._classify_binary(
            request_id=request_id,
            phase="robust_scope_classifier",
            prompt=SCOPE_CLASSIFIER_PROMPT,
            queries=referenced,
            scope_document_count=scope_document_count,
        )
        scope_decisions = dict(model_scope_decisions)
        scope_guard_overrides: dict[str, RetrievalCategory] = {}
        for ref, category in guard_categories.items():
            if category == "scope_direct" and not scope_decisions[ref]:
                scope_decisions[ref] = True
                scope_guard_overrides[ref] = category
        scope_queries = [item for item in referenced if scope_decisions[item.query_ref]]
        routed_queries = [item for item in referenced if not scope_decisions[item.query_ref]]
        if debug_enabled:
            classification_trace.append(
                {
                    "phase": "scope_decision",
                    "model_decisions": model_scope_decisions,
                    "decisions": scope_decisions,
                    "guard_overrides": scope_guard_overrides,
                    "scope_query_refs": [item.query_ref for item in scope_queries],
                    "routed_query_refs": [item.query_ref for item in routed_queries],
                }
            )
        if not routed_queries:
            groups = _materialize(scope_queries=scope_queries, routed_queries=[], clusters=[])
            if debug_enabled:
                classification_trace.append(_final_groups_trace(groups))
            return ClassificationRun(
                groups=groups,
                warnings=[],
                classification_trace=tuple(classification_trace),
            )

        direct_result, focused_result, grouping_result = await asyncio.gather(
            self._classify_binary(
                request_id=request_id,
                phase="robust_direct_classifier",
                prompt=DIRECT_CLASSIFIER_PROMPT,
                queries=routed_queries,
                scope_document_count=scope_document_count,
            ),
            self._classify_binary(
                request_id=request_id,
                phase="robust_focused_classifier",
                prompt=FOCUSED_CLASSIFIER_PROMPT,
                queries=routed_queries,
                scope_document_count=scope_document_count,
            ),
            self._group_documents(request_id=request_id, queries=routed_queries),
            return_exceptions=True,
        )
        refs = [item.query_ref for item in routed_queries]
        warnings: list[RetrievalWarning] = []
        model_direct = _decisions_or_broad_fallback(
            direct_result,
            refs=refs,
            code="DIRECT_CLASSIFIER_BROAD_FALLBACK",
            message=(
                "direct classifier failed; affected questions conservatively used broad retrieval"
            ),
            warnings=warnings,
        )
        model_focused = _decisions_or_broad_fallback(
            focused_result,
            refs=refs,
            code="FOCUSED_CLASSIFIER_BROAD_FALLBACK",
            message=(
                "focused classifier failed; affected questions conservatively used broad retrieval"
            ),
            warnings=warnings,
        )
        if isinstance(grouping_result, Exception):
            clusters = _fallback_clusters(routed_queries)
            warnings.append(
                RetrievalWarning(
                    code="TARGET_DOCUMENT_GROUPING_RULE_FALLBACK",
                    message=(
                        "target document grouping failed; each question used an independent "
                        "rule group"
                    ),
                    retryable=True,
                )
            )
        else:
            clusters = grouping_result
        direct = dict(model_direct)
        focused = dict(model_focused)
        non_scope_guard_overrides: dict[str, RetrievalCategory] = {}
        for ref in refs:
            category = guard_categories.get(ref)
            if category == "routed_direct":
                direct[ref] = True
                focused[ref] = False
                non_scope_guard_overrides[ref] = category
            elif category == "routed_broad":
                direct[ref] = False
                focused[ref] = False
                non_scope_guard_overrides[ref] = category
        categories = {
            ref: _category(
                is_scope=False,
                is_direct=direct[ref],
                is_focused=focused[ref],
            )
            for ref in refs
        }
        if debug_enabled:
            classification_trace.append(
                {
                    "phase": "non_scope_decision",
                    "model_direct_decisions": model_direct,
                    "model_focused_decisions": model_focused,
                    "direct_decisions": direct,
                    "focused_decisions": focused,
                    "category_by_query_ref": categories,
                    "guard_overrides": non_scope_guard_overrides,
                    "degraded_components": [warning.code for warning in warnings],
                }
            )
            classification_trace.append(
                {
                    "phase": "document_clusters",
                    "clusters": [
                        {
                            "query_refs": list(cluster.query_refs),
                            "target_docs_description": cluster.target_docs_description,
                            "target_docs_keywords": list(cluster.target_docs_keywords),
                        }
                        for cluster in clusters
                    ],
                    "fallback_used": isinstance(grouping_result, Exception),
                }
            )
        groups = _materialize(
            scope_queries=scope_queries,
            routed_queries=routed_queries,
            clusters=clusters,
            categories=categories,
        )
        if warnings:
            affected = [group.group_ref for group in groups if group.category != "scope_direct"]
            warnings = [
                warning.model_copy(update={"affected_group_refs": affected}) for warning in warnings
            ]
            groups = [
                group.model_copy(update={"degraded": group.category != "scope_direct"})
                for group in groups
            ]
        if debug_enabled:
            classification_trace.append(_final_groups_trace(groups))
        return ClassificationRun(
            groups=groups,
            warnings=warnings,
            degraded=bool(warnings),
            classification_trace=tuple(classification_trace),
        )

    async def _rewrite(
        self, *, request_id: str, query: str, scope_document_count: int
    ) -> list[str]:
        result = await self.gateway.complete_json(
            LLMRequest(
                request_id=request_id,
                phase="robust_query_rewrite",
                messages=[
                    {"role": "system", "content": QUERY_REWRITE_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "scope_document_count": scope_document_count,
                                "original_query": query,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                model=self.model,
                max_tokens=self.max_tokens,
                response_format=query_rewrite_schema(),
            )
        )
        return validate_rewritten_queries(result.data, query)

    async def _classify_binary(
        self,
        *,
        request_id: str,
        phase: str,
        prompt: str,
        queries: list[ReferencedQuery],
        scope_document_count: int,
    ) -> dict[str, bool]:
        refs = [item.query_ref for item in queries]
        result = await self.gateway.complete_json(
            LLMRequest(
                request_id=request_id,
                phase=phase,
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "scope_document_count": scope_document_count,
                                "queries": [
                                    {"query_ref": item.query_ref, "question": item.question}
                                    for item in queries
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                model=self.model,
                max_tokens=self.max_tokens,
                response_format=binary_decision_schema(
                    refs, schema_name=phase.removeprefix("robust_")
                ),
            )
        )
        return validate_binary_decisions(result.data, refs)

    async def _group_documents(
        self, *, request_id: str, queries: list[ReferencedQuery]
    ) -> list[DocumentCluster]:
        refs = [item.query_ref for item in queries]
        result = await self.gateway.complete_json(
            LLMRequest(
                request_id=request_id,
                phase="robust_target_document_grouping",
                messages=[
                    {"role": "system", "content": TARGET_DOCUMENT_GROUPING_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "queries": [
                                    {"query_ref": item.query_ref, "question": item.question}
                                    for item in queries
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                model=self.model,
                max_tokens=self.max_tokens,
                response_format=document_grouping_schema(refs),
            )
        )
        return validate_document_groups(result.data, refs)


def _normalize_queries(queries: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(item.strip() for item in queries if item.strip()))
    if not normalized or sum(len(item) for item in normalized) > 16000:
        raise ValueError("preprocessed query list is empty or exceeds the character limit")
    return normalized


def _serialize_referenced_queries(queries: list[ReferencedQuery]) -> list[dict[str, str]]:
    return [{"query_ref": item.query_ref, "question": item.question} for item in queries]


def _final_groups_trace(groups: list[QueryGroup]) -> dict[str, Any]:
    return {
        "phase": "final_groups",
        "groups": [
            {
                "group_ref": group.group_ref,
                "category": group.category,
                "queries": list(group.queries),
                "target_docs_description": group.target_docs_description,
                "target_docs_keywords": list(group.target_docs_keywords),
                "degraded": group.degraded,
            }
            for group in groups
        ],
    }


def _decisions_or_broad_fallback(
    result: Any,
    *,
    refs: list[str],
    code: str,
    message: str,
    warnings: list[RetrievalWarning],
) -> dict[str, bool]:
    if not isinstance(result, Exception):
        return result
    warnings.append(RetrievalWarning(code=code, message=message, retryable=True))
    return dict.fromkeys(refs, False)


def _fallback_clusters(queries: list[ReferencedQuery]) -> list[DocumentCluster]:
    return [
        DocumentCluster(
            query_refs=(item.query_ref,),
            target_docs_description=item.question,
            target_docs_keywords=tuple(_keywords(item.question)),
        )
        for item in queries
    ]


def _category(*, is_scope: bool, is_direct: bool, is_focused: bool) -> RetrievalCategory:
    if is_scope:
        return "scope_direct"
    if is_direct:
        return "routed_direct"
    if is_focused:
        return "routed_focused"
    return "routed_broad"


def _materialize(
    *,
    scope_queries: list[ReferencedQuery],
    routed_queries: list[ReferencedQuery],
    clusters: list[DocumentCluster],
    categories: dict[str, RetrievalCategory] | None = None,
) -> list[QueryGroup]:
    by_ref = {item.query_ref: item.question for item in routed_queries}
    drafts: dict[RetrievalCategory, list[RoutedQueryGroupDraft]] = defaultdict(list)
    for cluster in clusters:
        by_category: dict[RetrievalCategory, list[str]] = defaultdict(list)
        for ref in cluster.query_refs:
            by_category[(categories or {})[ref]].append(by_ref[ref])
        for category, questions in by_category.items():
            drafts[category].append(
                RoutedQueryGroupDraft(
                    queries=questions,
                    target_docs_description=cluster.target_docs_description,
                    target_docs_keywords=list(cluster.target_docs_keywords),
                )
            )
    output = QueryClassificationOutput(
        routed_focused=drafts["routed_focused"],
        routed_broad=drafts["routed_broad"],
        routed_direct=drafts["routed_direct"],
        scope_direct=[ScopeDirectGroupDraft(queries=[item.question]) for item in scope_queries],
    )
    return materialize_groups(output)
