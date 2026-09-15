from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel

EngineEventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]
CompletionValidator = Callable[[BaseModel, "RunContext"], Awaitable[None] | None]
CompletionResolver = Callable[[BaseModel, "RunContext"], Any]


@dataclass(frozen=True, slots=True)
class AgentBudget:
    max_steps: int = 8
    max_tool_calls: int = 12
    max_context_tokens: int = 131_072
    context_reserve_tokens: int = 8_192
    recent_full_rounds: int = 3
    max_compactions: int = 2
    call_timeout_seconds: float = 120.0
    total_timeout_seconds: float = 300.0
    max_same_tool_failures: int = 2
    max_tool_result_chars: int = 24_000


class CancellationToken:
    """Cooperative cancellation shared by model and tool handlers."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError

    async def wait(self) -> None:
        await self._event.wait()


@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str
    provider_item: dict[str, Any]


@dataclass(slots=True)
class ModelTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    response_items: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    text_emitted: bool = False


@dataclass(slots=True)
class RunContext:
    node_name: str
    run_id: str
    cancellation: CancellationToken
    objects: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    event_callback: EngineEventCallback | None = None

    async def emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.event_callback is None:
            return
        result = self.event_callback(kind, payload)
        if asyncio.iscoroutine(result):
            await result


class AgentModel(Protocol):
    async def agent_turn(
        self,
        *,
        run_id: str,
        call_id: str,
        phase: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        output_model: type[BaseModel],
        max_tool_calls: int,
        timeout_seconds: float,
        event_callback: EngineEventCallback | None = None,
    ) -> ModelTurn: ...


@dataclass(frozen=True, slots=True)
class AgentRequest:
    node_name: str
    run_id: str
    task: str
    system_prompt: str
    input_context: Mapping[str, Any]
    skill_allowlist: tuple[str, ...]
    tool_allowlist: tuple[str, ...]
    completion_model: type[BaseModel]
    budget: AgentBudget = AgentBudget()
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    event_callback: EngineEventCallback | None = None
    debug: bool = False
    private_context: Mapping[str, Any] = field(default_factory=dict)
    completion_validator: CompletionValidator | None = None
    completion_resolver: CompletionResolver | None = None


@dataclass(slots=True)
class AgentResult:
    status: Literal["completed", "degraded", "failed"]
    output: dict[str, Any] | None = None
    user_facing_message: str | None = None
    candidate_id: str | None = None
    candidate_hash: str | None = None
    steps: int = 0
    tool_calls: int = 0
    compressed: bool = False
    fallback_reason: str | None = None
