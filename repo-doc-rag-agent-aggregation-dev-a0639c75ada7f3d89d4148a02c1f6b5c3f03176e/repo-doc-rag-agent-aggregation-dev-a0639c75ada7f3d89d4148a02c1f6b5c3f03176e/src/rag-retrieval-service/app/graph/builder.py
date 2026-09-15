from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.api.schemas import RetrievalWarning
from app.budgeting.tokens import QueryBudgetError
from app.core.errors import ApiError
from app.core.models import CandidateChunk, ToolOutput
from app.graph.state import RetrievalGraphState
from app.workflows.document_routing.models import DocumentRoute, RoutedDocument
from app.workflows.merge.service import MergeResult


@dataclass(slots=True)
class RetrievalGraphServices:
    repository: Any
    classifier: Any
    keyword_prefilter: Any
    router: Any
    scope_access: Any
    direct_access: Any
    focused_search: Any
    optional_possible: Any
    broad_retrieval: Any
    gateway: Any
    merge: Callable[..., MergeResult]
    count_tokens: Callable[[str], int]
    default_return_tokens: int
    db_executor: Any | None = None


async def _run_repository(services: RetrievalGraphServices, method: Any, **kwargs: Any) -> Any:
    if services.db_executor is None:
        return method(**kwargs)
    return await services.db_executor.run(method, **kwargs)


def _request_order(profiles: list[Any], request: Any) -> list[Any]:
    explicit = [*request.doc_ids, *request.temp_doc_ids]
    if not explicit:
        return sorted(profiles, key=lambda profile: (profile.doc_name, profile.doc_id))
    index = {doc_id: position for position, doc_id in enumerate(explicit)}
    return sorted(
        profiles,
        key=lambda profile: (
            index.get(profile.doc_id, len(index)),
            profile.doc_name,
            profile.doc_id,
        ),
    )


def _max_tokens(state: RetrievalGraphState, services: RetrievalGraphServices) -> int:
    return state["request"].max_return_tokens or services.default_return_tokens


def _accepted_document_ids_by_group(state: RetrievalGraphState) -> dict[str, list[str]]:
    return {
        route.group_ref: list(dict.fromkeys(routed.document.doc_id for routed in route.accept_docs))
        for route in state.get("routes", [])
    }


def _single_document_routes(groups: list[Any], document: Any) -> list[DocumentRoute]:
    return [
        DocumentRoute(
            group_ref=group.group_ref,
            accept_docs=[
                RoutedDocument(
                    document=document,
                    grade="accept",
                    keyword_score=1.0,
                    decision_source="rule_fallback",
                )
            ],
            possible_docs=[],
            rejected_document_count=0,
            prefilter_candidate_count=1,
            prefilter_rejected_count=0,
        )
        for group in groups
        if group.category != "scope_direct"
    ]


def build_retrieval_graph(services: RetrievalGraphServices):
    def counts(state: RetrievalGraphState | dict[str, Any]) -> dict[str, int]:
        return {
            "documents": len(state.get("scope_documents", [])),
            "groups": len(state.get("groups", [])),
            "routes": len(state.get("routes", [])),
            "candidates": len(state.get("candidate_chunks", [])),
        }

    def traced(name: str, function: Any):
        async def run(state: RetrievalGraphState) -> dict[str, Any]:
            collector = state.get("trace_collector")
            if collector is None or not collector.enabled:
                return await function(state)
            with collector.span(
                node=name,
                phase="graph_node",
                input_counts=counts(state),
                group_refs=[group.group_ref for group in state.get("groups", [])],
            ) as span:
                output = await function(state)
                output_warnings = output.get("warnings", [])
                degraded = bool(output_warnings or output.get("degraded_group_refs"))
                span.finish(
                    output_counts=counts({**state, **output}),
                    status="degraded" if degraded else "ok",
                    fallback_used=any("FALLBACK" in warning.code for warning in output_warnings),
                    error_category=output_warnings[0].code if output_warnings else None,
                )
                return output

        return run

    async def validate_and_load_scope(state: RetrievalGraphState) -> dict[str, Any]:
        request = state["request"]
        profiles, missing = await _run_repository(
            services,
            services.repository.fetch_scope,
            user_id=request.user_id,
            kb_id=request.kb_id,
            doc_ids=request.doc_ids,
            temp_doc_ids=request.temp_doc_ids,
            session_id=request.session_id,
        )
        if missing:
            raise ApiError(
                404,
                "DOCUMENT_NOT_FOUND",
                "one or more requested documents were not found or are not ready",
                details={"missing_document_ids": missing},
            )
        if not profiles:
            raise ApiError(404, "EMPTY_DOCUMENT_SCOPE", "none of the requested documents are ready")
        return {
            "scope_documents": _request_order(profiles, request),
            "stats": {"scope_count": len(profiles)},
        }

    async def classify_query(state: RetrievalGraphState) -> dict[str, Any]:
        run = await services.classifier.classify(
            request_id=state["request_id"],
            query=state["request"].query,
            scope_document_count=len(state["scope_documents"]),
            debug_enabled=bool(state.get("trace_collector") and state["trace_collector"].enabled),
        )
        if not run.groups:
            raise ApiError(
                422, "QUERY_TOO_LONG_OR_INVALID", "query classification produced no work groups"
            )
        output: dict[str, Any] = {
            "groups": run.groups,
            "warnings": run.warnings,
            "classification_trace": list(run.classification_trace),
        }
        if len(state["scope_documents"]) == 1:
            output["routes"] = _single_document_routes(run.groups, state["scope_documents"][0])
        return output

    async def after_classify_query(state: RetrievalGraphState) -> str:
        return "single_document" if len(state["scope_documents"]) == 1 else "multiple_documents"

    async def keyword_prefilter_node(state: RetrievalGraphState) -> dict[str, Any]:
        routed_groups = [group for group in state["groups"] if group.category != "scope_direct"]
        run = services.keyword_prefilter.run(
            groups=routed_groups, documents=state["scope_documents"]
        )
        if not run.overflow_group_refs:
            return {
                "prefilter_results": run.results,
                "keyword_prefilter_overflow": False,
            }
        result = services.keyword_prefilter.build_overflow_result(
            run=run,
            groups=routed_groups,
            max_return_tokens=_max_tokens(state, services),
        )
        return {
            "prefilter_results": run.results,
            "keyword_prefilter_overflow": True,
            "merge_result": result,
            "warnings": result.warnings,
        }

    async def after_keyword_prefilter(state: RetrievalGraphState) -> str:
        return "overflow" if state.get("keyword_prefilter_overflow") else "route"

    async def multi_document_route(state: RetrievalGraphState) -> dict[str, Any]:
        routed_groups = [group for group in state["groups"] if group.category != "scope_direct"]
        if not routed_groups:
            return {"routes": []}
        try:
            routes, warnings = await services.router.route(
                request_id=state["request_id"],
                groups=routed_groups,
                documents=state["scope_documents"],
                prefiltered=state.get("prefilter_results"),
            )
        except QueryBudgetError as exc:
            raise ApiError(422, exc.code, str(exc)) from exc
        empty = [route.group_ref for route in routes if route.prefilter_candidate_count == 0]
        if empty:
            warnings = [
                *warnings,
                RetrievalWarning(
                    code="KEYWORD_PREFILTER_EMPTY",
                    message="keyword hard prefilter retained no document for one or more groups",
                    affected_group_refs=empty,
                ),
            ]
        return {"routes": routes, "warnings": warnings}

    async def core_access(state: RetrievalGraphState) -> dict[str, Any]:
        chunks: list[CandidateChunk] = []
        warnings: list[RetrievalWarning] = []
        degraded_group_refs: list[str] = []
        stats = dict(state.get("stats", {}))
        request = state["request"]
        routes = {route.group_ref: route for route in state.get("routes", [])}

        async def run_scope(group: Any) -> ToolOutput:
            maybe_output = services.scope_access.run(
                group=group,
                profiles=state["scope_documents"],
                request_id=state["request_id"],
                original_query=request.query_text,
            )
            return await maybe_output if hasattr(maybe_output, "__await__") else maybe_output

        direct = None
        if services.direct_access is not None:
            direct = (
                services.direct_access.for_request(request)
                if hasattr(services.direct_access, "for_request")
                else services.direct_access
            )
        focused = None
        if services.focused_search is not None:
            focused = (
                services.focused_search.for_request(request)
                if hasattr(services.focused_search, "for_request")
                else services.focused_search
            )

        access_tasks: list[tuple[str, str, str | None, Any]] = []
        for group in state["groups"]:
            if group.category == "scope_direct":
                access_tasks.append(("scope", group.group_ref, None, run_scope(group)))
                continue
            route = routes.get(group.group_ref)
            if group.category == "routed_direct" and direct is not None:
                documents = (
                    []
                    if route is None
                    else [routed.document for routed in (route.accept_docs or route.possible_docs)]
                )
                if not documents:
                    warnings.append(
                        RetrievalWarning(
                            code="DIRECT_NO_ROUTED_DOCUMENT",
                            message="direct access had no routed document",
                            affected_group_refs=[group.group_ref],
                        )
                    )
                    continue
                access_tasks.append(
                    (
                        "direct",
                        group.group_ref,
                        None,
                        direct.run(
                            request_id=state["request_id"],
                            group=group,
                            documents=documents,
                        ),
                    )
                )
                continue
            if group.category == "routed_focused" and focused is not None:
                for routed in route.accept_docs if route else []:
                    access_tasks.append(
                        (
                            "focused",
                            group.group_ref,
                            routed.document.doc_id,
                            focused.search(
                                request_id=state["request_id"],
                                user_id=request.user_id,
                                kb_id=request.kb_id,
                                group=group,
                                routed_document=routed,
                                trace_collector=state.get("trace_collector"),
                            ),
                        )
                    )

        if access_tasks:
            results = await asyncio.gather(
                *(task for _, _, _, task in access_tasks), return_exceptions=True
            )
            for (kind, group_ref, doc_id, _), result in zip(access_tasks, results, strict=True):
                if isinstance(result, Exception):
                    failure = {
                        "scope": (
                            "SCOPE_GROUP_FAILED",
                            "scope access failed for one query group",
                        ),
                        "direct": (
                            "DIRECT_GROUP_FAILED",
                            "direct access failed for one query group",
                        ),
                        "focused": (
                            "FOCUSED_DOCUMENT_FAILED",
                            "focused retrieval failed for one routed document",
                        ),
                    }[kind]
                    warnings.append(
                        RetrievalWarning(
                            code=failure[0],
                            message=failure[1],
                            affected_group_refs=[group_ref],
                            affected_document_ids=[doc_id] if doc_id is not None else [],
                            retryable=True,
                        )
                    )
                    continue
                chunks.extend(result.chunks)
                warnings.extend(result.warnings)
                stats["inspected_node_count"] = (
                    stats.get("inspected_node_count", 0) + result.inspected_node_count
                )
                stats["inspected_page_count"] = (
                    stats.get("inspected_page_count", 0) + result.inspected_page_count
                )
                if kind in {"scope", "direct"} and not result.coverage_complete:
                    degraded_group_refs.append(group_ref)
        return {
            "candidate_chunks": chunks,
            "warnings": warnings,
            "degraded_group_refs": degraded_group_refs,
            "stats": stats,
        }

    async def core_budget_checkpoint(state: RetrievalGraphState) -> dict[str, Any]:
        provisional = services.merge(
            candidates=state.get("candidate_chunks", []),
            groups=state["groups"],
            top_k=state["request"].top_k,
            max_return_tokens=_max_tokens(state, services),
            count_tokens=services.count_tokens,
            degraded_group_refs=state.get("degraded_group_refs", []),
            ensure_document_coverage=(state["request"].options.ensure_document_coverage),
            target_document_ids_by_group=_accepted_document_ids_by_group(state),
        )
        return {
            "remaining_return_tokens": max(
                0, _max_tokens(state, services) - provisional.returned_tokens
            ),
            "llm_request_count_at_checkpoint": services.gateway.stats(
                state["request_id"]
            ).request_count,
        }

    async def optional_focused_possible(state: RetrievalGraphState) -> dict[str, Any]:
        request = state["request"]
        if monotonic() >= state.get("soft_deadline_at", float("inf")):
            return {
                "warnings": [
                    RetrievalWarning(
                        code="REQUEST_SOFT_DEADLINE_REACHED",
                        message="optional retrieval was skipped after the request soft deadline",
                        retryable=True,
                    )
                ]
            }
        output = await _run_repository(
            services,
            services.optional_possible.retrieve,
            user_id=request.user_id,
            kb_id=request.kb_id,
            groups=state["groups"],
            routes=state.get("routes", []),
            existing_candidates=state.get("candidate_chunks", []),
            top_k=request.top_k,
            remaining_tokens=state.get("remaining_return_tokens", 0),
            session_id=request.session_id,
        )
        stats = dict(state.get("stats", {}))
        stats["inspected_node_count"] = (
            stats.get("inspected_node_count", 0) + output.inspected_node_count
        )
        stats["inspected_page_count"] = (
            stats.get("inspected_page_count", 0) + output.inspected_page_count
        )
        return {
            "candidate_chunks": output.chunks,
            "warnings": output.warnings,
            "stats": stats,
        }

    async def broad_retrieval(state: RetrievalGraphState) -> dict[str, Any]:
        request = state["request"]
        if monotonic() >= state.get("soft_deadline_at", float("inf")):
            return {
                "llm_request_count_after_extensions": services.gateway.stats(
                    state["request_id"]
                ).request_count
            }
        provisional = services.merge(
            candidates=state.get("candidate_chunks", []),
            groups=state["groups"],
            top_k=request.top_k,
            max_return_tokens=_max_tokens(state, services),
            count_tokens=services.count_tokens,
            degraded_group_refs=state.get("degraded_group_refs", []),
            ensure_document_coverage=request.options.ensure_document_coverage,
            target_document_ids_by_group=_accepted_document_ids_by_group(state),
        )
        remaining = max(0, _max_tokens(state, services) - provisional.returned_tokens)
        output = await _run_repository(
            services,
            services.broad_retrieval.retrieve,
            user_id=request.user_id,
            kb_id=request.kb_id,
            groups=state["groups"],
            routes=state.get("routes", []),
            remaining_tokens=remaining,
            session_id=request.session_id,
        )
        degraded_refs = (
            sorted(
                {
                    group_ref
                    for warning in output.warnings
                    for group_ref in warning.affected_group_refs
                }
            )
            if not output.coverage_complete
            else []
        )
        stats = dict(state.get("stats", {}))
        stats["inspected_node_count"] = (
            stats.get("inspected_node_count", 0) + output.inspected_node_count
        )
        stats["inspected_page_count"] = (
            stats.get("inspected_page_count", 0) + output.inspected_page_count
        )
        return {
            "candidate_chunks": output.chunks,
            "warnings": output.warnings,
            "degraded_group_refs": degraded_refs,
            "remaining_return_tokens": max(0, remaining),
            "stats": stats,
            "llm_request_count_after_extensions": services.gateway.stats(
                state["request_id"]
            ).request_count,
        }

    async def merge(state: RetrievalGraphState) -> dict[str, Any]:
        result = services.merge(
            candidates=state.get("candidate_chunks", []),
            groups=state["groups"],
            top_k=state["request"].top_k,
            max_return_tokens=_max_tokens(state, services),
            count_tokens=services.count_tokens,
            degraded_group_refs=state.get("degraded_group_refs", []),
            ensure_document_coverage=(state["request"].options.ensure_document_coverage),
            target_document_ids_by_group=_accepted_document_ids_by_group(state),
        )
        return {"merge_result": result, "warnings": result.warnings}

    graph = StateGraph(RetrievalGraphState)
    graph.add_node(
        "validate_and_load_scope", traced("validate_and_load_scope", validate_and_load_scope)
    )
    graph.add_node("classify_query", traced("classify_query", classify_query))
    graph.add_node("keyword_prefilter", traced("keyword_prefilter", keyword_prefilter_node))
    graph.add_node("multi_document_route", traced("multi_document_route", multi_document_route))
    graph.add_node("core_access", traced("core_access", core_access))
    graph.add_node(
        "core_budget_checkpoint", traced("core_budget_checkpoint", core_budget_checkpoint)
    )
    graph.add_node(
        "optional_focused_possible", traced("optional_focused_possible", optional_focused_possible)
    )
    graph.add_node("broad_retrieval", traced("broad_retrieval", broad_retrieval))
    graph.add_node("merge", traced("merge", merge))
    graph.add_edge(START, "validate_and_load_scope")
    graph.add_edge("validate_and_load_scope", "classify_query")
    graph.add_conditional_edges(
        "classify_query",
        after_classify_query,
        {"single_document": "core_access", "multiple_documents": "keyword_prefilter"},
    )
    graph.add_conditional_edges(
        "keyword_prefilter",
        after_keyword_prefilter,
        {"overflow": END, "route": "multi_document_route"},
    )
    graph.add_edge("multi_document_route", "core_access")
    graph.add_edge("core_access", "core_budget_checkpoint")
    graph.add_edge("core_budget_checkpoint", "optional_focused_possible")
    graph.add_edge("optional_focused_possible", "broad_retrieval")
    graph.add_edge("broad_retrieval", "merge")
    graph.add_edge("merge", END)
    return graph.compile()
