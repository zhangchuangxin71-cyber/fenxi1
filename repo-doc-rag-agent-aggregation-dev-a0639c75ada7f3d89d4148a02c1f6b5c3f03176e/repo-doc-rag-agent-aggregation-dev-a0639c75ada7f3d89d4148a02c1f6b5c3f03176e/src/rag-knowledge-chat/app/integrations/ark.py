from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.chat.models import RouteDecision
from app.chat.prompts import ROUTE_RESPONSE_FORMAT, build_route_messages
from app.platform.errors import ArkCallError, ArkResponseError


@dataclass(frozen=True, slots=True)
class AnswerThinkingStarted:
    pass


@dataclass(frozen=True, slots=True)
class AnswerReasoningDelta:
    delta: str


@dataclass(frozen=True, slots=True)
class AnswerTextDelta:
    delta: str


AnswerStreamEvent = AnswerThinkingStarted | AnswerReasoningDelta | AnswerTextDelta


class ArkClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        thinking_type: str = "disabled",
        timeout_seconds: float = 60.0,
        sdk_client: Any | None = None,
    ) -> None:
        self._client = sdk_client or AsyncOpenAI(
            api_key=api_key or "missing-ark-api-key",
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=1,
        )
        self._model = model
        self._thinking_type = thinking_type

    @staticmethod
    def _route_extra_body() -> dict[str, Any]:
        return {"thinking": {"type": "disabled"}}

    def _answer_extra_body(self) -> dict[str, Any]:
        return {"thinking": {"type": self._thinking_type}}

    async def decide(self, messages: list[dict[str, str]]) -> RouteDecision:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=build_route_messages(messages),
                temperature=0,
                max_tokens=2048,
                response_format=ROUTE_RESPONSE_FORMAT,
                extra_body=self._route_extra_body(),
            )
            content = response.choices[0].message.content or ""
            data = json.loads(content)
            return RouteDecision.model_validate(data)
        except (json.JSONDecodeError, ValidationError, AttributeError, IndexError, TypeError) as exc:
            raise ArkResponseError("Ark returned an invalid route decision") from exc
        except ArkCallError:
            raise
        except Exception as exc:
            raise ArkCallError("Ark route request failed") from exc

    async def stream_answer(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[AnswerStreamEvent]:
        try:
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                extra_body=self._answer_extra_body(),
            )
            thinking_started = False
            async for chunk in stream:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoningContent", None
                )
                if reasoning and not thinking_started:
                    thinking_started = True
                    yield AnswerThinkingStarted()
                if reasoning:
                    yield AnswerReasoningDelta(delta=reasoning)
                content = getattr(delta, "content", None)
                if content:
                    yield AnswerTextDelta(delta=content)
        except Exception as exc:
            raise ArkCallError("Ark answer request failed") from exc

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close:
            await close()
