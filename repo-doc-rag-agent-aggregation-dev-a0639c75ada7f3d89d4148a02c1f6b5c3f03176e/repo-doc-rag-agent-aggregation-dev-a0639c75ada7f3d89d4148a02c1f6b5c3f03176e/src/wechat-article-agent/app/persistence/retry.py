from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import cast

from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolTimeout

from app.core.errors import AppError

TRANSIENT_DATABASE_ERRORS = (OperationalError, InterfaceError, PoolTimeout)


async def retry_database_operation[ResultT](
    operation: Callable[[], Awaitable[ResultT]],
    *,
    max_retries: int,
    base_seconds: float = 0.1,
) -> ResultT:
    """Retry a complete, independently-owned DB operation after connection failures."""

    for attempt in range(max_retries + 1):
        try:
            return await operation()
        except TRANSIENT_DATABASE_ERRORS as exc:
            if attempt >= max_retries:
                raise AppError(
                    503,
                    "DATABASE_UNAVAILABLE",
                    "The article database is temporarily unavailable.",
                    True,
                ) from exc
            await asyncio.sleep(retry_delay(base_seconds, attempt))
    raise AssertionError("database retry loop exhausted unexpectedly")


def retry_delay(base_seconds: float, attempt: int) -> float:
    return cast(float, base_seconds * (2**attempt) * random.uniform(0.75, 1.25))
