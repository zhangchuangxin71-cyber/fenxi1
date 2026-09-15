from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from app.agent_engine.contracts import EngineEventCallback, ModelTurn, ToolCall
from app.config import AppSettings
from app.core.errors import AppError
from app.debug.trace import record
from app.llm.inputs import StructuredRepairInput, render_input

OutputT = TypeVar("OutputT", bound=BaseModel)
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]
StructuredRetryStrategy = Literal["repair", "restart"]


@dataclass(slots=True)
class ArkWebSearchResult[ResultT]:
    output: ResultT
    annotations: list[dict[str, Any]]
    queries: list[str]
    usage: dict[str, Any]


class ArkResponsesChatModel(BaseChatModel):
    """LangChain-observable wrapper around Volcengine's Responses endpoint."""

    model_name: str = Field(alias="model")
    api_key: str
    responses_url: str
    connect_timeout_seconds: float = 10
    first_event_timeout_seconds: float = 60
    stream_idle_timeout_seconds: float = 90
    call_max_seconds: float = 600
    max_retries: int = 2
    retry_base_seconds: float = 1
    structured_output_max_repairs: int = 1
    structured_output_max_retries: int = 2
    context_management_enabled: bool = False
    caching_enabled: bool = False

    _client: httpx.AsyncClient = PrivateAttr()
    _event_callback: EventCallback | None = PrivateAttr(default=None)

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    def __init__(self, **data: Any) -> None:
        callback = data.pop("event_callback", None)
        super().__init__(**data)
        timeout = httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.stream_idle_timeout_seconds,
            write=self.stream_idle_timeout_seconds,
            pool=self.connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(timeout=timeout)
        self._event_callback = callback

    @classmethod
    def from_settings(
        cls,
        settings: AppSettings,
        *,
        model: str | None = None,
        event_callback: EventCallback | None = None,
    ) -> ArkResponsesChatModel:
        return cls(
            model=model or settings.ark_model_main,
            api_key=settings.ark_api_key,
            responses_url=str(settings.ark_responses_url),
            connect_timeout_seconds=settings.ark_connect_timeout_seconds,
            first_event_timeout_seconds=settings.ark_first_event_timeout_seconds,
            stream_idle_timeout_seconds=settings.ark_stream_idle_timeout_seconds,
            call_max_seconds=settings.ark_call_max_seconds,
            max_retries=settings.ark_max_retries,
            retry_base_seconds=settings.ark_retry_base_seconds,
            structured_output_max_repairs=settings.ark_structured_output_max_repairs,
            structured_output_max_retries=settings.ark_structured_output_max_retries,
            context_management_enabled=settings.ark_context_management_enabled,
            caching_enabled=settings.ark_caching_enabled,
            event_callback=event_callback,
        )

    @property
    def _llm_type(self) -> str:
        return "volcengine-ark-responses"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model_name, "responses_url": self.responses_url}

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        raise RuntimeError("ArkResponsesChatModel is async-only; use ainvoke or astream")

    async def structured(
        self,
        *,
        call_id: str | None = None,
        phase: str,
        system_prompt: str,
        input_text: str,
        output_model: type[OutputT],
        thinking: bool,
        retry_strategy: StructuredRetryStrategy = "repair",
        metadata: dict[str, Any] | None = None,
        event_callback: EventCallback | None = None,
    ) -> OutputT:
        original_messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": input_text},
        ]
        messages = original_messages
        response_format = _response_format(output_model, phase)
        validation_error = ""
        text = ""
        retry_limit = (
            self.structured_output_max_repairs
            if retry_strategy == "repair"
            else self.structured_output_max_retries
        )
        for structured_attempt in range(retry_limit + 1):
            text = ""
            async for event_type, payload in self._stream_response(
                messages=messages,
                thinking=thinking if structured_attempt == 0 else False,
                response_format=response_format,
                metadata=metadata,
            ):
                if event_type == "output_text":
                    text += str(payload["delta"])
                elif event_type == "reasoning":
                    reasoning_payload = {"delta": str(payload["delta"]), "phase": phase}
                    if call_id is not None:
                        reasoning_payload["call_id"] = call_id
                    await self._notify(
                        "reasoning_delta",
                        reasoning_payload,
                        callback=event_callback,
                    )
                elif event_type == "usage":
                    usage_payload = {
                        "phase": phase,
                        "structured_attempt": structured_attempt,
                        "retry_strategy": retry_strategy,
                        **payload,
                    }
                    if call_id is not None:
                        usage_payload["call_id"] = call_id
                    await self._notify(
                        "usage",
                        usage_payload,
                        callback=event_callback,
                    )
            raw_trace = {
                "phase": phase,
                "provider_raw_output": text,
                "structured_attempt": structured_attempt,
                "retry_strategy": retry_strategy,
            }
            if call_id is not None:
                raw_trace["id"] = call_id
            record("llm_calls", raw_trace)
            try:
                return output_model.model_validate_json(text)
            except Exception as exc:
                validation_error = str(exc)[:2000]
                empty_output = not text.strip()
                if structured_attempt >= retry_limit:
                    raise AppError(
                        502,
                        "ARK_STRUCTURED_OUTPUT_INVALID",
                        f"Model returned invalid structured output for {phase}.",
                        retryable=empty_output,
                        stage=phase,
                        details={
                            "validation_error": validation_error,
                            "empty_output": empty_output,
                            "retry_strategy": retry_strategy,
                            "attempts": structured_attempt + 1,
                        },
                    ) from exc
                should_repair = retry_strategy == "repair" and not empty_output
                fallback = "strict_schema_repair" if should_repair else "strict_schema_restart"
                record(
                    "fallbacks",
                    {
                        "id": call_id,
                        "source": "structured_output",
                        "phase": phase,
                        "attempt": structured_attempt + 1,
                        "reason": validation_error,
                        "fallback": fallback,
                    },
                )
                event_name = "structured_repair" if should_repair else "structured_retry"
                await self._notify(
                    event_name,
                    {
                        "phase": phase,
                        "call_id": call_id,
                        "attempt": structured_attempt + 1,
                    },
                    callback=event_callback,
                )
                messages = (
                    _repair_messages(
                        invalid_output=text,
                        validation_error=validation_error,
                        phase=phase,
                    )
                    if should_repair
                    else original_messages
                )
                if not should_repair:
                    await asyncio.sleep(_retry_delay(self.retry_base_seconds, structured_attempt))
        raise AssertionError("structured output loop exhausted unexpectedly")

    async def web_search[ResultT: BaseModel](
        self,
        *,
        call_id: str | None = None,
        phase: str,
        system_prompt: str,
        input_text: str,
        output_model: type[ResultT] | None = None,
        max_keyword: int = 2,
        metadata: dict[str, Any] | None = None,
        event_callback: EventCallback | None = None,
    ) -> ArkWebSearchResult[ResultT | str]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": input_text},
        ]
        for structured_attempt in range(self.structured_output_max_retries + 1):
            text = ""
            annotations: list[dict[str, Any]] = []
            queries: list[str] = []
            usage: dict[str, Any] = {}
            async for event_type, payload in self._stream_response(
                messages=messages,
                thinking=False,
                response_format=_response_format(output_model, phase) if output_model else None,
                metadata=metadata,
                tools=[{"type": "web_search", "max_keyword": max_keyword}],
                tool_choice={"type": "web_search"},
                max_tool_calls=max_keyword,
            ):
                if event_type == "output_text":
                    text += str(payload["delta"])
                elif event_type == "annotation":
                    annotation = dict(payload["annotation"])
                    if annotation not in annotations:
                        annotations.append(annotation)
                elif event_type == "web_search_call":
                    query = str(payload.get("query") or "").strip()
                    if query and query not in queries:
                        queries.append(query)
                    await self._notify(
                        "web_search_call",
                        {"phase": phase, "call_id": call_id, **payload},
                        callback=event_callback,
                    )
                elif event_type == "usage":
                    usage.update(payload)
                    await self._notify(
                        "usage",
                        {
                            "phase": phase,
                            "call_id": call_id,
                            "structured_attempt": structured_attempt,
                            "retry_strategy": "restart",
                            **payload,
                        },
                        callback=event_callback,
                    )
            record(
                "llm_calls",
                {
                    "id": call_id,
                    "phase": phase,
                    "provider_raw_output": text,
                    "web_search_queries": queries,
                    "annotation_count": len(annotations),
                    "structured_attempt": structured_attempt,
                    "retry_strategy": "restart",
                },
            )
            if output_model is None:
                return ArkWebSearchResult(text, annotations, queries, usage)
            try:
                parsed = output_model.model_validate_json(text)
                return ArkWebSearchResult(parsed, annotations, queries, usage)
            except Exception as exc:
                validation_error = str(exc)[:2000]
                empty_output = not text.strip()
                if structured_attempt >= self.structured_output_max_retries:
                    raise AppError(
                        502,
                        "ARK_STRUCTURED_OUTPUT_INVALID",
                        f"Model returned invalid structured output for {phase}.",
                        retryable=empty_output,
                        stage=phase,
                        details={
                            "validation_error": validation_error,
                            "empty_output": empty_output,
                            "retry_strategy": "restart",
                            "attempts": structured_attempt + 1,
                        },
                    ) from exc
                record(
                    "fallbacks",
                    {
                        "id": call_id,
                        "source": "structured_output",
                        "phase": phase,
                        "attempt": structured_attempt + 1,
                        "reason": validation_error,
                        "fallback": "strict_schema_restart",
                    },
                )
                await self._notify(
                    "structured_retry",
                    {"phase": phase, "call_id": call_id, "attempt": structured_attempt + 1},
                    callback=event_callback,
                )
                await asyncio.sleep(_retry_delay(self.retry_base_seconds, structured_attempt))
        raise AssertionError("web search structured output loop exhausted unexpectedly")

    async def agent_turn(
        self,
        *,
        run_id: str,
        call_id: str,
        phase: str,
        input_items: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]],
        output_model: type[BaseModel],
        max_tool_calls: int,
        timeout_seconds: float,
        event_callback: EngineEventCallback | None = None,
    ) -> ModelTurn:
        """Execute one strict Responses turn for the generic ReAct Engine.

        This remains separate from ``structured`` so existing LangGraph nodes keep
        their one-call retry semantics while the Engine owns model/tool re-entry.
        Thinking is intentionally hard-disabled for Engine calls.
        """

        text = ""
        pending_explanation = ""
        explanation_emitted = False
        usage: dict[str, Any] = {}
        calls: dict[str, ToolCall] = {}
        async with asyncio.timeout(timeout_seconds):
            async for event_type, payload in self._stream_response(
                messages=input_items,
                thinking=False,
                response_format=_response_format(output_model, phase),
                metadata={"agent_phase": phase},
                tools=tools,
                tool_choice="auto" if tools else None,
                max_tool_calls=max_tool_calls,
            ):
                if event_type == "output_text":
                    delta = str(payload.get("delta") or "")
                    text += delta
                    pending_explanation += delta
                elif event_type == "reasoning":
                    await self._notify(
                        "reasoning_delta",
                        {"call_id": call_id, "phase": phase, "delta": str(payload.get("delta") or "")},
                        callback=event_callback,
                    )
                elif event_type == "function_call":
                    if pending_explanation.strip():
                        await self._notify(
                            "agent_output_text",
                            {
                                "call_id": call_id,
                                "phase": phase,
                                "text": pending_explanation,
                                "tool_group": True,
                            },
                            callback=event_callback,
                        )
                        pending_explanation = ""
                        explanation_emitted = True
                    tool_call = ToolCall(**dict(payload["tool_call"]))
                    existing = calls.get(tool_call.call_id)
                    if existing is not None:
                        if (existing.name, existing.arguments) != (
                            tool_call.name,
                            tool_call.arguments,
                        ):
                            raise AppError(
                                502,
                                "ARK_FUNCTION_CALL_ID_CONFLICT",
                                "Ark reused a function call ID with different content.",
                                retryable=False,
                                details={"call_id": tool_call.call_id},
                            )
                        continue
                    calls[tool_call.call_id] = tool_call
                    await self._notify(
                        "agent_tool_call",
                        {
                            "call_id": call_id,
                            "run_id": run_id,
                            "phase": phase,
                            "tool_call": {
                                "call_id": tool_call.call_id,
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                            },
                        },
                        callback=event_callback,
                    )
                elif event_type == "usage":
                    usage.update(payload)
        if calls and pending_explanation.strip():
            await self._notify(
                "agent_output_text",
                {
                    "call_id": call_id,
                    "phase": phase,
                    "text": pending_explanation,
                    "tool_group": True,
                },
                callback=event_callback,
            )
            explanation_emitted = True
        return ModelTurn(
            text=text,
            tool_calls=list(calls.values()),
            response_items=[call.provider_item for call in calls.values()],
            usage=usage,
            text_emitted=explanation_emitted,
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        chunks = [chunk async for chunk in self._astream(messages, stop, run_manager, **kwargs)]
        content = "".join(
            str(chunk.message.content) for chunk in chunks if isinstance(chunk.message.content, str)
        )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del stop
        provider_messages = [_message_to_dict(message) for message in messages]
        thinking = bool(kwargs.pop("thinking", False))
        async for event_type, payload in self._stream_response(
            messages=provider_messages,
            thinking=thinking,
            response_format=kwargs.pop("response_format", None),
            metadata=kwargs.pop("metadata", None),
        ):
            if event_type == "output_text":
                chunk = AIMessageChunk(content=str(payload["delta"]))
            elif event_type == "reasoning":
                chunk = AIMessageChunk(content=[{"type": "reasoning", "reasoning": str(payload["delta"])}])
            else:
                continue
            generation_chunk = ChatGenerationChunk(message=chunk)
            if run_manager is not None:
                await run_manager.on_llm_new_token(chunk.text(), chunk=generation_chunk)
            yield generation_chunk

    async def _stream_response(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        thinking: bool,
        response_format: dict[str, Any] | None,
        metadata: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, str] | None = None,
        max_tool_calls: int | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        if not self.api_key:
            raise AppError(503, "ARK_API_KEY_MISSING", "ARK_API_KEY is not configured.")
        payload: dict[str, Any] = {
            "model": self.model_name,
            "input": list(messages),
            "stream": True,
            "store": False,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if response_format:
            payload["text"] = {"format": response_format}
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if tools and max_tool_calls is not None and not any(
            tool.get("type") == "function" for tool in tools
        ):
            payload["max_tool_calls"] = max_tool_calls
        # Ark's Responses endpoint currently rejects the OpenAI-compatible
        # top-level ``metadata`` field. Keep it as an adapter-local argument so
        # callers can retain phase information in trace without sending an
        # unsupported provider field.
        if self.context_management_enabled:
            payload["context_management"] = {"type": "enabled"}
        if self.caching_enabled:
            payload["caching"] = {"type": "enabled"}

        meaningful = False
        for attempt in range(self.max_retries + 1):
            function_items: dict[str, dict[str, Any]] = {}
            function_arguments: dict[str, str] = {}
            completed = False
            request_payload = dict(payload)
            if attempt > 0:
                request_payload["thinking"] = {"type": "disabled"}
            try:
                async with asyncio.timeout(self.call_max_seconds):
                    async with self._client.stream(
                        "POST",
                        self.responses_url,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=request_payload,
                    ) as response:
                        if response.status_code >= 400:
                            body = (await response.aread()).decode(errors="replace")
                            if _capability_error(response.status_code, body, request_payload):
                                self.context_management_enabled = False
                                self.caching_enabled = False
                                async for fallback_event in self._stream_response(
                                    messages=messages,
                                    thinking=False,
                                    response_format=response_format,
                                    metadata=metadata,
                                    tools=tools,
                                    tool_choice=tool_choice,
                                    max_tool_calls=max_tool_calls,
                                ):
                                    yield fallback_event
                                return
                            if _retryable_status(response.status_code) and attempt < self.max_retries:
                                await asyncio.sleep(_retry_delay(self.retry_base_seconds, attempt))
                                continue
                            raise AppError(
                                502,
                                "ARK_UPSTREAM_ERROR",
                                f"Ark returned HTTP {response.status_code}.",
                                retryable=_retryable_status(response.status_code),
                                details={"provider_body": body[:2000]},
                            )
                        iterator = response.aiter_lines()
                        while True:
                            timeout = (
                                self.first_event_timeout_seconds
                                if not meaningful
                                else self.stream_idle_timeout_seconds
                            )
                            try:
                                line = await asyncio.wait_for(anext(iterator), timeout=timeout)
                            except StopAsyncIteration:
                                break
                            if not line.startswith("data:") or line == "data: [DONE]":
                                continue
                            event = json.loads(line[5:].strip())
                            event_type = str(event.get("type", ""))
                            if event_type == "response.output_text.delta":
                                meaningful = True
                                yield "output_text", {"delta": event.get("delta", "")}
                            elif event_type == "response.reasoning_summary_text.delta":
                                meaningful = True
                                yield "reasoning", {"delta": event.get("delta", "")}
                            elif event_type == "response.output_text.annotation.added":
                                annotation = event.get("annotation")
                                if isinstance(annotation, dict):
                                    yield "annotation", {"annotation": annotation}
                            elif event_type == "response.output_item.added":
                                item = event.get("item") or {}
                                if item.get("type") == "function_call":
                                    meaningful = True
                                    item_id = str(item.get("id") or item.get("call_id") or "")
                                    if item_id:
                                        function_items[item_id] = dict(item)
                                        function_arguments.setdefault(item_id, "")
                                        yield "function_call_started", {"item": item}
                            elif event_type == "response.function_call_arguments.delta":
                                meaningful = True
                                item_id = str(event.get("item_id") or "")
                                function_arguments[item_id] = function_arguments.get(item_id, "") + str(
                                    event.get("delta") or ""
                                )
                                yield (
                                    "function_call_arguments_delta",
                                    {
                                        "item_id": item_id,
                                        "delta": event.get("delta") or "",
                                    },
                                )
                            elif event_type == "response.function_call_arguments.done":
                                meaningful = True
                                item_id = str(event.get("item_id") or "")
                                if event.get("arguments") is not None:
                                    function_arguments[item_id] = str(event.get("arguments") or "")
                                yield (
                                    "function_call_arguments_done",
                                    {
                                        "item_id": item_id,
                                        "arguments": function_arguments.get(item_id, ""),
                                    },
                                )
                            elif event_type == "response.output_item.done":
                                item = event.get("item") or {}
                                if item.get("type") == "web_search_call":
                                    action = item.get("action") or {}
                                    yield (
                                        "web_search_call",
                                        {
                                            "id": item.get("id"),
                                            "status": item.get("status"),
                                            "query": action.get("query"),
                                        },
                                    )
                                elif item.get("type") == "function_call":
                                    meaningful = True
                                    item_id = str(item.get("id") or item.get("call_id") or "")
                                    started_item = function_items.get(item_id, {})
                                    accumulated = function_arguments.get(item_id, "")
                                    arguments = str(item.get("arguments") or accumulated)
                                    call_id = str(
                                        item.get("call_id") or started_item.get("call_id") or item_id
                                    )
                                    name = str(item.get("name") or started_item.get("name") or "")
                                    provider_item = {
                                        "type": "function_call",
                                        "call_id": call_id,
                                        "name": name,
                                        "arguments": arguments,
                                    }
                                    yield (
                                        "function_call",
                                        {
                                            "tool_call": {
                                                "call_id": call_id,
                                                "name": name,
                                                "arguments": arguments,
                                                "provider_item": provider_item,
                                            }
                                        },
                                    )
                            elif event_type == "response.completed":
                                completed = True
                                response_data = event.get("response") or {}
                                yield "usage", dict(response_data.get("usage") or {})
                            elif event_type in {"response.failed", "error"}:
                                raise AppError(
                                    502,
                                    "ARK_RESPONSE_FAILED",
                                    "Ark response failed.",
                                    retryable=not meaningful,
                                    details={"provider_event": event},
                                )
                        if not completed:
                            raise AppError(
                                502,
                                "ARK_STREAM_PROTOCOL_ERROR",
                                "Ark stream ended before response.completed.",
                                retryable=not meaningful,
                            )
                return
            except asyncio.CancelledError:
                raise
            except AppError as exc:
                if exc.retryable and not meaningful and attempt < self.max_retries:
                    await asyncio.sleep(_retry_delay(self.retry_base_seconds, attempt))
                    continue
                raise
            except json.JSONDecodeError as exc:
                if not meaningful and attempt < self.max_retries:
                    await asyncio.sleep(_retry_delay(self.retry_base_seconds, attempt))
                    continue
                raise AppError(
                    502,
                    "ARK_STREAM_PROTOCOL_ERROR",
                    "Ark returned a malformed SSE event.",
                    retryable=not meaningful,
                ) from exc
            except (httpx.TimeoutException, httpx.NetworkError, TimeoutError) as exc:
                if meaningful or attempt >= self.max_retries:
                    raise AppError(
                        504,
                        (
                            "ARK_STREAM_TIMEOUT"
                            if isinstance(exc, (httpx.TimeoutException, TimeoutError))
                            else "ARK_NETWORK_ERROR"
                        ),
                        "Ark stream timed out or disconnected.",
                        retryable=not meaningful,
                    ) from exc
                await asyncio.sleep(_retry_delay(self.retry_base_seconds, attempt))

    async def _notify(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        callback: EventCallback | None = None,
    ) -> None:
        selected = callback or self._event_callback
        if selected is None:
            return
        result = selected(event_type, payload)
        if result is not None:
            await result

    async def aclose(self) -> None:
        await self._client.aclose()


def _response_format(model: type[BaseModel], name: str) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": name.replace("-", "_")[:64],
        "strict": True,
        "schema": model.model_json_schema(),
    }


def _repair_messages(*, invalid_output: str, validation_error: str, phase: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "# 角色与任务\n修复一个不符合 strict JSON Schema 的模型输出。\n\n"
                "# 输入说明\n输入包含原始无效输出和校验错误。\n\n"
                "# 输出契约\n只返回符合当前 provider strict JSON Schema 的完整 JSON 对象。\n\n"
                "# 禁止事项\n不要解释错误，不要使用 Markdown 代码块，不要改变原任务语义。"
            ),
        },
        {
            "role": "user",
            "content": render_input(
                StructuredRepairInput(
                    phase=phase,
                    invalid_output=invalid_output,
                    validation_error=validation_error,
                )
            ),
        },
    ]


def _message_to_dict(message: BaseMessage) -> dict[str, Any]:
    role = {"human": "user", "ai": "assistant", "system": "system"}.get(message.type, message.type)
    return {"role": role, "content": message.text()}


def _retryable_status(status: int) -> bool:
    return status in {408, 429} or status >= 500


def _retry_delay(base_seconds: float, attempt: int) -> float:
    return cast(float, base_seconds * (2**attempt) * random.uniform(0.75, 1.25))


def _capability_error(status: int, body: str, payload: dict[str, Any]) -> bool:
    return (
        status in {400, 422}
        and bool({"context_management", "caching"} & payload.keys())
        and any(
            name in body.lower()
            for name in ("context_management", "caching", "unknown field", "extra inputs")
        )
    )
