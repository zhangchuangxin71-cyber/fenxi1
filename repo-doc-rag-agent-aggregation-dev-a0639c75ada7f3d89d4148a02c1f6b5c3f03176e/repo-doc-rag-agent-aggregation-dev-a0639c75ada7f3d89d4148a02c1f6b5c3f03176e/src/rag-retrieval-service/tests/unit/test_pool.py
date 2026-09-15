from __future__ import annotations

from queue import LifoQueue
from threading import Event, Thread

import pytest

from app.db.pool import PostgresPool


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: object = None) -> None:
        del params
        self.statements.append(sql)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.cursor_value = FakeCursor()

    def cursor(self) -> FakeCursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_transaction_commits_and_reuses_connection() -> None:
    connection = FakeConnection()
    pool = PostgresPool(
        dsn="postgresql://unused",
        min_size=0,
        max_size=1,
        acquire_timeout_seconds=0.1,
        query_timeout_seconds=1,
        connector=lambda **_: connection,
    )

    with pool.transaction() as acquired:
        assert acquired is connection

    assert connection.commits == 1
    with pool.transaction() as acquired_again:
        assert acquired_again is connection
    assert connection.commits == 2


def test_transaction_rolls_back_on_error() -> None:
    connection = FakeConnection()
    pool = PostgresPool(
        dsn="postgresql://unused",
        min_size=0,
        max_size=1,
        acquire_timeout_seconds=0.1,
        query_timeout_seconds=1,
        connector=lambda **_: connection,
    )

    with pytest.raises(RuntimeError):
        with pool.transaction():
            raise RuntimeError("boom")

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_idle_reaper_shrinks_burst_connections_but_preserves_minimum() -> None:
    clock = Clock()
    connections: list[FakeConnection] = []

    def connect(**_: object) -> FakeConnection:
        connection = FakeConnection()
        connections.append(connection)
        return connection

    pool = PostgresPool(
        dsn="postgresql://unused",
        min_size=1,
        max_size=3,
        acquire_timeout_seconds=0.1,
        query_timeout_seconds=1,
        idle_ttl_seconds=10,
        reaper_interval_seconds=1000,
        clock=clock,
        connector=connect,
    )
    pool.open()
    first = pool._acquire()
    second = pool._acquire()
    third = pool._acquire()
    pool._release(first)
    pool._release(second)
    pool._release(third)
    assert pool.created_connections == 3

    clock.value = 11
    reaped = pool.reap_idle()

    assert reaped == 2
    assert pool.created_connections == 1
    assert sum(connection.closed for connection in connections) == 2
    pool.close()


def test_idle_reaper_never_closes_checked_out_connection() -> None:
    clock = Clock()
    connections: list[FakeConnection] = []

    def connect(**_: object) -> FakeConnection:
        connection = FakeConnection()
        connections.append(connection)
        return connection

    pool = PostgresPool(
        dsn="postgresql://unused",
        min_size=1,
        max_size=2,
        acquire_timeout_seconds=0.1,
        query_timeout_seconds=1,
        idle_ttl_seconds=10,
        reaper_interval_seconds=1000,
        clock=clock,
        connector=connect,
    )
    pool.open()
    checked_out = pool._acquire()
    idle = pool._acquire()
    pool._release(idle)
    clock.value = 11

    assert pool.reap_idle() == 1
    assert checked_out.closed is False
    assert pool.created_connections == 1

    pool._release(checked_out)
    pool.close()


def test_connection_returned_while_pool_closes_is_not_leaked() -> None:
    class BlockingQueue:
        def __init__(self) -> None:
            self.inner: LifoQueue[object] = LifoQueue(maxsize=1)
            self.put_started = Event()
            self.allow_put = Event()

        def empty(self) -> bool:
            return self.inner.empty()

        def get(self, *args: object, **kwargs: object) -> object:
            return self.inner.get(*args, **kwargs)

        def get_nowait(self) -> object:
            return self.inner.get_nowait()

        def put(self, value: object) -> None:
            self.put_started.set()
            assert self.allow_put.wait(timeout=1)
            self.inner.put(value)

        def put_nowait(self, value: object) -> None:
            self.put(value)

    connection = FakeConnection()
    pool = PostgresPool(
        dsn="postgresql://unused",
        min_size=0,
        max_size=1,
        acquire_timeout_seconds=0.1,
        query_timeout_seconds=1,
        connector=lambda **_: connection,
    )
    checked_out = pool._acquire()
    blocking_queue = BlockingQueue()
    pool._available = blocking_queue  # type: ignore[assignment]

    release_thread = Thread(target=pool._release, args=(checked_out,))
    release_thread.start()
    assert blocking_queue.put_started.wait(timeout=1)
    close_thread = Thread(target=pool.close)
    close_thread.start()
    blocking_queue.allow_put.set()
    release_thread.join(timeout=1)
    close_thread.join(timeout=1)

    assert not release_thread.is_alive()
    assert not close_thread.is_alive()
    assert connection.closed is True
    assert pool.created_connections == 0
