from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from queue import Empty, Full, LifoQueue
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from typing import Any

import psycopg
from psycopg import Connection

from app.core.errors import ApiError


@dataclass(frozen=True, slots=True)
class _IdleConnection:
    connection: Connection[Any]
    released_at: float


class PostgresPool:
    """Small single-process psycopg pool with explicit transaction boundaries."""

    def __init__(
        self,
        *,
        dsn: str,
        min_size: int,
        max_size: int,
        acquire_timeout_seconds: float,
        query_timeout_seconds: float,
        connect_timeout_seconds: float = 5.0,
        idle_ttl_seconds: float = 120.0,
        reaper_interval_seconds: float = 30.0,
        clock: Callable[[], float] = monotonic,
        connector: Callable[..., Connection[Any]] = psycopg.connect,
    ) -> None:
        self._dsn = dsn
        self._min_size = max(0, int(min_size))
        self._max_size = max(1, int(max_size), self._min_size)
        self._acquire_timeout = max(0.01, float(acquire_timeout_seconds))
        self._query_timeout_ms = max(1, int(float(query_timeout_seconds) * 1000))
        self._connect_timeout = max(1, int(connect_timeout_seconds))
        self._idle_ttl = max(0.01, float(idle_ttl_seconds))
        self._reaper_interval = max(0.01, float(reaper_interval_seconds))
        self._clock = clock
        self._connector = connector
        self._available: LifoQueue[_IdleConnection] = LifoQueue(maxsize=self._max_size)
        self._lock = Lock()
        self._created = 0
        self._closed = False
        self._reaper_stop = Event()
        self._reaper_thread: Thread | None = None

    @property
    def created_connections(self) -> int:
        with self._lock:
            return self._created

    def open(self) -> None:
        with self._lock:
            self._closed = False
            if self._reaper_stop.is_set():
                self._reaper_stop = Event()
        while self.created_connections < self._min_size:
            connection = self._create_connection()
            with self._lock:
                self._created += 1
            self._available.put(_IdleConnection(connection, self._clock()))
        with self._lock:
            if self._reaper_thread is None or not self._reaper_thread.is_alive():
                self._reaper_thread = Thread(
                    target=self._reaper_loop,
                    name="rag-db-pool-reaper",
                    daemon=True,
                )
                self._reaper_thread.start()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._reaper_stop.set()
            reaper = self._reaper_thread
            self._reaper_thread = None
        if reaper is not None and reaper is not current_thread():
            reaper.join(timeout=max(1.0, self._reaper_interval + 1.0))
        while True:
            try:
                entry = self._available.get_nowait()
            except Empty:
                break
            try:
                entry.connection.close()
            finally:
                with self._lock:
                    self._created = max(0, self._created - 1)

    def reap_idle(self) -> int:
        now = self._clock()
        reaped = 0
        retained: list[_IdleConnection] = []
        with self._lock:
            while True:
                try:
                    entry = self._available.get_nowait()
                except Empty:
                    break
                expired = now - entry.released_at >= self._idle_ttl
                if expired and self._created > self._min_size:
                    try:
                        entry.connection.close()
                    finally:
                        self._created = max(0, self._created - 1)
                        reaped += 1
                else:
                    retained.append(entry)
            for entry in retained:
                self._available.put(entry)
        return reaped

    def _reaper_loop(self) -> None:
        while not self._reaper_stop.wait(self._reaper_interval):
            self.reap_idle()

    @contextmanager
    def transaction(self, *, read_only: bool = False) -> Iterator[Connection[Any]]:
        connection = self._acquire()
        try:
            if read_only:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            yield connection
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            finally:
                self._release(connection)
            raise
        else:
            self._release(connection)

    def ping(self) -> None:
        with self.transaction(read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

    def _create_connection(self) -> Connection[Any]:
        connection = self._connector(
            conninfo=self._dsn,
            connect_timeout=self._connect_timeout,
            autocommit=False,
            options=f"-c statement_timeout={self._query_timeout_ms}",
        )
        return connection

    def _acquire(self) -> Connection[Any]:
        with self._lock:
            if self._closed:
                raise ApiError(503, "DB_POOL_CLOSED", "database pool is closed", retryable=True)
            create = self._available.empty() and self._created < self._max_size
            if create:
                self._created += 1
        if create:
            try:
                return self._create_connection()
            except Exception:
                with self._lock:
                    self._created = max(0, self._created - 1)
                raise
        try:
            entry = self._available.get(timeout=self._acquire_timeout)
        except Empty as exc:
            raise ApiError(
                503,
                "DB_POOL_EXHAUSTED",
                "database connection pool is exhausted",
                retryable=True,
            ) from exc
        connection = entry.connection
        if getattr(connection, "closed", False):
            with self._lock:
                self._created = max(0, self._created - 1)
            return self._acquire()
        return connection

    def _release(self, connection: Connection[Any]) -> None:
        if getattr(connection, "closed", False):
            with self._lock:
                self._created = max(0, self._created - 1)
            return
        with self._lock:
            if self._closed:
                connection.close()
                self._created = max(0, self._created - 1)
                return
            try:
                self._available.put_nowait(_IdleConnection(connection, self._clock()))
            except Full:
                connection.close()
                self._created = max(0, self._created - 1)
