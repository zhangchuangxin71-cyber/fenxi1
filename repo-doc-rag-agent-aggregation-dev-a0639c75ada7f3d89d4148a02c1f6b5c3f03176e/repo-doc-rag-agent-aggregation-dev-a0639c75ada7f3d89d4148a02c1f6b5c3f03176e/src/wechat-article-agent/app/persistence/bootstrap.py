from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict

from app.persistence.migrate import apply_migrations


@dataclass(frozen=True, slots=True)
class ApplicationDatabase:
    name: str
    user: str
    password: str


def application_database(database_url: str) -> ApplicationDatabase:
    values = conninfo_to_dict(database_url)
    name = str(values.get("dbname") or "").strip()
    user = str(values.get("user") or "").strip()
    password = str(values.get("password") or "")
    if not name or not user or not password:
        raise ValueError("DATABASE_URL must include a database name, user and password")
    return ApplicationDatabase(name=name, user=user, password=password)


async def create_database(*, admin_url: str, database_url: str) -> None:
    target = application_database(database_url)
    admin_user = str(conninfo_to_dict(admin_url).get("user") or "").strip()
    if not admin_user:
        raise ValueError("DATABASE_ADMIN_URL must include an administrator user")
    if admin_user == target.user:
        raise ValueError("DATABASE_ADMIN_URL and DATABASE_URL must use different database roles")
    async with await AsyncConnection.connect(admin_url, autocommit=True) as connection:
        role_cursor = await connection.execute(
            "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = %s",
            (target.user,),
        )
        role = await role_cursor.fetchone()
        if role is None:
            await connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(target.user),
                    sql.Literal(target.password),
                )
            )
        else:
            can_login, is_superuser = role
            if not can_login or is_superuser:
                raise RuntimeError(
                    f"existing application role {target.user!r} must be a non-superuser LOGIN role"
                )
            await connection.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(target.user),
                    sql.Literal(target.password),
                )
            )

        database_cursor = await connection.execute(
            """
            SELECT owner.rolname
            FROM pg_database AS database
            JOIN pg_roles AS owner ON owner.oid = database.datdba
            WHERE database.datname = %s
            """,
            (target.name,),
        )
        database = await database_cursor.fetchone()
        if database is None:
            await connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(target.name),
                    sql.Identifier(target.user),
                )
            )
        elif database[0] != target.user:
            raise RuntimeError(
                f"existing database {target.name!r} is owned by {database[0]!r}, expected {target.user!r}"
            )


async def bootstrap(*, admin_url: str, database_url: str) -> None:
    await create_database(admin_url=admin_url, database_url=database_url)
    await apply_migrations(database_url)
    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        await saver.setup()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the WeChat article database and initialize all application tables."
    )
    parser.add_argument("--admin-url", default=os.getenv("DATABASE_ADMIN_URL"))
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    if not args.admin_url:
        parser.error("DATABASE_ADMIN_URL or --admin-url is required")
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")
    asyncio.run(bootstrap(admin_url=args.admin_url, database_url=args.database_url))


if __name__ == "__main__":
    main()
