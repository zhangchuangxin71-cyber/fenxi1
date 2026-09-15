from __future__ import annotations

from time import monotonic
from uuid import uuid4

from app.api.schemas import (
    DocumentRouteGroupResult,
    DocumentRouteRequest,
    DocumentRouteResponse,
    DocumentRouteUsage,
)
from app.budgeting.tokens import QueryBudgetError
from app.core.errors import ApiError
from app.workflows.classification.models import QueryGroup
from app.workflows.document_routing.models import PrefilterCandidate, PrefilterResult


class DocumentRouteEndpointService:
    def __init__(
        self,
        *,
        repository,
        db_executor,
        keyword_prefilter,
        router,
        gateway,
        max_doc_ids: int,
    ) -> None:
        self.repository = repository
        self.db_executor = db_executor
        self.keyword_prefilter = keyword_prefilter
        self.router = router
        self.gateway = gateway
        self.max_doc_ids = max(1, int(max_doc_ids))

    async def route(self, request: DocumentRouteRequest) -> DocumentRouteResponse:
        requested_ids = [*request.doc_ids, *request.temp_doc_ids]
        if len(requested_ids) > self.max_doc_ids:
            raise ApiError(
                422,
                "TOO_MANY_DOCUMENT_IDS",
                "document ID count exceeds the service maximum",
            )

        request_id = f"route_{uuid4()}"
        started = monotonic()
        self.gateway.begin_request(request_id, debug_enabled=True)
        try:
            documents, missing = await self._load_documents(request)
            if missing:
                raise ApiError(
                    404,
                    "DOCUMENT_NOT_FOUND",
                    "one or more requested documents were not found or are not ready",
                    details={"missing_document_ids": missing},
                )
            if not documents:
                raise ApiError(
                    404, "EMPTY_DOCUMENT_SCOPE", "none of the requested documents are ready"
                )

            documents_by_id = {document.doc_id: document for document in documents}
            ordered_documents = [
                documents_by_id[doc_id] for doc_id in requested_ids if doc_id in documents_by_id
            ]
            groups = [
                QueryGroup(
                    group_ref=f"g{index + 1:04d}",
                    category="routed_focused",
                    queries=list(criterion.queries),
                    target_docs_description=criterion.target_docs_description,
                    target_docs_keywords=list(criterion.target_docs_keywords),
                    ordinal=index + 1,
                )
                for index, criterion in enumerate(request.criteria)
            ]

            if request.keyword_prefilter:
                prefilter_run = self.keyword_prefilter.run(
                    groups=groups,
                    documents=ordered_documents,
                )
                if prefilter_run.overflow_group_refs:
                    raise ApiError(
                        422,
                        "DOCUMENT_PREFILTER_OVERFLOW",
                        "keyword prefilter retained too many candidate documents",
                        details={"affected_group_refs": prefilter_run.overflow_group_refs},
                    )
                prefiltered = prefilter_run.results
            else:
                prefiltered = _all_document_candidates(groups, ordered_documents)

            routes, warnings = await self.router.route(
                request_id=request_id,
                groups=groups,
                documents=ordered_documents,
                prefiltered=prefiltered,
            )
            route_by_ref = {route.group_ref: route for route in routes}
            results: list[DocumentRouteGroupResult] = []
            for index, group in enumerate(groups):
                route = route_by_ref[group.group_ref]
                accept_ids = [item.document.doc_id for item in route.accept_docs]
                possible_ids = [item.document.doc_id for item in route.possible_docs]
                selected_ids = {*accept_ids, *possible_ids}
                reject_ids = [
                    document.doc_id
                    for document in ordered_documents
                    if document.doc_id not in selected_ids
                ]
                sources = {
                    item.decision_source for item in [*route.accept_docs, *route.possible_docs]
                }
                if not sources or sources == {"llm"}:
                    decision_source = "llm"
                elif sources == {"rule_fallback"}:
                    decision_source = "rule_fallback"
                else:
                    decision_source = "mixed"
                group_warnings = [
                    warning
                    for warning in warnings
                    if not warning.affected_group_refs
                    or group.group_ref in warning.affected_group_refs
                ]
                results.append(
                    DocumentRouteGroupResult(
                        index=index,
                        accept_doc_ids=accept_ids,
                        possible_doc_ids=possible_ids,
                        reject_doc_ids=reject_ids,
                        decision_source=decision_source,
                        degraded=route.degraded,
                        warnings=group_warnings,
                    )
                )

            stats = self.gateway.stats(request_id)
            return DocumentRouteResponse(
                request_id=request_id,
                groups=results,
                usage=DocumentRouteUsage(
                    candidate_document_count=len(ordered_documents),
                    llm_request_count=stats.request_count,
                    prompt_tokens=stats.prompt_tokens,
                    completion_tokens=stats.completion_tokens,
                    latency_ms=int((monotonic() - started) * 1000),
                ),
            )
        except QueryBudgetError as exc:
            raise ApiError(422, exc.code, str(exc)) from exc
        finally:
            self.gateway.clear_stats(request_id)

    async def _load_documents(self, request: DocumentRouteRequest):
        kwargs = {
            "user_id": request.user_id,
            "kb_id": request.kb_id,
            "doc_ids": request.doc_ids,
            "temp_doc_ids": request.temp_doc_ids,
            "session_id": request.session_id,
        }
        try:
            if self.db_executor is None:
                return self.repository.fetch_scope(**kwargs)
            return await self.db_executor.run(self.repository.fetch_scope, **kwargs)
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(
                503,
                "DB_QUERY_FAILED",
                "failed to fetch document metadata for routing",
                retryable=True,
            ) from exc


def _all_document_candidates(groups, documents) -> dict[str, PrefilterResult]:
    return {
        group.group_ref: PrefilterResult(
            group_ref=group.group_ref,
            candidates=[
                PrefilterCandidate(document=document, keyword_score=0.0, components={})
                for document in documents
            ],
            rejected_count=0,
        )
        for group in groups
    }
