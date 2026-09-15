from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import AppSettings


def create_pool(settings: AppSettings) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        timeout=settings.db_pool_acquire_timeout_seconds,
        open=False,
        kwargs={
            "autocommit": False,
            "connect_timeout": int(settings.db_connect_timeout_seconds),
            "row_factory": dict_row,
            "options": f"-c statement_timeout={int(settings.db_query_timeout_seconds * 1000)}",
        },
    )
