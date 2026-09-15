from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

from app.adapter.state import pending_interaction
from app.artifacts.models import ArticleArtifact
from app.core.recovery import error_with_recovery
from app.events.responses import ResponseEventBuilder, response_snapshot, sse

PUBLIC_ARTIFACT_STAGES = frozenset(
    {
        "material_sources",
        "material_conflicts",
        "task_spec",
        "outline",
        "article_markdown",
        "images",
        "final_html",
    }
)
DEBUG_ARTIFACT_STAGES = frozenset({"session_memory", "material_library"})


class AgentEventNormalizer:
    def __init__(self, *, model: str = "wechat-article-agent") -> None:
        self.model = model

    async def stream(
        self,
        *,
        parts: AsyncIterator[Any],
        artifact: ArticleArtifact,
        final_artifact: Any,
        final_state: Any,
        runtime_status: Any,
        prelude_text: str = "",
        typing_delay_seconds: float = 0.0,
        include_debug_artifacts: bool = False,
        include_debug_trace: bool = False,
    ) -> AsyncIterator[str]:
        builder = ResponseEventBuilder(response_id=artifact.current_response_id, run_id=artifact.run_id)
        created = response_snapshot(
            response_id=artifact.current_response_id,
            model=self.model,
            status="in_progress",
            run_id=artifact.run_id,
            artifact_id=artifact.artifact_id,
            revision=artifact.revision,
            workflow_status="running",
        )
        yield sse("response.created", {"response": created})
        yield sse(
            "agent.activity",
            {
                **builder.common(),
                "schema_version": "1",
                "activity": {
                    "id": "activity_admission",
                    "kind": "node",
                    "name": "admission",
                    "label": "请求已接收，正在启动工作流",
                    "status": "completed",
                    "node": "adapter",
                    "input_summary": None,
                    "output_summary": None,
                    "error": None,
                },
            },
        )
        debug: dict[str, Any] | None = None
        usage: dict[str, Any] = {}
        observed_activity_states: set[tuple[str, str]] = set()
        if prelude_text:
            async for event in _typed_text_events(builder, prelude_text, typing_delay_seconds):
                yield event
        async for part in parts:
            event_type, data = _part(part)
            if event_type != "custom" or not isinstance(data, Mapping):
                continue
            kind = str(data.get("kind") or "")
            payload = data.get("data")
            if not isinstance(payload, Mapping):
                continue
            if kind == "public_text":
                async for event in _typed_text_events(
                    builder, str(payload.get("text") or ""), typing_delay_seconds
                ):
                    yield event
            elif kind == "reasoning_delta":
                async for event in _typed_reasoning_events(
                    builder, str(payload.get("delta") or ""), typing_delay_seconds
                ):
                    yield event
            elif kind == "activity":
                activity_state = (str(payload.get("id") or ""), str(payload.get("status") or ""))
                if activity_state in observed_activity_states:
                    continue
                observed_activity_states.add(activity_state)
                yield sse(
                    "agent.activity",
                    {**builder.common(), "schema_version": "1", "activity": dict(payload)},
                )
            elif kind == "artifact":
                artifact_payload = dict(payload)
                stage = str(artifact_payload.get("stage") or "")
                if stage not in PUBLIC_ARTIFACT_STAGES and not (
                    include_debug_artifacts and stage in DEBUG_ARTIFACT_STAGES
                ):
                    continue
                if (
                    include_debug_artifacts
                    and stage == "material_library"
                    and artifact_payload.get("content") is None
                ):
                    debug_artifact = await final_artifact()
                    artifact_payload["content"] = debug_artifact.material_library
                yield sse(
                    "agent.artifact",
                    {**builder.common(), "schema_version": "1", "artifact": artifact_payload},
                )
            elif kind == "warning":
                activity = {
                    "id": f"warning_{payload.get('code', 'degraded').lower()}",
                    "kind": "node",
                    "name": "workflow_warning",
                    "label": str(payload.get("message") or "工作流降级继续"),
                    "status": "degraded",
                    "node": "workflow",
                    "input_summary": None,
                    "output_summary": {"code": payload.get("code")},
                    "error": None,
                }
                yield sse(
                    "agent.activity",
                    {**builder.common(), "schema_version": "1", "activity": activity},
                )
            elif kind == "usage":
                _merge_usage(usage, payload)
            elif kind == "debug_snapshot" and include_debug_trace:
                debug = dict(payload)

        for event in builder.finish_content():
            yield event
        current = await final_artifact()
        state = await final_state()
        run = await runtime_status()
        pending = pending_interaction(state)
        workflow_status = _workflow_status(current, run, pending is not None)
        if pending is not None:
            yield interrupt_event(pending.raw, response_id=current.current_response_id, run_id=current.run_id)
        error = terminal_error(current, state, run) if workflow_status == "failed" else None
        snapshot = response_snapshot(
            response_id=current.current_response_id,
            model=self.model,
            status="failed" if workflow_status == "failed" else "completed",
            run_id=current.run_id,
            artifact_id=current.artifact_id,
            revision=current.revision,
            workflow_status=workflow_status,
            pending_interrupt_id=pending.interrupt_id if pending else None,
            error=error,
            usage=usage or None,
        )
        terminal = "response.failed" if workflow_status == "failed" else "response.completed"
        terminal_payload: dict[str, Any] = {"response": snapshot}
        if debug is not None:
            terminal_payload["debug"] = debug
        yield sse(terminal, terminal_payload)
        yield "data: [DONE]\n\n"


def interrupt_event(payload: Mapping[str, Any], *, response_id: str, run_id: str) -> str:
    interrupt = {
        "id": str(payload["interrupt_id"]),
        "status": "pending",
        "stage": str(payload["stage"]),
        "artifact_id": str(payload["artifact_id"]),
        "revision": int(payload["revision"]),
        "form": dict(payload.get("form") or {}),
    }
    return sse(
        "agent.interrupt",
        {
            "run_id": run_id,
            "response_id": response_id,
            "timestamp": ResponseEventBuilder(response_id=response_id, run_id=run_id).common()["timestamp"],
            "schema_version": "1",
            "interrupt": interrupt,
        },
    )


def artifact_event(artifact: ArticleArtifact, *, stage: str, content: Any, status: str) -> str:
    builder = ResponseEventBuilder(response_id=artifact.current_response_id, run_id=artifact.run_id)
    return sse(
        "agent.artifact",
        {
            **builder.common(),
            "schema_version": "1",
            "artifact": {
                "id": artifact.artifact_id,
                "revision": artifact.revision,
                "stage": stage,
                "status": status,
                "content": content,
                "summary": None,
            },
        },
    )


def _part(part: Any) -> tuple[str, Any]:
    if isinstance(part, Mapping):
        return str(part.get("event") or ""), part.get("data")
    return str(getattr(part, "event", "")), getattr(part, "data", None)


def _typing_chunks(text: str, size: int = 8) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]


async def _typed_text_events(
    builder: ResponseEventBuilder, text: str, delay_seconds: float, *, size: int = 8
) -> AsyncIterator[str]:
    for index, delta in enumerate(_typing_chunks(text, size=size)):
        if index and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        for event in builder.text_delta(delta):
            yield event


async def _typed_reasoning_events(
    builder: ResponseEventBuilder, text: str, delay_seconds: float, *, size: int = 8
) -> AsyncIterator[str]:
    for index, delta in enumerate(_typing_chunks(text, size=size)):
        if index and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        for event in builder.reasoning_delta(delta):
            yield event


def _merge_usage(total: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = incoming.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = int(total.get(key, 0)) + value
    for key in ("input_tokens_details", "output_tokens_details"):
        value = incoming.get(key)
        if not isinstance(value, Mapping):
            continue
        details = total.setdefault(key, {})
        for detail_key, detail_value in value.items():
            if isinstance(detail_value, int) and not isinstance(detail_value, bool):
                details[str(detail_key)] = int(details.get(str(detail_key), 0)) + detail_value


def _workflow_status(artifact: ArticleArtifact, run: Mapping[str, Any], pending: bool) -> str:
    if pending:
        return "waiting_for_input"
    if artifact.status != "running":
        return artifact.status
    runtime = str(run.get("status") or "")
    return "failed" if runtime in {"error", "timeout"} else artifact.status


def terminal_error(
    artifact: ArticleArtifact,
    state: Mapping[str, Any],
    run: Mapping[str, Any],
) -> dict[str, Any]:
    if artifact.last_error:
        return error_with_recovery(artifact.last_error)
    runtime_error = run.get("error")
    if isinstance(runtime_error, Mapping):
        return error_with_recovery(
            _error_payload(runtime_error, run=run, default_code="AGENT_RUNTIME_FAILED")
        )
    if runtime_error:
        message = str(runtime_error)[:2000]
        return error_with_recovery(
            {
                "code": _embedded_error_code(message) or "AGENT_RUNTIME_FAILED",
                "message": message,
                "retryable": str(run.get("status") or "") in {"timeout", "interrupted"},
                "stage": str(state.get("current_stage") or "workflow"),
            }
        )
    tasks = state.get("tasks") or []
    for task in tasks:
        if isinstance(task, Mapping) and task.get("error"):
            task_error = task["error"]
            if isinstance(task_error, Mapping):
                return error_with_recovery(
                    _error_payload(
                        task_error,
                        run=run,
                        default_code="AGENT_RUNTIME_FAILED",
                        stage=str(task.get("name") or state.get("current_stage") or "workflow"),
                    )
                )
            return error_with_recovery(
                {
                    "code": _embedded_error_code(str(task_error)) or "AGENT_RUNTIME_FAILED",
                    "message": str(task_error)[:2000],
                    "retryable": str(run.get("status") or "") in {"timeout", "interrupted"},
                    "stage": str(task.get("name") or state.get("current_stage") or "workflow"),
                }
            )
    return error_with_recovery(
        {
            "code": "AGENT_RUNTIME_FAILED",
            "message": f"Agent Server run ended with {run.get('status', 'error')}.",
            "retryable": str(run.get("status") or "") in {"timeout", "interrupted"},
            "stage": str(state.get("current_stage") or "workflow"),
        }
    )


def _embedded_error_code(message: str) -> str | None:
    match = re.search(r"\b([A-Z][A-Z0-9_]{2,}):", message)
    return match.group(1) if match else None


def _error_payload(
    value: Mapping[str, Any],
    *,
    run: Mapping[str, Any],
    default_code: str,
    stage: str | None = None,
) -> dict[str, Any]:
    code = str(value.get("code") or value.get("error_code") or default_code)
    message = str(value.get("message") or value.get("detail") or value)[:2000]
    retryable = value.get("retryable")
    if not isinstance(retryable, bool):
        retryable = str(run.get("status") or "") in {"timeout", "interrupted"}
    payload: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
    selected_stage = stage or value.get("stage")
    if selected_stage:
        payload["stage"] = str(selected_stage)
    details = value.get("details")
    if isinstance(details, Mapping):
        payload["details"] = dict(details)
    return payload
