from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.core.rate_limit import AsyncTokenBucket


@pytest.mark.asyncio
async def test_token_bucket_rejects_burst_above_abnormal_traffic_limit() -> None:
    bucket = AsyncTokenBucket(
        per_minute=60,
        burst=2,
        error_code="LIMITED",
        message="limited",
    )
    await bucket.take()
    await bucket.take()
    with pytest.raises(AppError) as raised:
        await bucket.take()
    assert raised.value.code == "LIMITED"
    assert raised.value.details["retry_after"] >= 1
