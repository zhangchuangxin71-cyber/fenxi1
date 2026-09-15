from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
import respx

from app.core.errors import AppError
from app.llm.ark_responses import ArkResponsesChatModel
from app.llm.schemas import TaskSpecOutput, WebMaterialOutput

URL = "https://ark.example/api/v3/responses"


def _sse(events: list[dict[str, Any]]) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"


@pytest.mark.contract
@respx.mock
async def test_structured_stream_separates_reasoning_from_json() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def callback(kind: str, data: dict[str, Any]) -> None:
        seen.append((kind, data))

    output = {
        "user_facing_message": "任务书已经整理完成。",
        "artifact": {
            "topic": "主题",
            "audience": "读者",
            "goal": "目标",
            "tone": "自然",
            "length": "1000字",
            "other_requirements": [],
        },
    }
    text = json.dumps(output, ensure_ascii=False)
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            text=_sse(
                [
                    {"type": "response.reasoning_summary_text.delta", "delta": "分析材料"},
                    {"type": "response.output_text.delta", "delta": text[:20]},
                    {"type": "response.output_text.delta", "delta": text[20:]},
                    {"type": "response.completed", "response": {"usage": {"total_tokens": 12}}},
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    model = ArkResponsesChatModel(
        model="test",
        api_key="secret",
        responses_url=URL,
        event_callback=callback,
        max_retries=0,
    )
    result = await model.structured(
        phase="task_spec",
        system_prompt="# 角色与任务\n测试",
        input_text="input",
        output_model=TaskSpecOutput,
        thinking=True,
    )
    await model.aclose()

    assert result.user_facing_message == "任务书已经整理完成。"
    assert seen[0] == ("reasoning_delta", {"delta": "分析材料", "phase": "task_spec"})
    assert seen[-1][0] == "usage"
    request_payload = json.loads(route.calls.last.request.content)
    output_schema = request_payload["text"]["format"]["schema"]
    assert request_payload["text"]["format"]["strict"] is True
    assert output_schema["properties"]["user_facing_message"]["description"]
    assert output_schema["$defs"]["TaskSpecArtifact"]["properties"]["topic"]["description"] == (
        "文章最终要讨论的明确主题。"
    )


@pytest.mark.contract
@respx.mock
async def test_context_capability_falls_back_once() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "unknown field context_management"}}),
            httpx.Response(
                200,
                text=_sse(
                    [
                        {
                            "type": "response.output_text.delta",
                            "delta": json.dumps(
                                {
                                    "user_facing_message": "ok",
                                    "artifact": {
                                        "topic": "t",
                                        "audience": "a",
                                        "goal": "g",
                                        "tone": "x",
                                        "length": "l",
                                        "other_requirements": [],
                                    },
                                }
                            ),
                        },
                        {"type": "response.completed", "response": {"usage": {}}},
                    ]
                ),
            ),
        ]
    )
    model = ArkResponsesChatModel(
        model="test",
        api_key="secret",
        responses_url=URL,
        context_management_enabled=True,
        caching_enabled=True,
        max_retries=0,
    )
    result = await model.structured(
        phase="task_spec",
        system_prompt="test",
        input_text="input",
        output_model=TaskSpecOutput,
        thinking=False,
    )
    await model.aclose()
    assert result.artifact.topic == "t"
    assert route.call_count == 2
    second_payload = json.loads(route.calls[1].request.content)
    assert "context_management" not in second_payload
    assert "caching" not in second_payload


@pytest.mark.contract
@respx.mock
async def test_invalid_strict_output_is_repaired_by_a_new_strict_call() -> None:
    valid = json.dumps(
        {
            "user_facing_message": "repaired",
            "artifact": {
                "topic": "t",
                "audience": "a",
                "goal": "g",
                "tone": "x",
                "length": "l",
                "other_requirements": [],
            },
        }
    )
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                text=_sse(
                    [
                        {"type": "response.output_text.delta", "delta": '{"broken":true}'},
                        {"type": "response.completed", "response": {"usage": {}}},
                    ]
                ),
            ),
            httpx.Response(
                200,
                text=_sse(
                    [
                        {"type": "response.output_text.delta", "delta": valid},
                        {"type": "response.completed", "response": {"usage": {}}},
                    ]
                ),
            ),
        ]
    )
    seen: list[str] = []

    async def callback(kind: str, _: dict[str, Any]) -> None:
        seen.append(kind)

    model = ArkResponsesChatModel(
        model="test",
        api_key="secret",
        responses_url=URL,
        structured_output_max_repairs=1,
        max_retries=0,
    )
    result = await model.structured(
        phase="task_spec",
        system_prompt="original prompt",
        input_text="input",
        output_model=TaskSpecOutput,
        thinking=True,
        event_callback=callback,
    )
    await model.aclose()

    assert result.user_facing_message == "repaired"
    assert route.call_count == 2
    assert "structured_repair" in seen
    first_payload = json.loads(route.calls[0].request.content)
    repair_payload = json.loads(route.calls[1].request.content)
    assert first_payload["thinking"] == {"type": "enabled"}
    assert repair_payload["thinking"] == {"type": "disabled"}
    assert repair_payload["text"]["format"]["strict"] is True
    assert "修复一个不符合 strict JSON Schema" in repair_payload["input"][0]["content"]
    assert repair_payload["input"][1]["content"].startswith("# 任务输入")
    assert "# 字段说明" in repair_payload["input"][1]["content"]
    assert "<input_json>" in repair_payload["input"][1]["content"]


@pytest.mark.contract
@respx.mock
async def test_invalid_strict_output_stops_at_repair_limit() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            text=_sse(
                [
                    {"type": "response.output_text.delta", "delta": '{"broken":true}'},
                    {"type": "response.completed", "response": {"usage": {}}},
                ]
            ),
        )
    )
    model = ArkResponsesChatModel(
        model="test",
        api_key="secret",
        responses_url=URL,
        structured_output_max_repairs=0,
        max_retries=0,
    )
    with pytest.raises(AppError) as raised:
        await model.structured(
            phase="task_spec",
            system_prompt="prompt",
            input_text="input",
            output_model=TaskSpecOutput,
            thinking=False,
        )
    await model.aclose()
    assert raised.value.code == "ARK_STRUCTURED_OUTPUT_INVALID"


@pytest.mark.contract
@respx.mock
async def test_workflow_structured_retry_restarts_original_task_without_thinking() -> None:
    valid = json.dumps(
        {
            "user_facing_message": "已重新判断。",
            "artifact": {
                "topic": "t",
                "audience": "a",
                "goal": "g",
                "tone": "x",
                "length": "l",
                "other_requirements": [],
            },
        }
    )
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                text=_sse(
                    [
                        {"type": "response.output_text.delta", "delta": '{"broken":true}'},
                        {"type": "response.completed", "response": {"usage": {}}},
                    ]
                ),
            ),
            httpx.Response(
                200,
                text=_sse(
                    [
                        {"type": "response.output_text.delta", "delta": valid},
                        {"type": "response.completed", "response": {"usage": {}}},
                    ]
                ),
            ),
        ]
    )
    seen: list[str] = []

    async def callback(kind: str, _: dict[str, Any]) -> None:
        seen.append(kind)

    model = ArkResponsesChatModel(
        model="test",
        api_key="secret",
        responses_url=URL,
        structured_output_max_retries=1,
        retry_base_seconds=0.0001,
        max_retries=0,
    )
    result = await model.structured(
        phase="intent_router",
        system_prompt="original system prompt",
        input_text="original input",
        output_model=TaskSpecOutput,
        thinking=True,
        retry_strategy="restart",
        event_callback=callback,
    )
    await model.aclose()

    assert result.user_facing_message == "已重新判断。"
    assert route.call_count == 2
    first_payload = json.loads(route.calls[0].request.content)
    retry_payload = json.loads(route.calls[1].request.content)
    assert first_payload["thinking"] == {"type": "enabled"}
    assert retry_payload["thinking"] == {"type": "disabled"}
    assert retry_payload["input"] == first_payload["input"]
    assert retry_payload["input"][0]["content"] == "original system prompt"
    assert "structured_retry" in seen
    assert "structured_repair" not in seen


@pytest.mark.contract
@respx.mock
async def test_transient_transport_retry_forces_thinking_off() -> None:
    valid = json.dumps(
        {
            "user_facing_message": "ok",
            "artifact": {
                "topic": "t",
                "audience": "a",
                "goal": "g",
                "tone": "x",
                "length": "l",
                "other_requirements": [],
            },
        }
    )
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, text="rate limited"),
            httpx.Response(
                200,
                text=_sse(
                    [
                        {"type": "response.output_text.delta", "delta": valid},
                        {"type": "response.completed", "response": {"usage": {}}},
                    ]
                ),
            ),
        ]
    )
    model = ArkResponsesChatModel(
        model="test",
        api_key="secret",
        responses_url=URL,
        max_retries=1,
        retry_base_seconds=0.0001,
    )
    result = await model.structured(
        phase="task_spec",
        system_prompt="prompt",
        input_text="input",
        output_model=TaskSpecOutput,
        thinking=True,
    )
    await model.aclose()

    assert result.user_facing_message == "ok"
    assert json.loads(route.calls[0].request.content)["thinking"] == {"type": "enabled"}
    assert json.loads(route.calls[1].request.content)["thinking"] == {"type": "disabled"}


@pytest.mark.contract
@respx.mock
async def test_empty_generation_output_restarts_original_task_instead_of_repairing() -> None:
    valid = json.dumps(
        {
            "user_facing_message": "ok",
            "artifact": {
                "topic": "t",
                "audience": "a",
                "goal": "g",
                "tone": "x",
                "length": "l",
                "other_requirements": [],
            },
        }
    )
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                text=_sse([{"type": "response.completed", "response": {"usage": {}}}]),
            ),
            httpx.Response(
                200,
                text=_sse(
                    [
                        {"type": "response.output_text.delta", "delta": valid},
                        {"type": "response.completed", "response": {"usage": {}}},
                    ]
                ),
            ),
        ]
    )
    seen: list[str] = []

    async def callback(kind: str, _: dict[str, Any]) -> None:
        seen.append(kind)

    model = ArkResponsesChatModel(
        model="test",
        api_key="secret",
        responses_url=URL,
        structured_output_max_repairs=1,
        retry_base_seconds=0.0001,
        max_retries=0,
    )
    result = await model.structured(
        phase="task_spec",
        system_prompt="original system prompt",
        input_text="original input",
        output_model=TaskSpecOutput,
        thinking=True,
        retry_strategy="repair",
        event_callback=callback,
    )
    await model.aclose()

    assert result.user_facing_message == "ok"
    first_payload = json.loads(route.calls[0].request.content)
    retry_payload = json.loads(route.calls[1].request.content)
    assert retry_payload["input"] == first_payload["input"]
    assert retry_payload["thinking"] == {"type": "disabled"}
    assert "structured_retry" in seen
    assert "structured_repair" not in seen


@pytest.mark.contract
@respx.mock
async def test_web_search_collects_provider_queries_annotations_and_strict_output() -> None:
    output = {
        "factual": {"summary": "事实摘要", "urls": ["https://example.com/fact"]},
        "resource": {"summary": "", "urls": []},
        "creative_reference": {"summary": "", "urls": []},
        "popular_culture": {"summary": "流行表达", "urls": ["https://example.com/meme"]},
    }
    text = json.dumps(output, ensure_ascii=False)
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "id": "ws_1",
                            "type": "web_search_call",
                            "status": "completed",
                            "action": {"type": "search", "query": "牛来 网络热梗 来源"},
                        },
                    },
                    {
                        "type": "response.output_text.annotation.added",
                        "annotation": {
                            "type": "url_citation",
                            "url": "https://example.com/fact",
                            "title": "事实来源",
                            "site_name": "示例站点",
                            "summary": "搜索摘要",
                        },
                    },
                    {"type": "response.output_text.delta", "delta": text[:30]},
                    {"type": "response.output_text.delta", "delta": text[30:]},
                    {"type": "response.completed", "response": {"usage": {"total_tokens": 42}}},
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    seen: list[tuple[str, dict[str, Any]]] = []

    async def callback(kind: str, data: dict[str, Any]) -> None:
        seen.append((kind, data))

    model = ArkResponsesChatModel(
        model="test",
        api_key="secret",
        responses_url=URL,
        event_callback=callback,
        max_retries=0,
    )
    result = await model.web_search(
        call_id="call_1",
        phase="web_material",
        system_prompt="# 角色与任务\n搜索素材",
        input_text="input",
        output_model=WebMaterialOutput,
        max_keyword=4,
    )
    await model.aclose()

    assert cast(WebMaterialOutput, result.output).factual.summary == "事实摘要"
    assert result.queries == ["牛来 网络热梗 来源"]
    assert result.annotations[0]["url"] == "https://example.com/fact"
    assert (
        "web_search_call",
        {
            "phase": "web_material",
            "call_id": "call_1",
            "id": "ws_1",
            "status": "completed",
            "query": "牛来 网络热梗 来源",
        },
    ) in seen
    request_payload = json.loads(route.calls.last.request.content)
    assert request_payload["tools"] == [{"type": "web_search", "max_keyword": 4}]
    assert request_payload["tool_choice"] == {"type": "web_search"}
    assert request_payload["max_tool_calls"] == 4
    assert request_payload["thinking"] == {"type": "disabled"}
    assert request_payload["text"]["format"]["strict"] is True
    assert request_payload["text"]["format"]["schema"]["properties"]["factual"]["description"]


@pytest.mark.contract
@respx.mock
async def test_web_search_restarts_after_invalid_structured_output() -> None:
    output = {
        "factual": {"summary": "事实摘要", "urls": ["https://example.com/fact"]},
        "resource": {"summary": "", "urls": []},
        "creative_reference": {"summary": "", "urls": []},
        "popular_culture": {"summary": "", "urls": []},
    }
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                text=_sse(
                    [
                        {"type": "response.output_text.delta", "delta": '{"broken":true}'},
                        {"type": "response.completed", "response": {"usage": {}}},
                    ]
                ),
            ),
            httpx.Response(
                200,
                text=_sse(
                    [
                        {
                            "type": "response.output_text.delta",
                            "delta": json.dumps(output, ensure_ascii=False),
                        },
                        {"type": "response.completed", "response": {"usage": {}}},
                    ]
                ),
            ),
        ]
    )
    seen: list[str] = []

    async def callback(kind: str, _: dict[str, Any]) -> None:
        seen.append(kind)

    model = ArkResponsesChatModel(
        model="test",
        api_key="secret",
        responses_url=URL,
        structured_output_max_retries=1,
        retry_base_seconds=0.0001,
        max_retries=0,
    )
    result = await model.web_search(
        phase="web_material",
        system_prompt="search prompt",
        input_text="search input",
        output_model=WebMaterialOutput,
        max_keyword=4,
        event_callback=callback,
    )
    await model.aclose()

    assert cast(WebMaterialOutput, result.output).factual.summary == "事实摘要"
    assert route.call_count == 2
    assert json.loads(route.calls[0].request.content)["thinking"] == {"type": "disabled"}
    retry_payload = json.loads(route.calls[1].request.content)
    assert retry_payload["thinking"] == {"type": "disabled"}
    assert retry_payload["input"][0]["content"] == "search prompt"
    assert "structured_retry" in seen
