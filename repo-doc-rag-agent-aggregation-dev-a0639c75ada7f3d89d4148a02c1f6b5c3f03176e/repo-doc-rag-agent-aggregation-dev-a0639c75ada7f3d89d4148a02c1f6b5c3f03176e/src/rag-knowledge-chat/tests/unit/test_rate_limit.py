import pytest

from app.platform.errors import ApiError
from app.platform.rate_limit import InMemoryRateLimiter


@pytest.mark.asyncio
async def test_limiter_rejects_calls_over_window_limit() -> None:
    now = 100.0
    limiter = InMemoryRateLimiter(limit=2, period_seconds=60, clock=lambda: now)

    await limiter.acquire("user")
    await limiter.acquire("user")
    with pytest.raises(ApiError) as exc_info:
        await limiter.acquire("user")

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_limiter_allows_calls_after_window_expires() -> None:
    current = [100.0]
    limiter = InMemoryRateLimiter(limit=1, period_seconds=60, clock=lambda: current[0])
    await limiter.acquire("user")

    current[0] = 161.0
    await limiter.acquire("user")
