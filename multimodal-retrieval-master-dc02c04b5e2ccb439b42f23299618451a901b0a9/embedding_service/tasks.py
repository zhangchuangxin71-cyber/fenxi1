from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class TaskRecord:
    job_id: str
    status: str = "queued"
    job_kind: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: dict[str, Any] | None = None
    result_size_bytes: int | None = None
    error: str | None = None
    error_type: str | None = None
    error_details: dict[str, Any] | None = None
    error_traceback: str | None = None
    progress: dict[str, Any] | None = None


class TaskManager:
    """Thread-safe task manager with SQLite persistence and automatic cleanup."""

    def __init__(self, db_path: str | Path = "runtime/tasks.db", max_tasks: int = 10000, ttl_hours: int = 24):
        self.db_path = Path(db_path)
        self.max_tasks = max_tasks
        self.ttl_seconds = ttl_hours * 3600
        self._lock = threading.Lock()
        self._local = threading.local()
        self._init_db()
        self._start_cleanup_thread()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self) -> None:
        """Initialize database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    result TEXT,
                    error TEXT,
                    error_type TEXT,
                    error_traceback TEXT,
                    progress TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON tasks(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON tasks(created_at)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "job_kind" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN job_kind TEXT")
            if "result_size_bytes" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN result_size_bytes INTEGER")
            if "error_details" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN error_details TEXT")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TaskRecord:
        keys = set(row.keys())
        result_size = row["result_size_bytes"] if "result_size_bytes" in keys else None
        return TaskRecord(
            job_id=row["job_id"],
            status=row["status"],
            job_kind=row["job_kind"] if "job_kind" in keys else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result=json.loads(row["result"]) if row["result"] else None,
            result_size_bytes=int(result_size) if result_size is not None else None,
            error=row["error"],
            error_type=row["error_type"],
            error_details=(
                json.loads(row["error_details"])
                if "error_details" in keys and row["error_details"]
                else None
            ),
            error_traceback=row["error_traceback"],
            progress=json.loads(row["progress"]) if row["progress"] else None,
        )

    def _start_cleanup_thread(self) -> None:
        """Start background thread for periodic cleanup."""
        def cleanup_loop():
            while True:
                try:
                    time.sleep(3600)  # Run every hour
                    self.cleanup_expired()
                except Exception as exc:
                    logger.error(f"Cleanup thread error: {exc}")

        thread = threading.Thread(target=cleanup_loop, daemon=True, name="TaskCleanup")
        thread.start()

    def create(self, job_kind: str) -> TaskRecord:
        """Create a new task record."""
        with self._lock:
            # Check task limit
            conn = self._get_conn()
            count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            if count >= self.max_tasks:
                raise RuntimeError(f"Task limit reached: {self.max_tasks}")

            job_id = uuid4().hex
            record = TaskRecord(job_id=job_id, job_kind=job_kind)
            conn.execute(
                """
                INSERT INTO tasks (
                    job_id, status, job_kind, created_at, updated_at,
                    result, error, error_type, error_traceback, progress
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.job_id,
                    record.status,
                    record.job_kind,
                    record.created_at,
                    record.updated_at,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            )
            conn.commit()
            return record

    def get(self, job_id: str) -> TaskRecord | None:
        """Get task record by job_id."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM tasks WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return self._row_to_record(row)

    def update(self, job_id: str, **kwargs) -> TaskRecord:
        """Update task record."""
        with self._lock:
            conn = self._get_conn()
            record = self.get(job_id)
            if not record:
                raise ValueError(f"Task not found: {job_id}")

            for key, value in kwargs.items():
                setattr(record, key, value)
            record.updated_at = time.time()

            result_json = json.dumps(record.result) if record.result else None
            if record.result is not None and record.result_size_bytes is None:
                record.result_size_bytes = len(result_json.encode("utf-8")) if result_json else 0

            conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, result = ?, result_size_bytes = ?,
                    error = ?, error_type = ?, error_details = ?, error_traceback = ?, progress = ?
                WHERE job_id = ?
                """,
                (
                    record.status,
                    record.updated_at,
                    result_json,
                    record.result_size_bytes,
                    record.error,
                    record.error_type,
                    json.dumps(record.error_details) if record.error_details else None,
                    record.error_traceback,
                    json.dumps(record.progress) if record.progress else None,
                    job_id,
                ),
            )
            conn.commit()
            return record

    def update_error(
        self,
        job_id: str,
        exc: Exception,
        *,
        error: str | None = None,
        error_type: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> TaskRecord:
        """Update task with exception details."""
        return self.update(
            job_id,
            status="failed",
            error=error if error is not None else str(exc),
            error_type=error_type if error_type is not None else type(exc).__name__,
            error_details=error_details,
            error_traceback=traceback.format_exc(),
        )

    def reset_interrupted_jobs(self) -> int:
        """Mark in-flight jobs as failed after a service restart."""
        with self._lock:
            conn = self._get_conn()
            now = time.time()
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'failed',
                    error = '服务重启导致任务中断，请重新提交',
                    error_type = 'INTERRUPTED',
                    error_details = '{"reason":"INTERRUPTED"}',
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            conn.commit()
            reset = cursor.rowcount
            if reset > 0:
                logger.info("Marked %s interrupted running jobs as failed", reset)
            return reset

    def cleanup_expired(self) -> int:
        """
        Remove expired tasks. Returns number of deleted tasks.
        
        Only removes tasks that are completed (succeeded/failed) and older than TTL.
        Running tasks are never removed regardless of age.
        """
        with self._lock:
            conn = self._get_conn()
            cutoff = time.time() - self.ttl_seconds
            # Only delete completed tasks (succeeded or failed) that are older than TTL
            # Use updated_at to ensure we don't delete long-running tasks
            cursor = conn.execute(
                "DELETE FROM tasks WHERE status IN ('succeeded', 'failed') AND updated_at < ?",
                (cutoff,)
            )
            conn.commit()
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} expired tasks")
            return deleted

    def get_stats(self) -> dict[str, Any]:
        """Get task statistics."""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        by_status = {}
        for row in conn.execute("SELECT status, COUNT(*) as count FROM tasks GROUP BY status"):
            by_status[row["status"]] = row["count"]
        return {"total": total, "by_status": by_status}

    def close(self) -> None:
        """Close database connections."""
        if hasattr(self._local, "conn"):
            self._local.conn.close()


task_manager = TaskManager()
