from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.chat.models import RouteDecision
from app.chat.prompts import ROUTE_RESPONSE_FORMAT
from app.integrations.ark import ArkClient


class FakeCompletions:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeSdk:
    def __init__(self, response) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(response))


@pytest.mark.asyncio
async def test_decide_sends_strict_response_format() -> None:
    message = SimpleNamespace(
        content='{"needs_retrieval":true,"query":["独立问题"],"reason_code":"knowledge_base"}'
    )
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    sdk = FakeSdk(response)
    client = ArkClient(
        api_key="secret",
        base_url="https://ark.example/v3",
        model="model",
        thinking_type="enabled",
        sdk_client=sdk,
    )

    result = await client.decide([{"role": "user", "content": "它呢？"}])

    call = sdk.chat.completions.calls[0]
    assert call["response_format"] == ROUTE_RESPONSE_FORMAT
    assert call["temperature"] == 0
    assert call["max_tokens"] == 2048
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert result.needs_retrieval is True
    assert result.query == ["独立问题"]


def test_route_decision_rejects_blank_preprocessed_query_items() -> None:
    with pytest.raises(ValidationError):
        RouteDecision(
            needs_retrieval=True,
            query=["有效问题", "   "],
            reason_code="knowledge_base",
        )


@pytest.mark.asyncio
async def test_stream_answer_yields_reasoning_and_content() -> None:
    async def stream():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, reasoning_content="隐藏推理一"),
                    finish_reason=None,
                )
            ]
        )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, reasoningContent="隐藏推理二"),
                    finish_reason=None,
                )
            ]
        )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="正文", reasoning_content="隐藏推理"),
                    finish_reason=None,
                )
            ]
        )
        yield SimpleNamespace(choices=[])

    sdk = FakeSdk(stream())
    client = ArkClient(
        api_key="secret",
        base_url="https://ark.example/v3",
        model="model",
        thinking_type="enabled",
        sdk_client=sdk,
    )

    events = [
        item
        async for item in client.stream_answer(
            [{"role": "user", "content": "问题"}],
            temperature=0.7,
            max_tokens=321,
        )
    ]

    assert [type(item).__name__ for item in events] == [
        "AnswerThinkingStarted",
        "AnswerReasoningDelta",
        "AnswerReasoningDelta",
        "AnswerReasoningDelta",
        "AnswerTextDelta",
    ]
    assert [item.delta for item in events[1:4]] == ["隐藏推理一", "隐藏推理二", "隐藏推理"]
    assert events[4].delta == "正文"
    call = sdk.chat.completions.calls[0]
    assert call["temperature"] == 0.7
    assert call["max_tokens"] == 321
    assert call["extra_body"] == {"thinking": {"type": "enabled"}}
