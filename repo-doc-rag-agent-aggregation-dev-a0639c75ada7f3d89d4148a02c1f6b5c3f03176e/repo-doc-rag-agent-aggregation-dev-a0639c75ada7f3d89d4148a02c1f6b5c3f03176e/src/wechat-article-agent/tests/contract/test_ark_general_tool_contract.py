from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.core.errors import AppError
from app.llm.ark_responses import ArkResponsesChatModel

URL = "https://ark.example/api/v3/responses"


def _sse(events: list[dict[str, Any]]) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"


@pytest.mark.contract
@respx.mock
async def test_ark_general_function_tool_stream_is_assembled() -> None:

    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "type": "response.output_item.added",
                        "item": {"type": "function_call", "call_id": "call_1", "name": "render"},
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": "call_1",
                        "delta": '{"theme_id":"professional-clean"}',
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": "call_1",
                        "arguments": '{"theme_id":"professional-clean"}',
                    },
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "render",
                            "arguments": '{"theme_id":"professional-clean"}',
                        },
                    },
                    {"type": "response.completed", "response": {"usage": {"total_tokens": 1}}},
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    model = ArkResponsesChatModel(
        model="test",
        api_key="secret",
        responses_url=URL,
        max_retries=0,
    )
    observed = [
        (event_type, payload)
        async for event_type, payload in model._stream_response(
            messages=[{"role": "user", "content": "select a theme"}],
            thinking=False,
            response_format=None,
            metadata=None,
            tools=[
                {
                    "type": "function",
                    "name": "render",
                    "description": "render with a selected theme",
                    "parameters": {
                        "type": "object",
                        "properties": {"theme_id": {"type": "string"}},
                        "required": ["theme_id"],
                        "additionalProperties": False,
                    },
                }
            ],
            tool_choice="auto",
            max_tool_calls=1,
        )
    ]
    await model.aclose()

    payload = json.loads(route.calls.last.request.content)
    assert payload["tools"][0]["type"] == "function"
    assert payload["tool_choice"] == "auto"
    assert "max_tool_calls" not in payload
    assert [kind for kind, _ in observed] == [
        "function_call_started",
        "function_call_arguments_delta",
        "function_call_arguments_done",
        "function_call",
        "usage",
    ]
    call = next(payload["tool_call"] for kind, payload in observed if kind == "function_call")
    assert call == {
        "call_id": "call_1",
        "name": "render",
        "arguments": '{"theme_id":"professional-clean"}',
        "provider_item": {
            "type": "function_call",
            "call_id": "call_1",
            "name": "render",
            "arguments": '{"theme_id":"professional-clean"}',
        },
    }


@pytest.mark.contract
@respx.mock
async def test_agent_turn_returns_tool_call_and_reuses_function_output_items() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            text=_sse(
                [
                    {"type": "response.output_text.delta", "delta": '{"done":true}'},
                    {"type": "response.completed", "response": {"usage": {"total_tokens": 3}}},
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    model = ArkResponsesChatModel(model="test", api_key="secret", responses_url=URL, max_retries=0)

    class Done(BaseModel):
        done: bool

    result = await model.agent_turn(
        run_id="run_contract",
        call_id="agent_1",
        phase="contract",
        input_items=[
            {"role": "user", "content": "run"},
            {"type": "function_call", "call_id": "call_1", "name": "echo", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": '{"ok":true}'},
        ],
        tools=[],
        output_model=Done,
        max_tool_calls=1,
        timeout_seconds=10,
    )
    await model.aclose()

    payload = json.loads(route.calls.last.request.content)
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["input"][2]["type"] == "function_call_output"
    assert "tool_choice" not in payload
    assert result.text == '{"done":true}'
    assert result.usage["total_tokens"] == 3


@pytest.mark.contract
@respx.mock
async def test_agent_turn_supports_text_reasoning_and_multiple_calls_with_duplicate_done() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            text=_sse(
                [
                    {"type": "response.reasoning_summary_text.delta", "delta": "先检查"},
                    {"type": "response.output_text.delta", "delta": "准备读取规范"},
                    {
                        "type": "response.output_item.added",
                        "item": {"type": "function_call", "id": "item_1", "call_id": "call_1", "name": "one"},
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": "item_1",
                        "delta": "{}",
                    },
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "id": "item_1",
                            "call_id": "call_1",
                            "name": "one",
                            "arguments": "{}",
                        },
                    },
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "id": "item_1",
                            "call_id": "call_1",
                            "name": "one",
                            "arguments": "{}",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": "call_2",
                        "arguments": "",
                    },
                    {
                        "type": "response.output_item.added",
                        "item": {"type": "function_call", "call_id": "call_2", "name": "two"},
                    },
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "call_id": "call_2",
                            "name": "two",
                            "arguments": "",
                        },
                    },
                    {"type": "response.completed", "response": {"usage": {"input_tokens": 4}}},
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    model = ArkResponsesChatModel(model="test", api_key="secret", responses_url=URL, max_retries=0)

    class Done(BaseModel):
        done: bool

    seen: list[tuple[str, dict[str, Any]]] = []

    async def callback(kind: str, payload: dict[str, Any]) -> None:
        seen.append((kind, payload))

    result = await model.agent_turn(
        run_id="run_multi",
        call_id="turn_multi",
        phase="render_html",
        input_items=[{"role": "user", "content": "run"}],
        tools=[],
        output_model=Done,
        max_tool_calls=3,
        timeout_seconds=10,
        event_callback=callback,
    )
    await model.aclose()

    assert result.text == "准备读取规范"
    assert [call.call_id for call in result.tool_calls] == ["call_1", "call_2"]
    assert result.tool_calls[1].arguments == ""
    assert any(kind == "reasoning_delta" for kind, _ in seen)
    assert any(kind == "agent_output_text" for kind, _ in seen)
    assert result.usage["input_tokens"] == 4
    assert json.loads(route.calls.last.request.content)["thinking"] == {"type": "disabled"}


@pytest.mark.contract
@respx.mock
async def test_agent_turn_rejects_conflicting_duplicate_call_ids() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "one",
                            "arguments": '{"value":1}',
                        },
                    },
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "one",
                            "arguments": '{"value":2}',
                        },
                    },
                    {"type": "response.completed", "response": {"usage": {}}},
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    model = ArkResponsesChatModel(model="test", api_key="secret", responses_url=URL, max_retries=0)

    class Done(BaseModel):
        done: bool

    with pytest.raises(AppError) as raised:
        await model.agent_turn(
            run_id="run_conflict",
            call_id="turn_conflict",
            phase="render_html",
            input_items=[{"role": "user", "content": "run"}],
            tools=[],
            output_model=Done,
            max_tool_calls=2,
            timeout_seconds=10,
        )
    await model.aclose()
    assert raised.value.code == "ARK_FUNCTION_CALL_ID_CONFLICT"


@pytest.mark.contract
@respx.mock
async def test_incomplete_general_stream_is_protocol_error() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            text="data: " + json.dumps({"type": "response.output_text.delta", "delta": "partial"}) + "\n\n",
            headers={"content-type": "text/event-stream"},
        )
    )
    model = ArkResponsesChatModel(model="test", api_key="secret", responses_url=URL, max_retries=0)
    with pytest.raises(AppError) as raised:
        async for _ in model._stream_response(
            messages=[{"role": "user", "content": "run"}],
            thinking=False,
            response_format=None,
            metadata=None,
        ):
            pass
    await model.aclose()
    assert raised.value.code == "ARK_STREAM_PROTOCOL_ERROR"


@pytest.mark.contract
@respx.mock
async def test_provider_failed_event_maps_to_stable_error() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            text=_sse([{"type": "response.failed", "error": {"code": "provider_failed"}}]),
            headers={"content-type": "text/event-stream"},
        )
    )
    model = ArkResponsesChatModel(model="test", api_key="secret", responses_url=URL, max_retries=0)
    with pytest.raises(AppError) as raised:
        async for _ in model._stream_response(
            messages=[{"role": "user", "content": "run"}],
            thinking=False,
            response_format=None,
            metadata=None,
        ):
            pass
    await model.aclose()
    assert raised.value.code == "ARK_RESPONSE_FAILED"
