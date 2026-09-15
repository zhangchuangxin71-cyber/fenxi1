from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class LLMProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> dict:
        """Return a non-streaming chat response as {text, usage, raw}."""

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> AsyncIterator[dict]:
        """Yield streaming chunks as {delta, usage?}."""
