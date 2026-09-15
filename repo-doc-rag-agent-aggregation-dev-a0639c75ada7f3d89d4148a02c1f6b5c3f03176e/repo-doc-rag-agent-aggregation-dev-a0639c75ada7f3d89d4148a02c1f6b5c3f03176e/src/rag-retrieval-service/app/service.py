from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import monotonic
from typing import Any
from uuid import uuid4

from app.api.schemas import (
    ChunkDocumentMeta,
    CoverageSummary,
    RetrievalDebug,
    RetrievalWarning,
    RetrieveRequest,
    RetrieveResponse,
    RetrieveUsage,
    SafeLLMCallSummary,
)
from app.config.settings import Settings
from app.core.errors import ApiError
from app.observability.trace import TraceCollector
from app.workflows.merge.service import MergeResult


class RetrievalEngine:
    def __init__(
        self,
        *,
        graph: Any,
        gateway: Any,
        settings: Settings,
        count_tokens: Callable[[str], int],
        tokenizer_name: str,
        merge: Callable[..., MergeResult],
    ) -> None:
        self.graph = graph
        self.gateway = gateway
        self.settings = settings
        self.count_tokens = count_tokens
        self.tokenizer_name = tokenizer_name
        self.merge = merge

    async def retrieve(self, request: RetrieveRequest) -> RetrieveResponse:
        if request.search_mode == "keyword":
            raise ApiError(
                501,
                "MODE_NOT_IMPLEMENTED",
                "keyword mode is reserved but is not implemented in this version",
            )
        if request.top_k > self.settings.rag_max_top_k:
            raise ApiError(422, "INVALID_TOP_K", "top_k exceeds the service maximum")
        if len([*request.doc_ids, *request.temp_doc_ids]) > self.settings.rag_max_doc_ids:
            raise ApiError(
                422, "TOO_MANY_DOCUMENT_IDS", "document ID count exceeds the service maximum"
            )
        max_tokens = request.max_return_tokens or self.settings.rag_default_return_tokens
        if (
            not self.settings.rag_min_return_tokens
            <= max_tokens
            <= self.settings.rag_max_return_tokens
        ):
            raise ApiError(
                422,
                "INVALID_RETURN_TOKEN_BUDGET",
                "max_return_tokens is outside the configured service range",
            )

        request_id = str(uuid4())
        started = monotonic()
        debug_enabled = self.settings.rag_debug_enabled and request.options.include_debug
        collector = (
            TraceCollector(enabled=True, request_id=request_id) if debug_enabled else None
        )
        if hasattr(self.gateway, "begin_request"):
            self.gateway.begin_request(request_id, debug_enabled=debug_enabled)
        initial: dict[str, Any] = {
            "request": request,
            "request_id": request_id,
            "soft_deadline_at": started + self.settings.request_soft_deadline_seconds,
            "trace_collector": collector,
            "candidate_chunks": [],
            "warnings": [],
        }
        latest = initial
        timed_out = False
        failed: Exception | None = None
        graph_budget = max(
            0.01,
            self.settings.request_hard_deadline_seconds
            - self.settings.request_finalization_reserve_seconds,
        )
        try:
            async with asyncio.timeout(graph_budget):
                async for snapshot in self.graph.astream(initial, stream_mode="values"):
                    latest = snapshot
        except TimeoutError:
            timed_out = True
        except ApiError:
            self.gateway.clear_stats(request_id)
            raise
        except Exception as exc:
            failed = exc

        if timed_out and hasattr(self.gateway, "cancel_request"):
            await self.gateway.cancel_request(request_id)

        warnings = list(latest.get("warnings", []))
        merge_result: MergeResult | None = latest.get("merge_result")
        if timed_out:
            warnings.append(
                RetrievalWarning(
                    code="REQUEST_HARD_DEADLINE_REACHED",
                    message=(
                        "request deadline was reached; complete evidence available so far "
                        "was returned"
                    ),
                    affected_group_refs=[group.group_ref for group in latest.get("groups", [])],
                    retryable=True,
                )
            )
        if failed is not None:
            warnings.append(
                RetrievalWarning(
                    code="REQUEST_PARTIAL_FAILURE",
                    message=(
                        "retrieval stopped after an internal dependency failure; available "
                        "evidence was returned"
                    ),
                    affected_group_refs=[group.group_ref for group in latest.get("groups", [])],
                    retryable=True,
                )
            )
        if merge_result is None and latest.get("groups") and latest.get("candidate_chunks"):
            merge_result = self.merge(
                candidates=latest["candidate_chunks"],
                groups=latest["groups"],
                top_k=request.top_k,
                max_return_tokens=max_tokens,
                count_tokens=self.count_tokens,
                degraded_group_refs=latest.get("degraded_group_refs", []),
                ensure_document_coverage=request.options.ensure_document_coverage,
                target_document_ids_by_group={
                    route.group_ref: list(
                        dict.fromkeys(routed.document.doc_id for routed in route.accept_docs)
                    )
                    for route in latest.get("routes", [])
                },
            )
            warnings.extend(merge_result.warnings)
        if merge_result is None:
            self.gateway.clear_stats(request_id)
            if timed_out:
                raise ApiError(
                    504,
                    "REQUEST_TIMEOUT",
                    "request timed out before any complete retrieval evidence was produced",
                    retryable=True,
                )
            if failed is not None:
                raise ApiError(
                    503,
                    "RETRIEVAL_DEPENDENCY_UNAVAILABLE",
                    "retrieval dependencies failed before any result was produced",
                    retryable=True,
                ) from failed
            raise ApiError(
                503, "NO_RETRIEVAL_RESULT", "retrieval graph produced no result", retryable=True
            )

        checkpoint_calls = latest.get("llm_request_count_at_checkpoint")
        after_calls = latest.get("llm_request_count_after_extensions")
        if (
            checkpoint_calls is not None
            and after_calls is not None
            and checkpoint_calls != after_calls
        ):
            warnings.append(
                RetrievalWarning(
                    code="POST_CHECKPOINT_LLM_CALL_DETECTED",
                    message="an invalid LLM call occurred after the core budget checkpoint",
                    retryable=False,
                )
            )
        warnings = _deduplicate_warnings(warnings)
        if not merge_result.chunks:
            self.gateway.clear_stats(request_id)
            if timed_out:
                raise ApiError(
                    504,
                    "REQUEST_TIMEOUT",
                    "request timed out before any complete retrieval evidence was produced",
                    retryable=True,
                )
            if failed is not None:
                raise ApiError(
                    503,
                    "RETRIEVAL_DEPENDENCY_UNAVAILABLE",
                    "retrieval dependencies failed before any result was produced",
                    retryable=True,
                ) from failed
            missing_resource = next(
                (warning for warning in warnings if warning.code == "DIRECT_RESOURCE_NOT_FOUND"),
                None,
            )
            if missing_resource is not None:
                raise ApiError(
                    404,
                    "DIRECT_RESOURCE_NOT_FOUND",
                    "one or more explicitly requested resources were not found",
                    details={"document_ids": missing_resource.affected_document_ids},
                )
        coverage = merge_result.coverage
        if timed_out or failed is not None:
            coverage = coverage.model_copy(
                update={
                    "complete": False,
                    "truncated": True,
                    "truncated_by": "deadline" if timed_out else "failure",
                }
            )

        stats = self.gateway.stats(request_id)
        call_records = ()
        if debug_enabled and collector is not None and hasattr(self.gateway, "call_records"):
            call_records = self.gateway.call_records(request_id)
            for record in call_records:
                collector.record(
                    node="llm_gateway",
                    phase=record.phase,
                    status=record.status,
                    duration_ms=record.queue_wait_ms + record.provider_duration_ms,
                    input_counts={},
                    output_counts={},
                    group_refs=[],
                    document_ids=[],
                    error_category=record.error_category,
                    llm=SafeLLMCallSummary(
                        model=record.model,
                        phase=record.phase,
                        group_index=record.group_index,
                        group_count=record.group_count,
                        queue_wait_ms=record.queue_wait_ms,
                        provider_duration_ms=record.provider_duration_ms,
                        prompt_tokens=record.prompt_tokens,
                        completion_tokens=record.completion_tokens,
                        provider_request_id=record.provider_request_id,
                    ),
                )
        elapsed_ms = int((monotonic() - started) * 1000)
        document_ids = {
            document_id
            for chunk in latest.get("candidate_chunks", [])
            for document_id in chunk.document_ids
        }
        page_candidates = [
            chunk for chunk in latest.get("candidate_chunks", []) if chunk.source_type == "page"
        ]
        profile_by_id = {profile.doc_id: profile for profile in latest.get("scope_documents", [])}
        response_chunks = []
        for chunk in merge_result.chunks:
            profile = profile_by_id.get(chunk.document_id or "")
            document_meta = None
            if request.options.include_document_meta and profile is not None:
                document_meta = ChunkDocumentMeta(
                    doc_type=profile.doc_type,
                    doc_description=profile.doc_description,
                    page_count=profile.page_count,
                    node_count=profile.node_count,
                    is_temporary=profile.is_temporary,
                )
            response_chunks.append(chunk.model_copy(update={"document_meta": document_meta}))
        usage = RetrieveUsage(
            candidate_document_count=len(document_ids) or len(latest.get("scope_documents", [])),
            inspected_node_count=int(latest.get("stats", {}).get("inspected_node_count", 0)),
            inspected_page_count=int(
                latest.get("stats", {}).get(
                    "inspected_page_count",
                    len({(chunk.document_id, chunk.page_number) for chunk in page_candidates}),
                )
            ),
            returned_count=len(response_chunks),
            returned_tokens=merge_result.returned_tokens,
            tokenizer=self.tokenizer_name,
            latency_ms=elapsed_ms,
            actual_mode="semantic",
            llm_request_count=stats.request_count,
            prompt_tokens=stats.prompt_tokens,
            completion_tokens=stats.completion_tokens,
            total_tokens=stats.total_tokens,
        )
        debug = (
            self._debug(
                request=request,
                request_id=request_id,
                latest=latest,
                collector=collector,
                coverage=coverage,
                gateway_stats=stats,
                call_records=call_records,
            )
            if debug_enabled and collector is not None
            else None
        )
        self.gateway.clear_stats(request_id)
        return RetrieveResponse(
            chunks=response_chunks,
            warnings=warnings,
            coverage=coverage,
            usage=usage,
            debug=debug,
        )

    @staticmethod
    def _debug(
        *,
        request: RetrieveRequest,
        request_id: str,
        latest: dict[str, Any],
        collector: TraceCollector,
        coverage: CoverageSummary,
        gateway_stats: Any,
        call_records: tuple[Any, ...],
    ) -> RetrievalDebug:
        durations: dict[str, int] = {}
        for event in collector.events:
            durations[event.node] = durations.get(event.node, 0) + int(event.duration_ms or 0)
        groups = latest.get("groups", [])
        routes = latest.get("routes", [])
        trace_events = list(collector.events)
        inspected_node_count = int(latest.get("stats", {}).get("inspected_node_count", 0))
        inspected_page_count = int(latest.get("stats", {}).get("inspected_page_count", 0))
        summary = {
            "scope_document_count": len(latest.get("scope_documents", [])),
            "group_count": len(groups),
            "groups_by_category": {
                category: sum(group.category == category for group in groups)
                for category in ("routed_focused", "routed_broad", "routed_direct", "scope_direct")
            },
            "route_accept_count": sum(len(route.accept_docs) for route in routes),
            "route_possible_count": sum(len(route.possible_docs) for route in routes),
            "route_reject_count": sum(route.rejected_document_count for route in routes),
            "inspected_node_count": inspected_node_count,
            "inspected_page_count": inspected_page_count,
            "candidate_count": len(latest.get("candidate_chunks", [])),
            "returned_count": len(latest.get("merge_result").chunks)
            if latest.get("merge_result")
            else 0,
            "uncovered_group_count": len(coverage.uncovered_group_refs),
            "degraded_group_refs": list(coverage.degraded_group_refs),
            "warning_codes": list(
                dict.fromkeys(warning.code for warning in latest.get("warnings", []))
            ),
            "llm_queue_wait_ms": gateway_stats.queue_wait_ms,
            "llm_provider_duration_ms": gateway_stats.provider_duration_ms,
            "llm_request_count": gateway_stats.request_count,
            "llm_prompt_tokens": gateway_stats.prompt_tokens,
            "llm_completion_tokens": gateway_stats.completion_tokens,
            "llm_total_tokens": gateway_stats.total_tokens,
            "llm_phase_counts": dict(gateway_stats.phase_counts),
            "failed_node_count": sum(event.status == "failed" for event in trace_events),
            "degraded_node_count": sum(event.status == "degraded" for event in trace_events),
        }
        return RetrievalDebug(
            request_id=request_id,
            graph_run_id=request_id,
            requested_mode=request.search_mode,
            actual_mode="semantic",
            state_summary=summary,
            node_durations_ms=durations,
            trace=trace_events,
            input={
                "query": request.query,
                "search_mode": request.search_mode,
                "top_k": request.top_k,
                "max_return_tokens": request.max_return_tokens,
                "scope_document_count": len(latest.get("scope_documents", [])),
            },
            classification_trace=list(latest.get("classification_trace", [])),
            groups=[group.model_dump(mode="json") for group in groups],
            routes=[route.model_dump(mode="json") for route in routes],
            candidates=[
                chunk.model_dump(mode="json") for chunk in latest.get("candidate_chunks", [])
            ],
            llm_calls=[
                {
                    "sequence": index,
                    "phase": record.phase,
                    "status": record.status,
                    "error_category": record.error_category,
                    "queue_wait_ms": record.queue_wait_ms,
                    "provider_duration_ms": record.provider_duration_ms,
                    "prompt_tokens": record.prompt_tokens,
                    "completion_tokens": record.completion_tokens,
                    "provider_request_id": record.provider_request_id,
                    "request": record.request,
                    "response": record.response,
                }
                for index, record in enumerate(call_records, start=1)
            ],
            tool_events=list(collector.details),
        )


def _deduplicate_warnings(warnings: list[RetrievalWarning]) -> list[RetrievalWarning]:
    output: list[RetrievalWarning] = []
    seen: set[tuple[Any, ...]] = set()
    for warning in warnings:
        key = (
            warning.code,
            tuple(warning.affected_group_refs),
            tuple(warning.affected_document_ids),
        )
        if key not in seen:
            output.append(warning)
            seen.add(key)
    return output
