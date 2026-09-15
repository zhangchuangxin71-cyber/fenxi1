import asyncio
import contextvars

import pytest

from app.core.errors import AppError
from app.core.resilience import CircuitBreaker
from app.llm.gateway import FairScheduler, _counts_toward_ark_circuit


@pytest.mark.asyncio
async def test_queue_timeout_does_not_limit_running_operation() -> None:
    scheduler = FairScheduler(concurrency=1, queue_size=4, per_run=1, wait_timeout=0.02)

    async def operation() -> str:
        await asyncio.sleep(0.05)
        return "done"

    try:
        assert await scheduler.submit("run-1", operation) == "done"
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_queue_timeout_withdraws_call_that_never_starts() -> None:
    scheduler = FairScheduler(concurrency=1, queue_size=4, per_run=1, wait_timeout=0.02)
    release = asyncio.Event()

    async def blocked() -> str:
        await release.wait()
        return "first"

    first = asyncio.create_task(scheduler.submit("run-1", blocked))
    await asyncio.sleep(0.005)
    with pytest.raises(Exception) as raised:
        await scheduler.submit("run-1", lambda: asyncio.sleep(0, result="second"))
    assert getattr(raised.value, "code", None) == "ARK_QUEUE_TIMEOUT"
    release.set()
    assert await first == "first"
    await scheduler.close()


@pytest.mark.asyncio
async def test_scheduler_preserves_submitter_context_for_each_operation() -> None:
    scheduler = FairScheduler(concurrency=1, queue_size=4, per_run=1, wait_timeout=0.1)
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="worker")

    async def submit(value: str) -> str:
        token = marker.set(value)
        try:
            return await scheduler.submit(value, lambda: asyncio.sleep(0, result=marker.get()))
        finally:
            marker.reset(token)

    try:
        results = await asyncio.gather(submit("first"), submit("second"))
        assert results[0] == "first"
        assert results[1] == "second"
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_circuit_breaker_allows_one_recovery_probe() -> None:
    breaker = CircuitBreaker(
        enabled=True,
        threshold=2,
        recovery_seconds=0.01,
        error_code="TEST_CIRCUIT_OPEN",
        provider_name="Test provider",
    )
    assert await breaker.before_call() is False
    await breaker.failure(counts=True, recovery_probe=False)
    await breaker.failure(counts=True, recovery_probe=False)
    with pytest.raises(AppError) as raised:
        await breaker.before_call()
    assert raised.value.code == "TEST_CIRCUIT_OPEN"
    await asyncio.sleep(0.015)
    assert await breaker.before_call() is True
    with pytest.raises(AppError):
        await breaker.before_call()
    await breaker.success(recovery_probe=True)
    assert await breaker.before_call() is False


@pytest.mark.asyncio
async def test_non_counted_failure_does_not_open_circuit() -> None:
    breaker = CircuitBreaker(
        enabled=True,
        threshold=1,
        recovery_seconds=1,
        error_code="TEST_CIRCUIT_OPEN",
        provider_name="Test provider",
    )
    await breaker.failure(counts=False, recovery_probe=False)
    assert await breaker.before_call() is False


def test_empty_structured_output_counts_toward_ark_circuit() -> None:
    assert _counts_toward_ark_circuit(
        AppError(
            502,
            "ARK_STRUCTURED_OUTPUT_INVALID",
            "empty",
            retryable=True,
            details={"empty_output": True},
        )
    )
    assert not _counts_toward_ark_circuit(
        AppError(
            502,
            "ARK_STRUCTURED_OUTPUT_INVALID",
            "invalid JSON",
            details={"empty_output": False},
        )
    )
