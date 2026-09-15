from __future__ import annotations

import asyncio
from pathlib import Path

from psycopg import AsyncConnection

MIGRATIONS = Path(__file__).with_name("migrations")


async def apply_migrations(database_url: str) -> None:
    migrations = await asyncio.to_thread(lambda: sorted(MIGRATIONS.glob("*.sql")))
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        for path in migrations:
            statement = await asyncio.to_thread(path.read_text, encoding="utf-8")
            await connection.execute(statement)
