from __future__ import annotations

import asyncio

import pytest

from app.llm.circuit_breaker import CircuitOpenError, LLMCircuitBreaker
from app.llm.gateway import LLMGateway, LLMRequest, LLMResult, LLMUsage


class ConcurrentProvider:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.calls: list[str] = []
        self.requests: list[LLMRequest] = []

    async def complete_json(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.calls.append(request.request_id)
        await asyncio.sleep(0.01)
        self.active -= 1
        return LLMResult(
            data={"ok": True},
            usage=LLMUsage(),
            request_id="provider-id",
            raw_output={"content": '{"ok":true}'},
        )


class CancellableProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def complete_json(self, request: LLMRequest) -> LLMResult:
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return LLMResult(data={}, usage=LLMUsage())


@pytest.mark.asyncio
async def test_gateway_enforces_global_and_per_request_concurrency() -> None:
    provider = ConcurrentProvider()
    gateway = LLMGateway(
        provider=provider,
        max_concurrency=2,
        max_queued_calls=20,
        per_request_max_in_flight=1,
        circuit_breaker=LLMCircuitBreaker(enabled=False),
    )
    try:
        tasks = [
            gateway.complete_json(
                LLMRequest(
                    request_id="request-a" if index < 4 else "request-b",
                    phase="test",
                    messages=[],
                    model="fake",
                    max_tokens=10,
                )
            )
            for index in range(6)
        ]
        results = await asyncio.gather(*tasks)
    finally:
        await gateway.close()

    assert all(result.data == {"ok": True} for result in results)
    assert provider.peak == 2
    assert provider.calls[:2] == ["request-a", "request-b"]


@pytest.mark.asyncio
async def test_gateway_records_time_spent_in_global_queue() -> None:
    provider = ConcurrentProvider()
    gateway = LLMGateway(
        provider=provider,
        max_concurrency=1,
        max_queued_calls=10,
        per_request_max_in_flight=1,
        circuit_breaker=LLMCircuitBreaker(enabled=False),
    )
    gateway.begin_request("same-request", debug_enabled=True)
    try:
        first = asyncio.create_task(
            gateway.complete_json(
                LLMRequest(
                    request_id="same-request",
                    phase="test",
                    messages=[],
                    model="fake",
                    max_tokens=10,
                )
            )
        )
        second = asyncio.create_task(
            gateway.complete_json(
                LLMRequest(
                    request_id="same-request",
                    phase="test",
                    messages=[],
                    model="fake",
                    max_tokens=10,
                )
            )
        )
        _, queued_result = await asyncio.gather(first, second)
    finally:
        await gateway.close()

    assert queued_result.queue_wait_ms >= 5


@pytest.mark.asyncio
async def test_gateway_aggregates_usage_by_external_request_id() -> None:
    provider = ConcurrentProvider()
    gateway = LLMGateway(
        provider=provider,
        max_concurrency=1,
        max_queued_calls=10,
        per_request_max_in_flight=1,
        circuit_breaker=LLMCircuitBreaker(enabled=False),
    )
    try:
        gateway.begin_request("request-a", debug_enabled=True)
        await gateway.complete_json(
            LLMRequest(
                request_id="request-a", phase="one", messages=[], model="fake", max_tokens=10
            )
        )
        await gateway.complete_json(
            LLMRequest(
                request_id="request-a", phase="two", messages=[], model="fake", max_tokens=10
            )
        )
        stats = gateway.stats("request-a")
        records = gateway.call_records("request-a")
        gateway.clear_stats("request-a")
    finally:
        await gateway.close()

    assert stats.request_count == 2
    assert stats.phase_counts == {"one": 1, "two": 1}
    assert [record.phase for record in records] == ["one", "two"]
    assert all(record.model == "fake" for record in records)
    assert all(record.status == "ok" for record in records)
    assert all(record.provider_request_id == "provider-id" for record in records)
    assert records[0].request["messages"] == []
    assert records[0].response == {
        "data": {"ok": True},
        "tool_calls": [],
        "raw_output": {"content": '{"ok":true}'},
    }
    assert gateway.stats("request-a").request_count == 0
    assert gateway.call_records("request-a") == ()


@pytest.mark.asyncio
async def test_gateway_debug_disabled_keeps_only_request_count() -> None:
    provider = ConcurrentProvider()
    gateway = LLMGateway(
        provider=provider,
        max_concurrency=1,
        max_queued_calls=10,
        per_request_max_in_flight=1,
        circuit_breaker=LLMCircuitBreaker(enabled=False),
    )
    gateway.begin_request("request-a", debug_enabled=False)
    try:
        await gateway.complete_json(
            LLMRequest(
                request_id="request-a",
                phase="classification",
                messages=[],
                model="fake",
                max_tokens=10,
            )
        )
        stats = gateway.stats("request-a")
    finally:
        gateway.clear_stats("request-a")
        await gateway.close()

    assert stats.request_count == 1
    assert stats.prompt_tokens == 0
    assert stats.completion_tokens == 0
    assert stats.total_tokens == 0
    assert stats.phase_counts == {}
    assert gateway.call_records("request-a") == ()
    assert provider.requests[0].collect_usage is False


@pytest.mark.asyncio
async def test_cancel_request_removes_queued_calls_and_cancels_provider_operation() -> None:
    provider = CancellableProvider()
    gateway = LLMGateway(
        provider=provider,
        max_concurrency=1,
        max_queued_calls=10,
        per_request_max_in_flight=1,
        circuit_breaker=LLMCircuitBreaker(enabled=False),
    )
    first = asyncio.create_task(
        gateway.complete_json(
            LLMRequest(
                request_id="request-a", phase="one", messages=[], model="fake", max_tokens=10
            )
        )
    )
    second = asyncio.create_task(
        gateway.complete_json(
            LLMRequest(
                request_id="request-a", phase="two", messages=[], model="fake", max_tokens=10
            )
        )
    )
    await provider.started.wait()
    await gateway.cancel_request("request-a")
    results = await asyncio.gather(first, second, return_exceptions=True)
    await gateway.close()

    assert provider.cancelled.is_set()
    assert all(isinstance(result, BaseException) for result in results)


def test_circuit_breaker_ignores_invalid_response_but_opens_on_infrastructure_failures() -> None:
    now = [0.0]
    breaker = LLMCircuitBreaker(
        enabled=True,
        failure_threshold=2,
        recovery_seconds=10,
        clock=lambda: now[0],
    )

    breaker.record_failure("invalid_response")
    assert breaker.state == "closed"
    breaker.record_failure("timeout")
    assert breaker.state == "closed"
    breaker.record_failure("connect")
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        breaker.before_call()

    now[0] = 11.0
    assert breaker.before_call() is True
    assert breaker.state == "half_open"
    breaker.record_success(recovery_probe=True)
    assert breaker.state == "closed"
