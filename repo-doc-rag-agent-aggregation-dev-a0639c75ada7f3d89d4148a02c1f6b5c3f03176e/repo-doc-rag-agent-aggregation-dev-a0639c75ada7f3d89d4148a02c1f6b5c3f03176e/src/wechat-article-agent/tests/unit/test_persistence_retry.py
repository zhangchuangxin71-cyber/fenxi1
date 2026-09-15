from __future__ import annotations

import pytest
from psycopg import OperationalError

from app.core.errors import AppError
from app.persistence.retry import retry_database_operation


@pytest.mark.asyncio
async def test_database_operation_retries_transient_connection_error() -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OperationalError("connection dropped")
        return "ok"

    assert await retry_database_operation(operation, max_retries=2, base_seconds=0.0001) == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_database_operation_exhaustion_returns_stable_error() -> None:
    async def operation() -> None:
        raise OperationalError("connection dropped")

    with pytest.raises(AppError) as raised:
        await retry_database_operation(operation, max_retries=1, base_seconds=0.0001)
    assert raised.value.code == "DATABASE_UNAVAILABLE"
    assert raised.value.retryable is True
