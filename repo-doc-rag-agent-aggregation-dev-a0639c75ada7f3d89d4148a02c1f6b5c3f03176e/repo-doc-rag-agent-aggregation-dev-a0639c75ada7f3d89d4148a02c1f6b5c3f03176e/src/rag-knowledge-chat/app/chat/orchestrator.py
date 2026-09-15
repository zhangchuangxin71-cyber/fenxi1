from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.api.contracts import ChatCompletionRequest
from app.chat.events import (
    ChatEvent,
    CompleteEvent,
    ErrorEvent,
    ReasoningDeltaEvent,
    StatusEvent,
    TextDeltaEvent,
)
from app.chat.evidence import EvidenceBundle, build_evidence, build_references
from app.chat.models import AnswerBasis, RetrievalResult, RetrievalStatus
from app.chat.prompts import build_answer_messages, build_route_input_messages
from app.chat.query_rewrite import downgrade_unresolved_route_queries
from app.integrations.ark import AnswerReasoningDelta, AnswerTextDelta, AnswerThinkingStarted
from app.platform.errors import ApiError
from app.platform.trace import TraceCollector

GENERAL_NOTICE = "本次回答未检索知识库，以下内容基于模型通识生成：\n\n"
NO_RESULT_NOTICE = "知识库中未检索到相关内容，以下内容由模型基于通识生成：\n\n"
RETRIEVAL_ERROR_NOTICE = "知识库检索暂时不可用，以下内容由模型基于通识生成：\n\n"


class ChatOrchestrator:
    def __init__(
        self,
        *,
        ark: Any,
        retrieval: Any,
        ark_limiter: Any,
        ark_max_concurrency: int = 4,
        debug_enabled: bool = True,
        deadline_seconds: float = 90.0,
    ) -> None:
        self._ark = ark
        self._retrieval = retrieval
        self._ark_limiter = ark_limiter
        self._semaphore = asyncio.Semaphore(ark_max_concurrency)
        self._debug_enabled = debug_enabled
        self._deadline_seconds = deadline_seconds

    async def run(
        self,
        *,
        request: ChatCompletionRequest,
        request_id: str,
        trace: TraceCollector,
        first_ark_permit_acquired: bool = False,
    ) -> AsyncIterator[ChatEvent]:
        try:
            async with asyncio.timeout(self._deadline_seconds):
                async for event in self._run(
                    request=request,
                    request_id=request_id,
                    trace=trace,
                    first_ark_permit_acquired=first_ark_permit_acquired,
                ):
                    yield event
        except TimeoutError:
            trace.record("request_deadline_exceeded")
            yield ErrorEvent(
                code="request_deadline_exceeded",
                message="问答请求超过总处理时限",
                retryable=True,
                debug={"trace": trace.snapshot()} if trace.enabled else None,
            )

    async def _run(
        self,
        *,
        request: ChatCompletionRequest,
        request_id: str,
        trace: TraceCollector,
        first_ark_permit_acquired: bool = False,
    ) -> AsyncIterator[ChatEvent]:
        try:
            yield StatusEvent(stage="route", message="正在判断是否需要检索知识库")
            trace.record("route_started")
            if not first_ark_permit_acquired:
                await self._ark_limiter.acquire("ark")
            document_count = len(request.rag.doc_ids) + len(request.rag.temp_doc_ids)
            incremental_document_names: list[str] = []
            if request.rag.incremental_doc_ids:
                trace.record(
                    "incremental_document_metadata_started",
                    document_count=len(request.rag.incremental_doc_ids),
                )
                incremental_document_names = await self._retrieval.get_document_names(
                    user_id=request.rag.user_id,
                    kb_id=request.rag.kb_id,
                    requested_doc_ids=request.rag.incremental_doc_ids,
                    permanent_scope_ids=request.rag.doc_ids,
                    temporary_scope_ids=request.rag.temp_doc_ids,
                    session_id=request.rag.session_id,
                )
                trace.record(
                    "incremental_document_metadata_completed",
                    document_count=len(incremental_document_names),
                )
            unique_document_name: str | None = None
            if document_count == 1:
                only_document_id = (request.rag.doc_ids or request.rag.temp_doc_ids)[0]
                incremental_names_by_id = dict(
                    zip(
                        request.rag.incremental_doc_ids,
                        incremental_document_names,
                        strict=True,
                    )
                )
                unique_document_name = incremental_names_by_id.get(only_document_id)
                if unique_document_name is None:
                    trace.record("unique_document_metadata_started")
                    names = await self._retrieval.get_document_names(
                        user_id=request.rag.user_id,
                        kb_id=request.rag.kb_id,
                        requested_doc_ids=[only_document_id],
                        permanent_scope_ids=request.rag.doc_ids,
                        temporary_scope_ids=request.rag.temp_doc_ids,
                        session_id=request.rag.session_id,
                    )
                    unique_document_name = names[0]
                    trace.record("unique_document_metadata_completed")
            route_messages = build_route_input_messages(
                request.llm_messages(),
                document_count=document_count,
                incremental_document_names=incremental_document_names,
                unique_document_name=unique_document_name,
            )
            async with self._semaphore:
                decision = await self._ark.decide(route_messages)
            unique_incremental_document_name = (
                incremental_document_names[0] if len(incremental_document_names) == 1 else None
            )
            downgraded_queries = downgrade_unresolved_route_queries(
                decision,
                unique_incremental_document_name=unique_incremental_document_name,
            )
            resolved_queries = decision.query_items
            ambiguous_queries = decision.ambiguous_query_items
            trace.record(
                "route_completed",
                needs_retrieval=decision.needs_retrieval,
                reason_code=decision.reason_code,
                resolved_query_count=len(resolved_queries),
                ambiguous_query_count=len(ambiguous_queries),
                downgraded_query_count=len(downgraded_queries),
            )
        except asyncio.CancelledError:
            raise
        except ApiError as exc:
            trace.record("route_context_failed", code=exc.code)
            yield ErrorEvent(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                debug={"trace": trace.snapshot()} if trace.enabled else None,
            )
            return
        except Exception:
            trace.record("route_failed")
            yield ErrorEvent(
                code="ark_upstream_error",
                message="模型服务暂时不可用",
                retryable=True,
                debug={"trace": trace.snapshot()} if trace.enabled else None,
            )
            return

        retrieval_result = RetrievalResult(status=RetrievalStatus.NO_RESULT)
        evidence = EvidenceBundle(chunks=[], prompt_text="")
        retrieval_called = False
        if decision.needs_retrieval and resolved_queries:
            query: str | list[str] = resolved_queries
            retrieval_called = True
            yield StatusEvent(stage="retrieval", message="正在检索知识库")
            trace.record("retrieval_started", query=query)
            retrieval_result = await self._retrieval.retrieve(
                request=request,
                query=query,
                include_debug=request.rag.include_debug,
            )
            trace.record(
                "retrieval_completed",
                status=retrieval_result.status.value,
                request_id=retrieval_result.request_id,
            )
            if retrieval_result.status is RetrievalStatus.SUCCESS:
                basis = AnswerBasis.KNOWLEDGE_BASE
                evidence = build_evidence(retrieval_result.chunks)
                notice = ""
            elif retrieval_result.status is RetrievalStatus.NO_RESULT:
                basis = AnswerBasis.GENERAL_NO_RESULT
                notice = NO_RESULT_NOTICE
            else:
                basis = AnswerBasis.GENERAL_RETRIEVAL_ERROR
                notice = RETRIEVAL_ERROR_NOTICE
        else:
            basis = AnswerBasis.GENERAL_NO_RETRIEVAL
            notice = "" if decision.reason_code == "clarification" or ambiguous_queries else GENERAL_NOTICE

        clarification_required = decision.reason_code == "clarification" or (
            bool(ambiguous_queries) and not resolved_queries
        )

        yield StatusEvent(stage="generation", message="正在生成回答")
        pending_notice = notice
        trace.record("generation_started", answer_basis=basis.value)
        answer_parts: list[str] = []
        try:
            await self._ark_limiter.acquire("ark")
            answer_messages = build_answer_messages(
                request.llm_messages(),
                basis=basis,
                evidence_text=evidence.prompt_text,
                clarification_required=clarification_required,
                unresolved_queries=ambiguous_queries,
                document_count=document_count,
                incremental_document_names=incremental_document_names,
                resolved_queries=resolved_queries,
            )
            async with self._semaphore:
                first_token = True
                async for item in self._ark.stream_answer(
                    answer_messages,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                ):
                    if isinstance(item, AnswerThinkingStarted):
                        trace.record("thinking_started")
                        yield StatusEvent(stage="thinking", message="正在思考")
                        continue
                    if isinstance(item, AnswerReasoningDelta):
                        yield ReasoningDeltaEvent(delta=item.delta)
                        continue
                    if not isinstance(item, AnswerTextDelta):
                        raise TypeError("unsupported Ark answer stream event")
                    delta = item.delta
                    if first_token:
                        trace.record("first_token")
                        first_token = False
                    if pending_notice:
                        yield TextDeltaEvent(delta=pending_notice)
                        pending_notice = ""
                    answer_parts.append(delta)
                    yield TextDeltaEvent(delta=delta)
            if pending_notice:
                if first_token:
                    trace.record("first_token")
                    first_token = False
                yield TextDeltaEvent(delta=pending_notice)
        except asyncio.CancelledError:
            raise
        except ApiError as exc:
            trace.record("generation_failed", code=exc.code)
            yield ErrorEvent(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                debug={"trace": trace.snapshot()} if trace.enabled else None,
            )
            return
        except Exception:
            trace.record("generation_failed", code="ark_upstream_error")
            yield ErrorEvent(
                code="ark_upstream_error",
                message="模型服务暂时不可用",
                retryable=True,
                debug={"trace": trace.snapshot()} if trace.enabled else None,
            )
            return

        answer = "".join(answer_parts)
        references, invalid_citations = build_references(answer, evidence.chunks)
        trace.record(
            "generation_completed",
            answer_chars=len(answer),
            reference_count=len(references),
            invalid_citations=invalid_citations,
        )
        retrieval_meta: dict[str, Any] = {
            "status": retrieval_result.status.value if retrieval_called else "not_called",
            "request_id": retrieval_result.request_id,
        }
        if retrieval_result.error:
            retrieval_meta["error"] = retrieval_result.error
        debug: dict[str, Any] | None = None
        if trace.enabled:
            debug = {
                "route": decision.model_dump(),
                "retrieval": {
                    "usage": retrieval_result.usage,
                    "debug": retrieval_result.debug,
                    "error": retrieval_result.error,
                },
                "trace": trace.snapshot(),
            }
        elif retrieval_result.debug is not None:
            debug = {
                "retrieval": {
                    "usage": retrieval_result.usage,
                    "debug": retrieval_result.debug,
                    "error": retrieval_result.error,
                }
            }
        yield CompleteEvent(
            answer_basis=basis,
            retrieval=retrieval_meta,
            references=references,
            chunks=evidence.chunks,
            debug=debug,
        )
