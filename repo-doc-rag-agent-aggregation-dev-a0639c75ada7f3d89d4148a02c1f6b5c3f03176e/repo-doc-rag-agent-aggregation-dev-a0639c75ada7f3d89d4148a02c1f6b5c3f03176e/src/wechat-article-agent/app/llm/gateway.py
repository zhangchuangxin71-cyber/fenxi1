from __future__ import annotations

import asyncio
import contextvars
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, TypeVar

from pydantic import BaseModel

from app.agent_engine.contracts import EngineEventCallback, ModelTurn
from app.config import AppSettings
from app.core.errors import AppError
from app.core.rate_limit import AsyncTokenBucket
from app.core.resilience import CircuitBreaker
from app.debug.trace import current as current_debug_trace
from app.debug.trace import record
from app.llm.ark_responses import ArkResponsesChatModel, ArkWebSearchResult, EventCallback

ResultT = TypeVar("ResultT")
OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(slots=True)
class _Call[ResultT]:
    run_id: str
    operation: Callable[[], Awaitable[ResultT]]
    future: asyncio.Future[ResultT]
    started: asyncio.Future[None]
    context: contextvars.Context


class FairScheduler:
    def __init__(self, *, concurrency: int, queue_size: int, per_run: int, wait_timeout: float) -> None:
        self.concurrency = concurrency
        self.queue_size = queue_size
        self.per_run = per_run
        self.wait_timeout = wait_timeout
        self._queues: dict[str, deque[_Call[Any]]] = {}
        self._rotation: deque[str] = deque()
        self._in_flight: dict[str, int] = {}
        self._condition = asyncio.Condition()
        self._workers: list[asyncio.Task[None]] = []
        self._active: dict[asyncio.Future[Any], asyncio.Future[Any]] = {}
        self._queued = 0
        self._closed = False

    async def submit(self, run_id: str, operation: Callable[[], Awaitable[ResultT]]) -> ResultT:
        await self._start()
        future: asyncio.Future[ResultT] = asyncio.get_running_loop().create_future()
        started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        call = _Call(run_id, operation, future, started, contextvars.copy_context())
        async with self._condition:
            if self._queued >= self.queue_size:
                raise AppError(503, "ARK_QUEUE_FULL", "Ark call queue is full.", True)
            queue = self._queues.setdefault(run_id, deque())
            queue.append(call)
            self._queued += 1
            if run_id not in self._rotation:
                self._rotation.append(run_id)
            self._condition.notify_all()
        try:
            await asyncio.wait_for(asyncio.shield(started), self.wait_timeout)
            return await future
        except TimeoutError as exc:
            await self._withdraw(call)
            raise AppError(503, "ARK_QUEUE_TIMEOUT", "Ark call waited too long.", True) from exc
        except asyncio.CancelledError:
            await self._withdraw(call)
            raise

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def _start(self) -> None:
        if self._workers:
            return
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]

    async def _withdraw(self, target: _Call[Any]) -> None:
        active_task: asyncio.Future[Any] | None = None
        async with self._condition:
            active_task = self._active.get(target.future)
            if active_task is not None:
                active_task.cancel()
            queue = self._queues.get(target.run_id)
            if queue is None:
                pass
            else:
                try:
                    queue.remove(target)
                    self._queued -= 1
                except ValueError:
                    pass
                if not queue:
                    self._queues.pop(target.run_id, None)
                    self._rotation = deque(value for value in self._rotation if value != target.run_id)
        if active_task is not None:
            await asyncio.gather(active_task, return_exceptions=True)

    async def _take(self) -> _Call[Any] | None:
        async with self._condition:
            while True:
                if self._closed:
                    return None
                for _ in range(len(self._rotation)):
                    run_id = self._rotation.popleft()
                    queue = self._queues.get(run_id)
                    if not queue:
                        continue
                    if self._in_flight.get(run_id, 0) >= self.per_run:
                        self._rotation.append(run_id)
                        continue
                    call = queue.popleft()
                    self._queued -= 1
                    self._in_flight[run_id] = self._in_flight.get(run_id, 0) + 1
                    if not call.started.done():
                        call.started.set_result(None)
                    if queue:
                        self._rotation.append(run_id)
                    else:
                        self._queues.pop(run_id, None)
                    return call
                await self._condition.wait()

    async def _worker(self) -> None:
        while call := await self._take():
            try:
                # Workers are long-lived and otherwise keep the Context captured by the
                # first submitter. Each operation must inherit its LangGraph stream
                # writer and debug collector from the node that enqueued it.
                operation_task: asyncio.Future[Any] = call.context.run(
                    lambda: asyncio.ensure_future(call.operation())
                )
                async with self._condition:
                    self._active[call.future] = operation_task
                result = await operation_task
                if not call.future.done():
                    call.future.set_result(result)
            except asyncio.CancelledError:
                if not call.future.done():
                    call.future.cancel()
            except Exception as exc:
                if not call.future.done():
                    call.future.set_exception(exc)
            finally:
                async with self._condition:
                    self._active.pop(call.future, None)
                    self._in_flight[call.run_id] = max(0, self._in_flight.get(call.run_id, 1) - 1)
                    if not self._in_flight[call.run_id]:
                        self._in_flight.pop(call.run_id, None)
                    self._condition.notify_all()


class LLMGateway:
    _THINKING_PHASES = {"task_spec", "outline", "article"}
    _OUTPUT_REPAIR_PHASES = {"task_spec", "outline", "article"}

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.model = ArkResponsesChatModel.from_settings(settings)
        self.search_model = ArkResponsesChatModel.from_settings(settings, model=settings.ark_model_fast)
        self.scheduler = FairScheduler(
            concurrency=settings.ark_max_concurrency,
            queue_size=settings.ark_max_queued_calls,
            per_run=settings.ark_per_run_max_in_flight,
            wait_timeout=settings.ark_queue_wait_timeout_seconds,
        )
        self.breaker = CircuitBreaker(
            enabled=settings.ark_circuit_breaker_enabled,
            threshold=settings.ark_circuit_failure_threshold,
            recovery_seconds=settings.ark_circuit_recovery_seconds,
            error_code="ARK_CIRCUIT_OPEN",
            provider_name="Ark",
        )
        self.rate_limiter = AsyncTokenBucket(
            per_minute=settings.ark_rate_limit_per_minute,
            burst=settings.ark_rate_limit_burst,
            error_code="ARK_RATE_LIMITED",
            message="Ark call rate exceeded the configured abnormal-traffic limit.",
        )

    async def structured(
        self,
        *,
        run_id: str,
        phase: str,
        system_prompt: str,
        input_text: str,
        output_model: type[OutputT],
        event_callback: EventCallback | None = None,
        thinking: bool | None = None,
    ) -> OutputT:
        thinking_enabled = (
            phase in self._THINKING_PHASES and phase in self.settings.thinking_phases
            if thinking is None
            else thinking
        )
        call_id = f"llm_{phase}_{id(input_text):x}"
        started = monotonic()
        record(
            "llm_calls",
            {
                "id": call_id,
                "phase": phase,
                "model": self.model.model_name,
                "thinking": thinking_enabled,
                "system_prompt": system_prompt,
                "input": input_text,
                "started_at": datetime.now(UTC).isoformat(),
            },
        )

        async def operation() -> OutputT:
            await self.rate_limiter.take()
            probe = await self.breaker.before_call()
            try:
                result = await self.model.structured(
                    call_id=call_id,
                    phase=phase,
                    system_prompt=system_prompt,
                    input_text=input_text,
                    output_model=output_model,
                    thinking=thinking_enabled,
                    retry_strategy=("repair" if phase in self._OUTPUT_REPAIR_PHASES else "restart"),
                    event_callback=event_callback,
                )
            except asyncio.CancelledError:
                await self.breaker.failure(counts=False, recovery_probe=probe)
                raise
            except AppError as exc:
                await self.breaker.failure(
                    counts=_counts_toward_ark_circuit(exc),
                    recovery_probe=probe,
                )
                raise
            await self.breaker.success(probe)
            return result

        try:
            result = await self.scheduler.submit(run_id, operation)
        except Exception as exc:
            record(
                "errors",
                {
                    "source": "llm",
                    "call_id": call_id,
                    "phase": phase,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "elapsed_ms": round((monotonic() - started) * 1000, 3),
                },
            )
            raise
        record(
            "llm_calls",
            {
                "id": call_id,
                "phase": phase,
                "status": "completed",
                "raw_output": result.model_dump(),
                "elapsed_ms": round((monotonic() - started) * 1000, 3),
            },
        )
        return result

    async def web_search[OutputT: BaseModel](
        self,
        *,
        run_id: str,
        phase: str,
        system_prompt: str,
        input_text: str,
        output_model: type[OutputT] | None = None,
        max_keyword: int = 2,
        event_callback: EventCallback | None = None,
    ) -> ArkWebSearchResult[OutputT | str]:
        call_id = f"llm_{phase}_{id(input_text):x}"
        tool_call_id = f"tool_{phase}_{id(input_text):x}"
        started = monotonic()
        record(
            "llm_calls",
            {
                "id": call_id,
                "phase": phase,
                "model": self.search_model.model_name,
                "thinking": False,
                "system_prompt": system_prompt,
                "input": input_text,
                "started_at": datetime.now(UTC).isoformat(),
            },
        )
        record(
            "tool_calls",
            {
                "id": tool_call_id,
                "kind": "tool",
                "tool": "ark.web_search",
                "status": "running",
                "arguments": {"phase": phase, "max_keyword": max_keyword, "input": input_text},
            },
        )

        async def operation() -> ArkWebSearchResult[OutputT | str]:
            await self.rate_limiter.take()
            probe = await self.breaker.before_call()
            try:
                result = await self.search_model.web_search(
                    call_id=call_id,
                    phase=phase,
                    system_prompt=system_prompt,
                    input_text=input_text,
                    output_model=output_model,
                    max_keyword=max_keyword,
                    event_callback=event_callback,
                )
            except asyncio.CancelledError:
                await self.breaker.failure(counts=False, recovery_probe=probe)
                raise
            except AppError as exc:
                await self.breaker.failure(
                    counts=_counts_toward_ark_circuit(exc),
                    recovery_probe=probe,
                )
                raise
            await self.breaker.success(probe)
            return result

        try:
            result = await self.scheduler.submit(run_id, operation)
        except Exception as exc:
            record(
                "tool_calls",
                {
                    "id": tool_call_id,
                    "kind": "tool",
                    "tool": "ark.web_search",
                    "status": "failed",
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                    "elapsed_ms": round((monotonic() - started) * 1000, 3),
                },
            )
            raise
        elapsed_ms = round((monotonic() - started) * 1000, 3)
        record(
            "llm_calls",
            {
                "id": call_id,
                "phase": phase,
                "status": "completed",
                "raw_output": (
                    result.output.model_dump() if isinstance(result.output, BaseModel) else result.output
                ),
                "elapsed_ms": elapsed_ms,
            },
        )
        record(
            "tool_calls",
            {
                "id": tool_call_id,
                "kind": "tool",
                "tool": "ark.web_search",
                "status": "completed",
                "result": {
                    "queries": result.queries,
                    "annotations": result.annotations,
                    "output": (
                        result.output.model_dump() if isinstance(result.output, BaseModel) else result.output
                    ),
                },
                "elapsed_ms": elapsed_ms,
            },
        )
        return result

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
    ) -> ModelTurn:
        """Schedule one generic Engine model turn with thinking disabled."""

        started = monotonic()
        if current_debug_trace() is not None:
            record(
                "llm_calls",
                {
                    "id": call_id,
                    "phase": phase,
                    "model": self.model.model_name,
                    "thinking": False,
                    "input": input_items,
                    "tools": tools,
                    "started_at": datetime.now(UTC).isoformat(),
                },
            )

        async def operation() -> ModelTurn:
            await self.rate_limiter.take()
            probe = await self.breaker.before_call()
            try:
                result = await self.model.agent_turn(
                    run_id=run_id,
                    call_id=call_id,
                    phase=phase,
                    input_items=input_items,
                    tools=tools,
                    output_model=output_model,
                    max_tool_calls=max_tool_calls,
                    timeout_seconds=timeout_seconds,
                    event_callback=event_callback,
                )
            except asyncio.CancelledError:
                await self.breaker.failure(counts=False, recovery_probe=probe)
                raise
            except AppError as exc:
                await self.breaker.failure(
                    counts=_counts_toward_ark_circuit(exc),
                    recovery_probe=probe,
                )
                raise
            await self.breaker.success(probe)
            return result

        try:
            result = await self.scheduler.submit(run_id=run_id, operation=operation)
        except Exception as exc:
            record(
                "errors",
                {
                    "source": "agent_model",
                    "call_id": call_id,
                    "phase": phase,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "elapsed_ms": round((monotonic() - started) * 1000, 3),
                },
            )
            raise
        if current_debug_trace() is not None:
            record(
                "llm_calls",
                {
                    "id": call_id,
                    "phase": phase,
                    "status": "completed",
                    "raw_output": {
                        "text": result.text,
                        "tool_calls": [
                            {
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments": call.arguments,
                            }
                            for call in result.tool_calls
                        ],
                    },
                    "usage": result.usage,
                    "elapsed_ms": round((monotonic() - started) * 1000, 3),
                },
            )
        return result

    async def close(self) -> None:
        await self.scheduler.close()
        await self.model.aclose()
        await self.search_model.aclose()


def _counts_toward_ark_circuit(exc: AppError) -> bool:
    if exc.code in {
        "ARK_UPSTREAM_ERROR",
        "ARK_RESPONSE_FAILED",
        "ARK_STREAM_TIMEOUT",
        "ARK_NETWORK_ERROR",
        "ARK_STREAM_PROTOCOL_ERROR",
    }:
        return True
    return exc.code == "ARK_STRUCTURED_OUTPUT_INVALID" and exc.details.get("empty_output") is True
