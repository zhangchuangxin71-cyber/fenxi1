from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm.gateway import LLMCallError, LLMRequest
from app.llm.provider import OpenAICompatibleProvider


class _Completions:
    def __init__(self, response: object) -> None:
        self.response = response
        self.kwargs: dict = {}

    async def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        return self.response


def _sdk(response: object) -> SimpleNamespace:
    completions = _Completions(response)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions), completions=completions)


@pytest.mark.asyncio
async def test_provider_parses_json_and_disables_thinking() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content='{"grade":"accept"}'),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4, total_tokens=15),
        _request_id="provider-request",
    )
    sdk = _sdk(response)
    provider = OpenAICompatibleProvider(sdk_client=sdk)

    result = await provider.complete_json(
        LLMRequest(
            request_id="run-1",
            phase="classifier",
            messages=[{"role": "user", "content": "test"}],
            model="model",
            max_tokens=100,
            response_format={"type": "json_object"},
        )
    )

    assert result.data == {"grade": "accept"}
    assert result.usage.total_tokens == 15
    assert result.request_id == "provider-request"
    assert result.raw_output == {"content": '{"grade":"accept"}'}
    assert sdk.completions.kwargs["temperature"] == 0
    assert sdk.completions.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_provider_uses_siliconflow_non_thinking_field() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content='{"grade":"accept"}'),
                finish_reason="stop",
            )
        ],
        usage=None,
    )
    sdk = _sdk(response)
    provider = OpenAICompatibleProvider(
        base_url="https://api.siliconflow.cn/v1",
        sdk_client=sdk,
    )

    await provider.complete_json(
        LLMRequest(
            request_id="run-1",
            phase="classifier",
            messages=[{"role": "user", "content": "test"}],
            model="Pro/zai-org/GLM-5",
            max_tokens=100,
            response_format={"type": "json_object"},
        )
    )

    assert sdk.completions.kwargs["extra_body"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_provider_marks_non_json_as_invalid_response() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="not json"), finish_reason="stop")
        ],
        usage=None,
    )
    provider = OpenAICompatibleProvider(sdk_client=_sdk(response))

    with pytest.raises(LLMCallError) as error:
        await provider.complete_json(
            LLMRequest(
                request_id="run-1",
                phase="classifier",
                messages=[],
                model="model",
                max_tokens=100,
            )
        )

    assert error.value.error_category == "invalid_response"
    assert error.value.debug_details == {"raw_content": "not json"}


@pytest.mark.asyncio
async def test_provider_sends_strict_tools_and_parses_openai_tool_calls() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(
                                name="scan_node_tree",
                                arguments='{"root_node_id":"n1","level":1}',
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )
    sdk = _sdk(response)
    provider = OpenAICompatibleProvider(sdk_client=sdk)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "scan_node_tree",
                "description": "扫描指定目录节点的下一级节点。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "root_node_id": {"type": "string", "description": "父节点 ID。"},
                        "level": {"type": "integer", "description": "向下扫描层数。"},
                    },
                    "required": ["root_node_id", "level"],
                    "additionalProperties": False,
                },
            },
        }
    ]

    result = await provider.complete_json(
        LLMRequest(
            request_id="run-1",
            phase="tree_navigation",
            messages=[{"role": "user", "content": "test"}],
            model="model",
            max_tokens=100,
            tools=tools,
            tool_choice="required",
            parallel_tool_calls=True,
            collect_usage=False,
        )
    )

    assert result.data == {}
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "call-1"
    assert result.tool_calls[0].name == "scan_node_tree"
    assert result.tool_calls[0].arguments == {"root_node_id": "n1", "level": 1}
    assert result.raw_output == {
        "tool_calls": [
            {
                "call_id": "call-1",
                "name": "scan_node_tree",
                "arguments": {"root_node_id": "n1", "level": 1},
            }
        ]
    }
    assert sdk.completions.kwargs["tools"] == tools
    assert sdk.completions.kwargs["tool_choice"] == "required"
    assert sdk.completions.kwargs["parallel_tool_calls"] is True
    assert "response_format" not in sdk.completions.kwargs


@pytest.mark.asyncio
async def test_provider_does_not_read_usage_when_collection_is_disabled() -> None:
    class ForbiddenUsage:
        def __getattribute__(self, name: str) -> object:
            raise AssertionError(f"usage field must not be read: {name}")

    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content='{"grade":"accept"}'),
                finish_reason="stop",
            )
        ],
        usage=ForbiddenUsage(),
    )
    provider = OpenAICompatibleProvider(sdk_client=_sdk(response))

    result = await provider.complete_json(
        LLMRequest(
            request_id="run-1",
            phase="classifier",
            messages=[],
            model="model",
            max_tokens=100,
            response_format={"type": "json_object"},
            collect_usage=False,
        )
    )

    assert result.usage.total_tokens == 0
