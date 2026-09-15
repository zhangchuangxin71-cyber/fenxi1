from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from langgraph_sdk.errors import ConflictError, NotFoundError

from app.adapter.history import compact_history
from app.adapter.state import (
    PendingInteraction,
    artifact_stage_for_workflow_stage,
    pending_interaction,
    state_values,
)
from app.api.schemas import ClarificationSelection, ConflictSelection, ResponseRequest
from app.artifacts.models import ArticleArtifact, RevisionCreate
from app.artifacts.repository import ArtifactRepository
from app.config import AppSettings
from app.core.errors import AppError
from app.core.ids import prefixed_id, thread_id_for_session
from app.core.recovery import error_with_recovery, recovery_action
from app.core.requests import is_pure_continue
from app.events.normalizer import AgentEventNormalizer, artifact_event, interrupt_event, terminal_error
from app.events.responses import ResponseEventBuilder, response_snapshot, sse
from app.llm.gateway import LLMGateway
from app.llm.inputs import PendingFormInput, PendingPreflightInput, render_input
from app.llm.schemas import PreflightOutput
from app.prompts.system import PENDING_PREFLIGHT_SYSTEM_PROMPT
from app.runtime.admission import RunAdmissionController, RunPermit
from app.runtime.client import GraphRuntimeClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StreamPlan:
    kind: Literal["runtime", "replay"]
    artifact: ArticleArtifact
    thread_id: str
    runtime_run_id: str | None = None
    pending: PendingInteraction | None = None
    replay_reason: str = ""
    replay_stage: str | None = None
    replay_content: Any = None
    prelude_text: str = ""
    runtime_parts: AsyncIterator[Any] | None = None
    typing_delay_seconds: float = 0.0


class AdapterService:
    def __init__(
        self,
        *,
        settings: AppSettings,
        artifacts: ArtifactRepository,
        runtime: GraphRuntimeClient,
        llm: LLMGateway,
        admission: RunAdmissionController,
    ) -> None:
        self.settings = settings
        self.artifacts = artifacts
        self.runtime = runtime
        self.llm = llm
        self.admission = admission
        self.normalizer = AgentEventNormalizer()
        self._monitors: set[asyncio.Task[None]] = set()

    async def admit(self, request: ResponseRequest) -> StreamPlan:
        thread_id = thread_id_for_session(
            self.settings.wechat_agent_thread_namespace, request.context.session_id
        )
        await self.runtime.ensure_thread(thread_id)
        state = await self._state_or_empty(thread_id)
        pending = pending_interaction(state)
        latest = await self.artifacts.latest(request.context.session_id)

        # An explicit cancel invalidates every interaction from that revision. Some
        # Agent Server versions may still expose the consumed interrupt in the latest
        # state snapshot, so artifact status must take precedence over that snapshot.
        if latest is not None and latest.status == "cancelling":
            raise AppError(409, "RUN_CANCELLING", "The current workflow is still cancelling.", True)
        if latest is not None and latest.status == "cancelled":
            if request.context.hitl is not None:
                raise AppError(409, "STALE_INTERRUPT", "The pending interaction is stale.")
            return await self._start_revision(
                request,
                thread_id,
                latest,
                state,
                prelude_text="已取消的待审批结果不会恢复。我会根据当前需求重新规划生成流程。",
            )
        if request.context.hitl is not None:
            return await self._resume_explicit(request, thread_id, latest, pending)
        if pending is not None:
            return await self._pending_free_input(request, thread_id, latest, pending)
        if latest is None:
            return await self._start_revision(request, thread_id, None, state)
        if latest.status == "running":
            raise AppError(
                409,
                "RUN_IN_PROGRESS",
                "The workflow is still running. Retry after it reaches a review or terminal state.",
                True,
                details={"response_id": latest.current_response_id},
            )
        if is_pure_continue(request.latest_user_text):
            if latest.status == "completed":
                return self._replay_final(latest, thread_id)
            if latest.status == "failed":
                action = recovery_action(latest.last_error)
                if action == "resume_checkpoint":
                    return await self._retry_failed(request, thread_id, latest)
                if action == "new_revision":
                    return await self._start_revision(
                        request,
                        thread_id,
                        latest,
                        state,
                        prelude_text=(
                            "上一次运行已达到本轮尝试上限。我会创建新的 revision，"
                            "并根据当前对话重新确定生成入口。"
                        ),
                    )
                error = latest.last_error or {}
                return StreamPlan(
                    kind="replay",
                    artifact=latest,
                    thread_id=thread_id,
                    replay_reason=(
                        f"上一次运行因“{error.get('message') or error.get('code') or '输入或数据问题'}”"
                        "而停止。请先修正请求或文档条件，再发起新一轮生成。"
                    ),
                )
        return await self._start_revision(request, thread_id, latest, state)

    async def stream(self, plan: StreamPlan) -> AsyncIterator[str]:
        if plan.kind == "replay":
            async for event in self._stream_replay(plan):
                yield event
            return
        assert plan.runtime_run_id is not None
        parts = plan.runtime_parts or self.runtime.stream_run(plan.thread_id, plan.runtime_run_id)
        try:
            async for event in self.normalizer.stream(
                parts=parts,
                artifact=plan.artifact,
                final_artifact=lambda: self._require_artifact(plan.artifact.artifact_id),
                final_state=lambda: self._state_or_empty(plan.thread_id),
                runtime_status=lambda: self.runtime.run(plan.thread_id, plan.runtime_run_id or ""),
                prelude_text=plan.prelude_text,
                typing_delay_seconds=plan.typing_delay_seconds,
                include_debug_artifacts=plan.typing_delay_seconds > 0,
                include_debug_trace=self.settings.debug_enabled and plan.typing_delay_seconds > 0,
            ):
                yield event
        except asyncio.CancelledError:
            # Closing the product SSE intentionally does not cancel the Agent Server run.
            raise
        except Exception as exc:
            try:
                current = await self._require_artifact(plan.artifact.artifact_id)
                state = await self._state_or_empty(plan.thread_id)
                run = await self.runtime.run(plan.thread_id, plan.runtime_run_id or "")
                error = terminal_error(current, state, run)
            except Exception:
                error = _runtime_error(exc)
            builder = ResponseEventBuilder(
                response_id=plan.artifact.current_response_id, run_id=plan.artifact.run_id
            )
            snapshot = response_snapshot(
                response_id=plan.artifact.current_response_id,
                model="wechat-article-agent",
                status="failed",
                run_id=plan.artifact.run_id,
                artifact_id=plan.artifact.artifact_id,
                revision=plan.artifact.revision,
                workflow_status="failed",
                error=error,
            )
            yield sse("response.failed", {**builder.common(), "response": snapshot})
            yield "data: [DONE]\n\n"

    async def cancel(self, *, session_id: str, response_id: str) -> tuple[ArticleArtifact, int]:
        thread_id = thread_id_for_session(self.settings.wechat_agent_thread_namespace, session_id)
        pending: PendingInteraction | None = None
        runtime_run_id: str | None = None
        artifact: ArticleArtifact
        async with self.artifacts.session_lock(thread_id) as connection:
            current = await self.artifacts.latest(session_id, connection=connection)
            if current is None:
                raise AppError(404, "RESPONSE_NOT_FOUND", "The response does not exist.")
            if current.current_response_id != response_id:
                raise AppError(409, "STALE_RESPONSE", "The response no longer controls this workflow.")
            if current.status == "cancelled":
                return current, 200
            if current.status not in {"running", "waiting_for_input", "cancelling"}:
                raise AppError(409, "RESPONSE_NOT_CANCELLABLE", "The response is already terminal.")
            state = await self._state_or_empty(thread_id)
            pending = pending_interaction(state)
            artifact = await self.artifacts.begin_cancel(
                current.artifact_id,
                response_id=response_id,
                connection=connection,
            )
            if current.status in {"running", "cancelling"}:
                runtime_run_id = current.active_runtime_run_id

        try:
            if runtime_run_id:
                await asyncio.wait_for(
                    self.runtime.cancel_run(thread_id, runtime_run_id, wait=True),
                    timeout=self.settings.cancel_confirm_timeout_seconds,
                )
                artifact = await self.artifacts.finish_cancel(artifact.artifact_id, response_id=response_id)
                return artifact, 200
            if pending is not None:
                run = await self.runtime.create_run(
                    thread_id,
                    resume={"type": "cancel"},
                    context={
                        "conversation": [],
                        "compact_history": [],
                        "debug_enabled": False,
                        "response_id": response_id,
                    },
                )
                runtime_run_id = str(run["run_id"])
                await self.artifacts.set_cancelling_runtime(
                    artifact.artifact_id,
                    response_id=response_id,
                    runtime_run_id=runtime_run_id,
                )
                await asyncio.wait_for(
                    self.runtime.join(thread_id, runtime_run_id),
                    timeout=self.settings.cancel_confirm_timeout_seconds,
                )
                refreshed = await self._require_artifact(artifact.artifact_id)
                if refreshed.status == "cancelling":
                    refreshed = await self.artifacts.finish_cancel(
                        refreshed.artifact_id, response_id=response_id
                    )
                return refreshed, 200
            artifact = await self.artifacts.finish_cancel(artifact.artifact_id, response_id=response_id)
            return artifact, 200
        except TimeoutError:
            refreshed = await self._require_artifact(artifact.artifact_id)
            return refreshed, _cancel_confirmation_status(refreshed.status)
        except Exception:
            refreshed = await self._require_artifact(artifact.artifact_id)
            if refreshed.status == "cancelled":
                return refreshed, _cancel_confirmation_status(refreshed.status)
            logger.exception("cancel confirmation failed", extra={"artifact_id": artifact.artifact_id})
            return refreshed, 202

    async def close(self) -> None:
        # These tasks only observe Agent Server runs. Cancelling the local watchers
        # leaves the remote runs alive, which is the required shutdown/disconnect behavior.
        monitors = tuple(self._monitors)
        for task in monitors:
            task.cancel()
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)

    async def _resume_explicit(
        self,
        request: ResponseRequest,
        thread_id: str,
        latest: ArticleArtifact | None,
        pending: PendingInteraction | None,
    ) -> StreamPlan:
        hitl = request.context.hitl
        assert hitl is not None
        if latest is None or pending is None:
            raise AppError(409, "STALE_INTERRUPT", "There is no pending interaction to resume.")
        self._validate_pending(request, latest, pending)
        clarification_stages = {"intent_clarification", "docs_research_clarification"}
        conflict_stage = "material_conflict_review"
        if pending.stage in clarification_stages | {conflict_stage} and hitl.selection is None:
            if pending.stage == "docs_research_clarification":
                raise AppError(
                    422,
                    "RESEARCH_DIRECTION_REQUIRED",
                    "Select a recommended research direction or provide a custom about and target.",
                    stage="docs_research",
                )
            if pending.stage == conflict_stage:
                raise AppError(
                    422,
                    "CONFLICT_RESOLUTION_REQUIRED",
                    "Select how the conflicting material should be resolved.",
                    stage="docs_research",
                )
            raise AppError(
                422,
                "INTENT_TOPIC_REQUIRED",
                "Select a recommended article topic or provide a custom topic.",
                stage=pending.stage,
            )
        if pending.stage not in clarification_stages | {conflict_stage} and hitl.selection is not None:
            raise AppError(
                422,
                "UNEXPECTED_RESEARCH_DIRECTION",
                "A clarification selection is only valid for an intent or document clarification.",
                stage=pending.stage,
            )
        if hitl.selection is not None:
            selection = hitl.selection
            if pending.stage == conflict_stage and not isinstance(selection, ConflictSelection):
                raise AppError(
                    422,
                    "INVALID_CONFLICT_SELECTION",
                    "Conflict review requires a conflict resolution option.",
                    stage="docs_research",
                )
            if pending.stage in clarification_stages and not isinstance(selection, ClarificationSelection):
                raise AppError(
                    422,
                    "INVALID_CLARIFICATION_SELECTION",
                    "This clarification requires an article topic or research direction.",
                    stage=pending.stage,
                )
            if (
                pending.stage == "intent_clarification"
                and isinstance(selection, ClarificationSelection)
                and (selection.about or selection.target)
            ):
                raise AppError(
                    422,
                    "INVALID_INTENT_SELECTION",
                    "Intent clarification accepts a topic, not a research direction.",
                    stage=pending.stage,
                )
            if (
                pending.stage == "docs_research_clarification"
                and isinstance(selection, ClarificationSelection)
                and selection.topic
            ):
                raise AppError(
                    422,
                    "INVALID_RESEARCH_DIRECTION",
                    "Document clarification accepts about and target, not an intent topic.",
                    stage=pending.stage,
                )
        return await self._submit_resume(
            request,
            thread_id,
            latest,
            pending,
            decision=hitl.decision,
            feedback=hitl.feedback,
            selection=hitl.selection.model_dump(exclude_defaults=True) if hitl.selection else None,
        )

    async def _pending_free_input(
        self,
        request: ResponseRequest,
        thread_id: str,
        latest: ArticleArtifact | None,
        pending: PendingInteraction,
    ) -> StreamPlan:
        if latest is None:
            raise AppError(409, "STATE_ARTIFACT_MISMATCH", "Pending state has no artifact.")
        if is_pure_continue(request.latest_user_text):
            return self._replay_pending(latest, thread_id, pending, "当前正等待您审批，已重新展示待办内容。")
        decision = await self._classify_pending(request, latest, pending)
        if decision.action == "replay" or decision.confidence < 0.7:
            return self._replay_pending(latest, thread_id, pending, decision.reason)
        if decision.action == "supersede":
            return await self._supersede_and_start(request, thread_id, latest, pending, decision.reason)
        if pending.stage in {
            "intent_clarification",
            "docs_research_clarification",
            "material_conflict_review",
        }:
            messages = {
                "intent_clarification": "请在意图确认卡片中选择一个建议主题，或填写其他文章主题。",
                "docs_research_clarification": (
                    "请在素材搜集方向卡片中选择一个推荐方向，或填写自定义的素材主题与关注方面。"
                ),
                "material_conflict_review": "请在素材冲突卡片中选择处理方式；如选择自定义，请补充处理意见。",
            }
            return self._replay_pending(
                latest,
                thread_id,
                pending,
                messages[pending.stage],
            )
        mapped = {
            "approve_current": "approve",
            "revise_current": "revise",
            "regenerate_current": "regenerate",
        }[decision.action]
        return await self._submit_resume(
            request,
            thread_id,
            latest,
            pending,
            decision=mapped,
            feedback=decision.feedback,
            prelude_text=decision.reason,
        )

    async def _classify_pending(
        self, request: ResponseRequest, artifact: ArticleArtifact, pending: PendingInteraction
    ) -> PreflightOutput:
        try:
            return await self.llm.structured(
                run_id=artifact.run_id,
                phase="pending_preflight",
                system_prompt=PENDING_PREFLIGHT_SYSTEM_PROMPT,
                input_text=render_input(
                    PendingPreflightInput(
                        stage=pending.stage,
                        form=PendingFormInput.model_validate(pending.form),
                        artifact_summary=_stage_content(artifact, pending.stage),
                        latest_user_input=request.latest_user_text,
                    )
                ),
                output_model=PreflightOutput,
            )
        except AppError:
            return PreflightOutput(
                action="replay",
                reason="我没有足够把握将这条消息解读为审批，已保留并重新展示当前结果。",
                feedback="",
                confidence=0,
            )

    async def _submit_resume(
        self,
        request: ResponseRequest,
        thread_id: str,
        latest: ArticleArtifact,
        pending: PendingInteraction,
        *,
        decision: str,
        feedback: str,
        selection: dict[str, str] | None = None,
        prelude_text: str = "",
    ) -> StreamPlan:
        permit = await self.admission.acquire()
        response_id = prefixed_id("resp")
        run_id: str | None = None
        runtime_parts: AsyncIterator[Any] | None = None
        try:
            async with self.artifacts.session_lock(thread_id) as connection:
                current = await self.artifacts.latest(request.context.session_id, connection=connection)
                state = await self._state_or_empty(thread_id)
                current_pending = pending_interaction(state)
                if current is None or current_pending is None:
                    raise AppError(409, "STALE_INTERRUPT", "The pending interaction is no longer current.")
                self._validate_pending(request, current, current_pending, allow_free_input=True)
                run_id, runtime_parts = await self.runtime.start_stream(
                    thread_id,
                    resume={
                        "decision": decision,
                        "feedback": feedback,
                        "selection": selection,
                        "response_id": response_id,
                    },
                    context=self._runtime_context(request, response_id=response_id),
                )
                updated = await self.artifacts.set_runtime_run(
                    current.artifact_id,
                    expected_response_id=current.current_response_id,
                    response_id=response_id,
                    runtime_run_id=run_id,
                    connection=connection,
                )
        except Exception:
            await permit.release()
            if run_id is not None:
                await self._compensating_cancel(thread_id, run_id)
            raise
        self._monitor(thread_id, run_id, updated.artifact_id, response_id, permit=permit)
        await self.runtime.refresh_thread_ttl(thread_id)
        return StreamPlan(
            kind="runtime",
            artifact=updated,
            thread_id=thread_id,
            runtime_run_id=run_id,
            prelude_text=prelude_text,
            runtime_parts=runtime_parts,
            typing_delay_seconds=self._typing_delay(request),
        )

    async def _supersede_and_start(
        self,
        request: ResponseRequest,
        thread_id: str,
        latest: ArticleArtifact,
        pending: PendingInteraction,
        reason: str,
    ) -> StreamPlan:
        permit = await self.admission.acquire()
        run: Mapping[str, Any] | None = None
        try:
            async with self.artifacts.session_lock(thread_id) as connection:
                current = await self.artifacts.latest(request.context.session_id, connection=connection)
                state = await self._state_or_empty(thread_id)
                current_pending = pending_interaction(state)
                if current is None or current_pending is None:
                    raise AppError(409, "STALE_INTERRUPT", "The pending interaction is no longer current.")
                if (
                    current.artifact_id != latest.artifact_id
                    or current_pending.interrupt_id != pending.interrupt_id
                ):
                    raise AppError(409, "STALE_INTERRUPT", "The pending interaction changed concurrently.")
                run = await self.runtime.create_run(
                    thread_id,
                    resume={"type": "supersede"},
                    context=self._runtime_context(request, response_id=current.current_response_id),
                )
                await self.artifacts.set_runtime_run(
                    current.artifact_id,
                    expected_response_id=current.current_response_id,
                    response_id=current.current_response_id,
                    runtime_run_id=str(run["run_id"]),
                    connection=connection,
                )
            await self.runtime.join(thread_id, str(run["run_id"]))
        except Exception as exc:
            if run is not None:
                await self._compensating_cancel(thread_id, str(run["run_id"]))
            raise AppError(
                503, "SUPERSEDE_FAILED", "Could not supersede the pending workflow.", True
            ) from exc
        finally:
            await permit.release()
        refreshed = await self._require_artifact(latest.artifact_id)
        if refreshed.status != "superseded":
            raise AppError(409, "SUPERSEDE_FAILED", "The old workflow did not reach superseded state.")
        state = await self._state_or_empty(thread_id)
        return await self._start_revision(request, thread_id, refreshed, state, prelude_text=reason)

    async def _start_revision(
        self,
        request: ResponseRequest,
        thread_id: str,
        previous: ArticleArtifact | None,
        state: Mapping[str, Any],
        *,
        prelude_text: str = "",
    ) -> StreamPlan:
        values = state_values(state)
        user_id = request.context.user_id or str(values.get("user_id") or "")
        kb_id = request.context.kb_id or str(values.get("kb_id") or "")
        doc_ids = (
            request.context.doc_ids
            if request.context.doc_ids is not None
            else (previous.doc_ids if previous else [])
        )
        temp_doc_ids = (
            request.context.temp_doc_ids
            if request.context.temp_doc_ids is not None
            else (previous.temp_doc_ids if previous else [])
        )
        if not user_id or not kb_id:
            raise AppError(422, "DOCUMENT_CONTEXT_REQUIRED", "user_id and kb_id are required.")
        permit = await self.admission.acquire()
        artifact_id, run_id, response_id = (
            prefixed_id("art"),
            prefixed_id("run"),
            prefixed_id("resp"),
        )
        runtime_run_id: str | None = None
        runtime_parts: AsyncIterator[Any] | None = None
        try:
            async with self.artifacts.session_lock(thread_id) as connection:
                current = await self.artifacts.latest(request.context.session_id, connection=connection)
                expected_id = previous.artifact_id if previous else None
                if (current.artifact_id if current else None) != expected_id:
                    raise AppError(409, "SESSION_CHANGED", "The session changed concurrently.")
                artifact = await self.artifacts.create_revision(
                    RevisionCreate(
                        artifact_id=artifact_id,
                        session_id=request.context.session_id,
                        run_id=run_id,
                        current_response_id=response_id,
                        parent_artifact_id=previous.artifact_id if previous else None,
                        entry_stage="docs_research",
                        doc_ids=doc_ids,
                        temp_doc_ids=temp_doc_ids,
                    ),
                    connection=connection,
                )
                graph_input = self._initial_state(
                    request,
                    artifact,
                    previous=previous,
                    user_id=user_id,
                    kb_id=kb_id,
                    doc_ids=doc_ids,
                    temp_doc_ids=temp_doc_ids,
                )
                runtime_run_id, runtime_parts = await self.runtime.start_stream(
                    thread_id,
                    input=graph_input,
                    context=self._runtime_context(request, response_id=response_id),
                )
                artifact = await self.artifacts.set_runtime_run(
                    artifact.artifact_id,
                    expected_response_id=response_id,
                    response_id=response_id,
                    runtime_run_id=runtime_run_id,
                    connection=connection,
                )
        except ConflictError as exc:
            await permit.release()
            if runtime_run_id is not None:
                await self._compensating_cancel(thread_id, runtime_run_id)
            raise AppError(
                409, "RUN_IN_PROGRESS", "Another run already controls this session.", True
            ) from exc
        except Exception:
            await permit.release()
            if runtime_run_id is not None:
                await self._compensating_cancel(thread_id, runtime_run_id)
            raise
        assert runtime_run_id is not None
        self._monitor(thread_id, runtime_run_id, artifact.artifact_id, response_id, permit=permit)
        return StreamPlan(
            kind="runtime",
            artifact=artifact,
            thread_id=thread_id,
            runtime_run_id=runtime_run_id,
            prelude_text=prelude_text,
            runtime_parts=runtime_parts,
            typing_delay_seconds=self._typing_delay(request),
        )

    async def _retry_failed(
        self, request: ResponseRequest, thread_id: str, latest: ArticleArtifact
    ) -> StreamPlan:
        permit = await self.admission.acquire()
        response_id = prefixed_id("resp")
        runtime_run_id: str | None = None
        runtime_parts: AsyncIterator[Any] | None = None
        try:
            async with self.artifacts.session_lock(thread_id) as connection:
                current = await self.artifacts.latest(request.context.session_id, connection=connection)
                if current is None or current.artifact_id != latest.artifact_id or current.status != "failed":
                    raise AppError(409, "SESSION_CHANGED", "The failed workflow changed concurrently.")
                runtime_run_id, runtime_parts = await self.runtime.start_stream(
                    thread_id,
                    state_update={
                        "response_id": response_id,
                        "status": "running",
                        "generation_attempts": {},
                    },
                    context=self._runtime_context(request, response_id=response_id),
                )
                updated = await self.artifacts.retry_failed(
                    current.artifact_id,
                    expected_response_id=current.current_response_id,
                    response_id=response_id,
                    runtime_run_id=runtime_run_id,
                    connection=connection,
                )
        except Exception:
            await permit.release()
            if runtime_run_id is not None:
                await self._compensating_cancel(thread_id, runtime_run_id)
            raise
        assert runtime_run_id is not None
        self._monitor(thread_id, runtime_run_id, updated.artifact_id, response_id, permit=permit)
        await self.runtime.refresh_thread_ttl(thread_id)
        return StreamPlan(
            kind="runtime",
            artifact=updated,
            thread_id=thread_id,
            runtime_run_id=runtime_run_id,
            prelude_text="已按您的明确指令，从最近失败的检查点重试。",
            runtime_parts=runtime_parts,
            typing_delay_seconds=self._typing_delay(request),
        )

    async def _stream_replay(self, plan: StreamPlan) -> AsyncIterator[str]:
        artifact = plan.artifact
        await self.artifacts.refresh_ttl(artifact.artifact_id)
        try:
            await self.runtime.refresh_thread_ttl(plan.thread_id)
        except Exception as exc:
            # Artifact cleanup deletes the LangGraph thread before business data, so
            # a transient runtime TTL refresh failure must not hide a replayable result.
            logger.warning("Could not refresh replayed thread TTL", exc_info=exc)
        builder = ResponseEventBuilder(response_id=artifact.current_response_id, run_id=artifact.run_id)
        snapshot = response_snapshot(
            response_id=artifact.current_response_id,
            model="wechat-article-agent",
            status="in_progress",
            run_id=artifact.run_id,
            artifact_id=artifact.artifact_id,
            revision=artifact.revision,
            workflow_status=artifact.status,
            pending_interrupt_id=plan.pending.interrupt_id if plan.pending else None,
        )
        yield sse("response.created", {"response": snapshot})
        if plan.replay_reason:
            for chunk in _typing_chunks(plan.replay_reason):
                for event in builder.text_delta(chunk):
                    yield event
        if plan.replay_stage and plan.replay_content is not None:
            yield artifact_event(
                artifact,
                stage=plan.replay_stage,
                content=plan.replay_content,
                status="pending_review" if plan.pending else "completed",
            )
        if plan.pending:
            yield interrupt_event(
                plan.pending.raw,
                response_id=artifact.current_response_id,
                run_id=artifact.run_id,
            )
        for event in builder.finish_content():
            yield event
        completed = response_snapshot(
            response_id=artifact.current_response_id,
            model="wechat-article-agent",
            status="completed" if artifact.status != "failed" else "failed",
            run_id=artifact.run_id,
            artifact_id=artifact.artifact_id,
            revision=artifact.revision,
            workflow_status="waiting_for_input" if plan.pending else artifact.status,
            pending_interrupt_id=plan.pending.interrupt_id if plan.pending else None,
            error=artifact.last_error if artifact.status == "failed" else None,
        )
        yield sse(
            "response.failed" if artifact.status == "failed" else "response.completed",
            {"response": completed},
        )
        yield "data: [DONE]\n\n"

    def _replay_pending(
        self,
        artifact: ArticleArtifact,
        thread_id: str,
        pending: PendingInteraction,
        reason: str,
    ) -> StreamPlan:
        replay_content = _stage_content(artifact, pending.stage)
        if pending.stage == "material_conflict_review":
            replay_content = {"conflicts": pending.form.get("fields") or []}
        return StreamPlan(
            kind="replay",
            artifact=artifact,
            thread_id=thread_id,
            pending=pending,
            replay_reason=reason,
            replay_stage=artifact_stage_for_workflow_stage(pending.stage),
            replay_content=replay_content,
        )

    @staticmethod
    def _replay_final(artifact: ArticleArtifact, thread_id: str) -> StreamPlan:
        return StreamPlan(
            kind="replay",
            artifact=artifact,
            thread_id=thread_id,
            replay_reason="已重新展示上一次完成的排版结果。",
            replay_stage="final_html",
            replay_content=artifact.final_html,
        )

    def _validate_pending(
        self,
        request: ResponseRequest,
        artifact: ArticleArtifact,
        pending: PendingInteraction,
        *,
        allow_free_input: bool = False,
    ) -> None:
        hitl = request.context.hitl
        expected_response = request.previous_response_id if hitl is not None else pending.response_id
        expected_interrupt = hitl.interrupt_id if hitl is not None else pending.interrupt_id
        if (
            artifact.status != "waiting_for_input"
            or artifact.current_response_id != pending.response_id
            or artifact.artifact_id != pending.artifact_id
            or artifact.revision != pending.revision
            or expected_response != pending.response_id
            or expected_interrupt != pending.interrupt_id
        ):
            raise AppError(409, "STALE_INTERRUPT", "The pending interaction is stale.")
        if not allow_free_input and hitl is None:
            raise AppError(409, "STALE_INTERRUPT", "A structured HITL decision is required.")

    def _runtime_context(self, request: ResponseRequest, *, response_id: str) -> dict[str, Any]:
        conversation = request.conversation
        return {
            "conversation": conversation,
            "compact_history": compact_history(conversation),
            "debug_enabled": bool(self.settings.debug_enabled and request.context.debug),
            "response_id": response_id,
        }

    def _typing_delay(self, request: ResponseRequest) -> float:
        # Structured calls expose public_text only after strict JSON validation. Pace
        # debug streams so the development panel can visibly render each delta before
        # the complete artifact or interrupt payload arrives. Product streams remain
        # unchanged.
        return 0.025 if self.settings.debug_enabled and request.context.debug else 0.0

    def _initial_state(
        self,
        request: ResponseRequest,
        artifact: ArticleArtifact,
        *,
        previous: ArticleArtifact | None = None,
        user_id: str,
        kb_id: str,
        doc_ids: list[str],
        temp_doc_ids: list[str],
    ) -> dict[str, Any]:
        return {
            "session_id": request.context.session_id,
            "user_id": user_id,
            "kb_id": kb_id,
            "run_id": artifact.run_id,
            "response_id": artifact.current_response_id,
            "revision": artifact.revision,
            "intent": "",
            "has_prior_article_revision": bool(
                previous is not None and previous.current_stage != "non_wechat"
            ),
            "intent_clarification_used": False,
            "intent_topic_options": [],
            "entry_stage": "docs_research",
            "route_reason": "",
            "previous_revision_status": previous.status if previous else "",
            "current_user_input": request.latest_user_text,
            "session_memory": {
                "docs_research": "",
                "task_spec": "",
                "outline": "",
                "article": "",
                "ai_image": "",
                "html_layout": "",
            },
            "doc_ids": doc_ids,
            "temp_doc_ids": temp_doc_ids,
            "relevant_doc_ids": [],
            "document_coverage_mode": "best_effort",
            "artifact_id": artifact.artifact_id,
            "current_stage": "intent_router",
            "generation_attempts": {},
            "clarification_round": 0,
            "status": "running",
            "review_action": "",
            "review_feedback": "",
            "pending_interrupt_id": "",
            "research_direction_options": [],
            "research_direction": {},
            "confirmed_requirements": "",
            "document_meta": [],
            "retrieval_warnings": [],
            "need_web_search": False,
            "web_info_overview": "",
            "material_conflicts": [],
            "conflict_resolution": "",
            "conflict_feedback": "",
            "document_material_status": "pending",
            "web_material_status": "pending",
        }

    def _monitor(
        self,
        thread_id: str,
        runtime_run_id: str,
        artifact_id: str,
        response_id: str,
        *,
        permit: RunPermit | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._monitor_run(thread_id, runtime_run_id, artifact_id, response_id, permit=permit),
            name=f"monitor:{runtime_run_id}",
        )
        self._monitors.add(task)
        task.add_done_callback(self._monitors.discard)

    async def _monitor_run(
        self,
        thread_id: str,
        runtime_run_id: str,
        artifact_id: str,
        response_id: str,
        *,
        permit: RunPermit | None = None,
    ) -> None:
        try:
            await self.runtime.join(thread_id, runtime_run_id)
            run = await self.runtime.run(thread_id, runtime_run_id)
            current = await self._require_artifact(artifact_id)
            if current.current_response_id != response_id:
                return
            if current.status == "cancelling":
                await self.artifacts.finish_cancel(artifact_id, response_id=response_id)
            elif current.status == "running" and str(run.get("status")) in {
                "error",
                "timeout",
                "interrupted",
            }:
                state = await self._state_or_empty(thread_id)
                await self.artifacts.mark_terminal(
                    artifact_id,
                    response_id=response_id,
                    status="failed",
                    current_stage=current.current_stage,
                    last_error=terminal_error(current, state, run),
                )
            elif current.status == "running" and str(run.get("status")) == "success":
                state = await self._state_or_empty(thread_id)
                values = state_values(state)
                if values.get("status") == "completed":
                    await self.artifacts.mark_terminal(
                        artifact_id,
                        response_id=response_id,
                        status="completed",
                        current_stage=str(values.get("current_stage") or "completed"),
                    )
        except Exception:
            logger.exception(
                "runtime monitor failed",
                extra={"artifact_id": artifact_id, "runtime_run_id": runtime_run_id},
            )
        finally:
            if permit is not None:
                await permit.release()
            try:
                await self.runtime.refresh_thread_ttl(thread_id)
            except Exception:
                logger.exception("thread TTL refresh failed", extra={"thread_id": thread_id})

    async def _state_or_empty(self, thread_id: str) -> Mapping[str, Any]:
        try:
            return await self.runtime.state(thread_id)
        except NotFoundError:
            return {}

    async def _require_artifact(self, artifact_id: str) -> ArticleArtifact:
        artifact = await self.artifacts.get(artifact_id)
        if artifact is None:
            raise AppError(404, "ARTIFACT_NOT_FOUND", "The article artifact does not exist.")
        return artifact

    async def _compensating_cancel(self, thread_id: str, runtime_run_id: str) -> None:
        try:
            await self.runtime.cancel_run(thread_id, runtime_run_id, wait=True)
        except Exception:
            logger.exception(
                "compensating Agent Server cancel failed",
                extra={"runtime_run_id": runtime_run_id},
            )


def _stage_content(artifact: ArticleArtifact, stage: str) -> Any:
    return {
        "task_spec_review": artifact.task_spec,
        "outline_review": (artifact.outline or {}).get("markdown") if artifact.outline else None,
        "article_review": artifact.article_markdown,
        "docs_research_clarification": None,
    }.get(stage)


def _typing_chunks(text: str, size: int = 8) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]


def _runtime_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, AppError):
        return error_with_recovery({"code": exc.code, "message": exc.message, "retryable": exc.retryable})
    return error_with_recovery(
        {"code": "STREAM_RUNTIME_ERROR", "message": str(exc)[:1000], "retryable": True}
    )


def _cancel_confirmation_status(status: str) -> int:
    return 200 if status == "cancelled" else 202
