from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI

from app.llm.gateway import LLMCallError, LLMRequest, LLMResult, LLMToolCall, LLMUsage


def _status_code(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if value is None and response is not None:
        value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _category(error: Exception) -> str:
    status = _status_code(error)
    if status == 429 or "ratelimit" in type(error).__name__.lower():
        return "rate_limit"
    if status is not None and status >= 500:
        return "upstream_5xx"
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, (httpx.ConnectError, httpx.NetworkError)):
        return "connect"
    return "connect"


def _request_id(value: Any) -> str | None:
    request_id = getattr(value, "request_id", None) or getattr(value, "_request_id", None)
    response = getattr(value, "response", None)
    if not request_id and response is not None:
        request_id = getattr(response, "request_id", None) or getattr(response, "_request_id", None)
        headers = getattr(response, "headers", None)
        if headers:
            request_id = request_id or headers.get("x-request-id") or headers.get("request-id")
    return str(request_id) if request_id else None


def _thinking_extra_body(base_url: str | None) -> dict[str, Any]:
    host = urlparse(base_url or "").hostname or ""
    if host == "siliconflow.cn" or host.endswith(".siliconflow.cn"):
        return {"enable_thinking": False}
    return {"thinking": {"type": "disabled"}}


class OpenAICompatibleProvider:
    """OpenAI-compatible adapter for strict structured output and function calling."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str | None = None,
        timeout_seconds: float = 20.0,
        max_retries: int = 0,
        sdk_client: Any | None = None,
    ) -> None:
        self._thinking_extra_body = _thinking_extra_body(base_url)
        self._client = sdk_client or AsyncOpenAI(
            api_key=api_key or "missing-api-key",
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max(0, int(max_retries)),
        )

    async def complete_json(self, request: LLMRequest) -> LLMResult:
        if request.tools and request.response_format:
            raise LLMCallError(
                "tools and response_format are mutually exclusive",
                error_category="invalid_request",
            )
        if request.tools:
            _validate_strict_tools(request.tools)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "temperature": 0,
            "max_tokens": max(1, int(request.max_tokens)),
            "extra_body": self._thinking_extra_body,
        }
        if request.tools:
            kwargs["tools"] = request.tools
            kwargs["tool_choice"] = request.tool_choice or "required"
            if request.parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = request.parallel_tool_calls
        else:
            kwargs["response_format"] = request.response_format or {"type": "json_object"}
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMCallError(
                "LLM provider request failed",
                error_category=_category(exc),
            ) from exc

        provider_request_id = _request_id(response)
        try:
            choice = response.choices[0]
            message = choice.message
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMCallError(
                "LLM provider response did not contain a completion",
                error_category="invalid_response",
            ) from exc
        data: dict[str, Any] = {}
        tool_calls: tuple[LLMToolCall, ...] = ()
        if request.tools:
            tool_calls = _parse_tool_calls(message)
            raw_output = {
                "tool_calls": [
                    {
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    for call in tool_calls
                ]
            }
        else:
            content = message.content or ""
            try:
                parsed = json.loads(content)
            except (TypeError, json.JSONDecodeError) as exc:
                raise LLMCallError(
                    "LLM provider response was not valid JSON",
                    error_category="invalid_response",
                    debug_details={"raw_content": content},
                ) from exc
            if not isinstance(parsed, dict):
                raise LLMCallError(
                    "LLM provider JSON response must be an object",
                    error_category="invalid_response",
                )
            data = parsed
            raw_output = {"content": content}
        usage = LLMUsage()
        if request.collect_usage:
            raw_usage = getattr(response, "usage", None)
            usage = LLMUsage(
                prompt_tokens=int(getattr(raw_usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(raw_usage, "completion_tokens", 0) or 0),
                total_tokens=int(getattr(raw_usage, "total_tokens", 0) or 0),
            )
        return LLMResult(
            data=data,
            usage=usage,
            tool_calls=tool_calls,
            request_id=provider_request_id,
            raw_output=raw_output,
        )

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


def _validate_strict_tools(tools: list[dict[str, Any]]) -> None:
    for tool in tools:
        function = tool.get("function") if tool.get("type") == "function" else None
        parameters = function.get("parameters") if isinstance(function, dict) else None
        if not isinstance(function, dict) or function.get("strict") is not True:
            raise LLMCallError(
                "every function tool must set strict=true",
                error_category="invalid_request",
            )
        if not isinstance(function.get("description"), str) or not function["description"].strip():
            raise LLMCallError(
                "every function tool requires a description",
                error_category="invalid_request",
            )
        if not isinstance(parameters, dict) or parameters.get("additionalProperties") is not False:
            raise LLMCallError(
                "strict function parameters must forbid additional properties",
                error_category="invalid_request",
            )


def _parse_tool_calls(message: Any) -> tuple[LLMToolCall, ...]:
    raw_calls = getattr(message, "tool_calls", None)
    if not raw_calls:
        raise LLMCallError(
            "LLM provider response did not contain a tool call",
            error_category="invalid_response",
        )
    parsed_calls: list[LLMToolCall] = []
    for call in raw_calls:
        try:
            call_id = str(call.id)
            name = str(call.function.name)
            arguments = json.loads(call.function.arguments)
        except (AttributeError, TypeError, json.JSONDecodeError) as exc:
            raise LLMCallError(
                "LLM provider returned an invalid tool call",
                error_category="invalid_response",
            ) from exc
        if not call_id or not name or not isinstance(arguments, dict):
            raise LLMCallError(
                "LLM provider tool arguments must be a JSON object",
                error_category="invalid_response",
            )
        parsed_calls.append(LLMToolCall(call_id=call_id, name=name, arguments=arguments))
    return tuple(parsed_calls)
