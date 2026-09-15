from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.llm.base import LLMProvider


class DoubaoProvider(LLMProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.ark_api_key or "missing-ark-api-key",
            base_url=settings.ark_base_url,
            timeout=settings.llm_timeout_seconds,
        )

    @staticmethod
    def _extra_body() -> dict[str, Any]:
        body: dict[str, Any] = {"thinking": {"type": settings.doubao_thinking_type}}
        if settings.doubao_reasoning_effort:
            body["reasoning_effort"] = settings.doubao_reasoning_effort
        return body

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> dict:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format
        kwargs["extra_body"] = self._extra_body()
        resp = await self.client.chat.completions.create(**kwargs)
        usage = resp.usage
        message = resp.choices[0].message
        return {
            "text": message.content or "",
            "reasoning_text": getattr(message, "reasoning_content", None)
            or getattr(message, "reasoningContent", None)
            or "",
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
                "total_tokens": getattr(usage, "total_tokens", 0) if usage else 0,
            },
            "raw": resp,
        }

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> AsyncIterator[dict]:
        stream = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=self._extra_body(),
        )
        async for chunk in stream:
            delta = ""
            reasoning_delta = ""
            if chunk.choices:
                delta_obj = chunk.choices[0].delta
                if getattr(delta_obj, "content", None):
                    delta = delta_obj.content
                reasoning_delta = (
                    getattr(delta_obj, "reasoning_content", None)
                    or getattr(delta_obj, "reasoningContent", None)
                    or ""
                )
            usage = None
            if chunk.usage:
                usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                }
            if delta or reasoning_delta or usage:
                yield {"delta": delta, "reasoning_delta": reasoning_delta, "usage": usage}
