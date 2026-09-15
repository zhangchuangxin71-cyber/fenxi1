from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar

T = TypeVar("T")


class DatabaseExecutor:
    """Shared bounded executor for blocking psycopg repository calls."""

    def __init__(self, *, max_workers: int) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)), thread_name_prefix="rag-db"
        )

    async def run(self, function: Callable[..., T], /, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(function, **kwargs))

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
