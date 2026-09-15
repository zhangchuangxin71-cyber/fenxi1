from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Any, Protocol

from app.llm.circuit_breaker import CircuitOpenError, LLMCircuitBreaker


@dataclass(frozen=True, slots=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMRequest:
    request_id: str
    phase: str
    messages: list[dict[str, Any]]
    model: str
    max_tokens: int
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    collect_usage: bool = True


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMResult:
    data: dict[str, Any]
    usage: LLMUsage
    tool_calls: tuple[LLMToolCall, ...] = ()
    request_id: str | None = None
    queue_wait_ms: int = 0
    provider_duration_ms: int = 0
    raw_output: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class GatewayRequestStats:
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    queue_wait_ms: int = 0
    provider_duration_ms: int = 0
    phase_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GatewayCallRecord:
    model: str
    phase: str
    group_index: int | None = None
    group_count: int | None = None
    queue_wait_ms: int = 0
    provider_duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider_request_id: str | None = None
    status: str = "ok"
    error_category: str | None = None
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] | None = None


class LLMCallError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_category: str = "connect",
        debug_details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_category = error_category
        self.debug_details = debug_details or {}


class LLMProvider(Protocol):
    async def complete_json(self, request: LLMRequest) -> LLMResult: ...


@dataclass(slots=True)
class _QueuedCall:
    request: LLMRequest
    operation: Callable[[], Awaitable[LLMResult]]
    future: asyncio.Future[LLMResult]
    queued_at: float | None


class _FairScheduler:
    def __init__(
        self, *, max_concurrency: int, max_queued_calls: int, per_request_max_in_flight: int
    ) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_queued_calls = max(1, int(max_queued_calls))
        self.per_request_max_in_flight = max(1, int(per_request_max_in_flight))
        self._condition = asyncio.Condition()
        self._queues: dict[str, deque[_QueuedCall]] = {}
        self._rotation: deque[str] = deque()
        self._in_flight: dict[str, int] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._active_tasks: dict[str, set[asyncio.Task[LLMResult]]] = {}
        self._cancelled_requests: set[str] = set()
        self._queued_count = 0
        self._closed = False

    async def start(self) -> None:
        if not self._workers:
            self._workers = [
                asyncio.create_task(self._worker()) for _ in range(self.max_concurrency)
            ]

    async def submit(
        self, request: LLMRequest, operation: Callable[[], Awaitable[LLMResult]]
    ) -> LLMResult:
        await self.start()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[LLMResult] = loop.create_future()
        call = _QueuedCall(
            request=request,
            operation=operation,
            future=future,
            queued_at=monotonic() if request.collect_usage else None,
        )
        async with self._condition:
            if self._closed:
                raise RuntimeError("LLM scheduler is closed")
            if request.request_id in self._cancelled_requests:
                raise LLMCallError("LLM request was cancelled", error_category="cancelled")
            if self._queued_count >= self.max_queued_calls:
                raise LLMCallError("LLM queue is full", error_category="overload")
            queue = self._queues.setdefault(request.request_id, deque())
            queue.append(call)
            self._queued_count += 1
            if request.request_id not in self._rotation:
                self._rotation.append(request.request_id)
            self._condition.notify_all()
        try:
            return await future
        except asyncio.CancelledError:
            await self._cancel_queued(call)
            raise

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            for queue in self._queues.values():
                while queue:
                    call = queue.popleft()
                    self._queued_count -= 1
                    if not call.future.done():
                        call.future.set_exception(
                            LLMCallError("LLM call cancelled", error_category="cancelled")
                        )
            self._queues.clear()
            self._rotation.clear()
            self._condition.notify_all()
            active = [task for tasks in self._active_tasks.values() for task in tasks]
            for task in active:
                task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()

    async def cancel_request(self, request_id: str) -> None:
        async with self._condition:
            self._cancelled_requests.add(request_id)
            queue = self._queues.pop(request_id, deque())
            while queue:
                call = queue.popleft()
                self._queued_count -= 1
                if not call.future.done():
                    call.future.set_exception(
                        LLMCallError("LLM request was cancelled", error_category="cancelled")
                    )
            self._rotation = deque(
                queued_request_id
                for queued_request_id in self._rotation
                if queued_request_id != request_id
            )
            active = list(self._active_tasks.get(request_id, set()))
            for task in active:
                task.cancel()
            self._condition.notify_all()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    def clear_cancelled(self, request_id: str) -> None:
        self._cancelled_requests.discard(request_id)

    async def _cancel_queued(self, target: _QueuedCall) -> None:
        async with self._condition:
            queue = self._queues.get(target.request.request_id)
            if queue is None:
                return
            try:
                queue.remove(target)
            except ValueError:
                return
            self._queued_count -= 1
            if not queue:
                self._queues.pop(target.request.request_id, None)
                try:
                    self._rotation.remove(target.request.request_id)
                except ValueError:
                    pass
            self._condition.notify_all()

    async def _take(self) -> _QueuedCall | None:
        async with self._condition:
            while True:
                if self._closed and self._queued_count == 0:
                    return None
                for _ in range(len(self._rotation)):
                    request_id = self._rotation.popleft()
                    queue = self._queues.get(request_id)
                    if not queue:
                        self._queues.pop(request_id, None)
                        continue
                    if self._in_flight.get(request_id, 0) >= self.per_request_max_in_flight:
                        self._rotation.append(request_id)
                        continue
                    call = queue.popleft()
                    self._queued_count -= 1
                    self._in_flight[request_id] = self._in_flight.get(request_id, 0) + 1
                    if queue:
                        self._rotation.append(request_id)
                    else:
                        self._queues.pop(request_id, None)
                    return call
                await self._condition.wait()

    async def _worker(self) -> None:
        while True:
            call = await self._take()
            if call is None:
                return
            request_id = call.request.request_id
            if request_id in self._cancelled_requests:
                if not call.future.done():
                    call.future.set_exception(
                        LLMCallError("LLM request was cancelled", error_category="cancelled")
                    )
                async with self._condition:
                    self._in_flight[request_id] = max(0, self._in_flight.get(request_id, 1) - 1)
                    if not self._in_flight[request_id]:
                        self._in_flight.pop(request_id, None)
                    self._condition.notify_all()
                continue
            try:
                queue_wait_ms = (
                    int((monotonic() - call.queued_at) * 1000)
                    if call.queued_at is not None
                    else 0
                )
                operation_task = asyncio.create_task(call.operation())
                self._active_tasks.setdefault(request_id, set()).add(operation_task)
                result = await operation_task
                if call.request.collect_usage:
                    result = replace(
                        result,
                        queue_wait_ms=max(result.queue_wait_ms, queue_wait_ms),
                    )
                if not call.future.done():
                    call.future.set_result(result)
            except asyncio.CancelledError:
                if not call.future.done():
                    call.future.set_exception(
                        LLMCallError("LLM request was cancelled", error_category="cancelled")
                    )
            except Exception as exc:
                if not call.future.done():
                    call.future.set_exception(exc)
            finally:
                async with self._condition:
                    active = self._active_tasks.get(request_id)
                    if active is not None:
                        active.discard(locals().get("operation_task"))
                        if not active:
                            self._active_tasks.pop(request_id, None)
                    self._in_flight[request_id] = max(0, self._in_flight.get(request_id, 1) - 1)
                    if not self._in_flight[request_id]:
                        self._in_flight.pop(request_id, None)
                    self._condition.notify_all()


class LLMGateway:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        max_concurrency: int,
        max_queued_calls: int,
        per_request_max_in_flight: int,
        circuit_breaker: LLMCircuitBreaker,
    ) -> None:
        self.provider = provider
        self.circuit_breaker = circuit_breaker
        self._scheduler = _FairScheduler(
            max_concurrency=max_concurrency,
            max_queued_calls=max_queued_calls,
            per_request_max_in_flight=per_request_max_in_flight,
        )
        self._stats: dict[str, GatewayRequestStats] = {}
        self._records: dict[str, list[GatewayCallRecord]] = {}
        self._request_counts: dict[str, int] = {}
        self._debug_requests: set[str] = set()

    def begin_request(self, request_id: str, *, debug_enabled: bool) -> None:
        self.clear_stats(request_id)
        self._request_counts[request_id] = 0
        if debug_enabled:
            self._debug_requests.add(request_id)

    async def complete_json(self, request: LLMRequest) -> LLMResult:
        diagnostics_enabled = request.request_id in self._debug_requests
        effective_request = replace(request, collect_usage=diagnostics_enabled)

        async def operation() -> LLMResult:
            self._request_counts[request.request_id] = (
                self._request_counts.get(request.request_id, 0) + 1
            )
            if diagnostics_enabled:
                current = self._stats.get(request.request_id, GatewayRequestStats())
                phase_counts = dict(current.phase_counts)
                phase_counts[request.phase] = phase_counts.get(request.phase, 0) + 1
                self._stats[request.request_id] = replace(
                    current,
                    request_count=current.request_count + 1,
                    phase_counts=phase_counts,
                )
            recovery_probe = self.circuit_breaker.before_call()
            started = monotonic() if diagnostics_enabled else None
            try:
                result = await self.provider.complete_json(effective_request)
            except CircuitOpenError:
                raise
            except LLMCallError as exc:
                self.circuit_breaker.record_failure(exc.error_category)
                raise
            except TimeoutError as exc:
                self.circuit_breaker.record_failure("timeout")
                raise LLMCallError("LLM provider timed out", error_category="timeout") from exc
            except Exception as exc:
                self.circuit_breaker.record_failure("connect")
                raise LLMCallError("LLM provider call failed", error_category="connect") from exc
            self.circuit_breaker.record_success(recovery_probe=recovery_probe)
            if not diagnostics_enabled:
                return replace(result, request_id=result.request_id or request.request_id)
            return replace(
                result,
                request_id=result.request_id or request.request_id,
                provider_duration_ms=result.provider_duration_ms
                or int((monotonic() - started) * 1000),
            )

        try:
            result = await self._scheduler.submit(effective_request, operation)
        except Exception as exc:
            if diagnostics_enabled:
                self._records.setdefault(request.request_id, []).append(
                    _call_record(
                        request=request,
                        status="failed",
                        error_category=getattr(exc, "error_category", "internal"),
                        error=exc,
                    )
                )
            raise
        if diagnostics_enabled:
            self._records.setdefault(request.request_id, []).append(
                _call_record(request=request, result=result)
            )
            current = self._stats.get(request.request_id, GatewayRequestStats())
            self._stats[request.request_id] = GatewayRequestStats(
                request_count=current.request_count,
                prompt_tokens=current.prompt_tokens + result.usage.prompt_tokens,
                completion_tokens=current.completion_tokens + result.usage.completion_tokens,
                total_tokens=current.total_tokens + result.usage.total_tokens,
                queue_wait_ms=current.queue_wait_ms + result.queue_wait_ms,
                provider_duration_ms=current.provider_duration_ms + result.provider_duration_ms,
                phase_counts=dict(current.phase_counts),
            )
        return result

    def stats(self, request_id: str) -> GatewayRequestStats:
        stats = self._stats.get(request_id)
        if stats is not None:
            return stats
        return GatewayRequestStats(request_count=self._request_counts.get(request_id, 0))

    def call_records(self, request_id: str) -> tuple[GatewayCallRecord, ...]:
        return tuple(self._records.get(request_id, ()))

    def clear_stats(self, request_id: str) -> None:
        self._stats.pop(request_id, None)
        self._records.pop(request_id, None)
        self._request_counts.pop(request_id, None)
        self._debug_requests.discard(request_id)
        self._scheduler.clear_cancelled(request_id)

    async def cancel_request(self, request_id: str) -> None:
        await self._scheduler.cancel_request(request_id)

    async def close(self) -> None:
        await self._scheduler.close()


def _call_record(
    *,
    request: LLMRequest,
    result: LLMResult | None = None,
    status: str = "ok",
    error_category: str | None = None,
    error: Exception | None = None,
) -> GatewayCallRecord:
    match = re.search(r":(\d+)/(\d+)$", request.phase)
    request_debug = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": request.messages,
        "response_format": request.response_format,
        "tools": request.tools,
        "tool_choice": request.tool_choice,
        "parallel_tool_calls": request.parallel_tool_calls,
    }
    response_debug = None
    if result is not None:
        response_debug = {
            "data": result.data,
            "tool_calls": [
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                for call in result.tool_calls
            ],
            "raw_output": result.raw_output,
        }
    elif error is not None:
        response_debug = {
            "error": {
                "type": type(error).__name__,
                "message": str(error),
                "category": error_category,
                "details": getattr(error, "debug_details", {}),
            }
        }
    return GatewayCallRecord(
        model=request.model,
        phase=request.phase,
        group_index=int(match.group(1)) if match else None,
        group_count=int(match.group(2)) if match else None,
        queue_wait_ms=int(result.queue_wait_ms) if result else 0,
        provider_duration_ms=int(result.provider_duration_ms) if result else 0,
        prompt_tokens=int(result.usage.prompt_tokens) if result else 0,
        completion_tokens=int(result.usage.completion_tokens) if result else 0,
        provider_request_id=result.request_id if result else None,
        status=status,
        error_category=error_category,
        request=request_debug,
        response=response_debug,
    )
