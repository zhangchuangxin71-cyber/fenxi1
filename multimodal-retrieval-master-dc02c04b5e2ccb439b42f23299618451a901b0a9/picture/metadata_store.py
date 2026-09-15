from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class PictureMetadataStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS images (
                image_id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                embedding_path TEXT,
                embedding_norm REAL,
                modality TEXT DEFAULT 'image',
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
            """
        )
        self.conn.commit()

    def _run(self, sql: str, params=(), *, commit: bool = False):
        with self._lock:
            cur = self.conn.execute(sql, params)
            if commit:
                self.conn.commit()
            return cur

    def _fetchone(self, sql: str, params=()):
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params=()):
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def upsert_image(
        self,
        *,
        image_id: str,
        path: str,
        width=None,
        height=None,
        embedding_path=None,
        embedding_norm=None,
        status="done",
        error_message=None,
        updated_at=None,
    ) -> None:
        from datetime import datetime, timezone

        ts = updated_at or datetime.now(timezone.utc).isoformat()
        self._run(
            """
            INSERT INTO images (image_id, path, width, height, embedding_path, embedding_norm,
                modality, status, error_message, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'image', ?, ?, ?)
            ON CONFLICT(image_id) DO UPDATE SET
                path=excluded.path, width=excluded.width, height=excluded.height,
                embedding_path=excluded.embedding_path, embedding_norm=excluded.embedding_norm,
                status=excluded.status, error_message=excluded.error_message, updated_at=excluded.updated_at
            """,
            (
                image_id,
                path,
                width,
                height,
                embedding_path,
                embedding_norm,
                status,
                error_message,
                ts,
            ),
            commit=True,
        )

    def mark_failed(self, image_id: str, path: str, error_message: str) -> None:
        self.upsert_image(
            image_id=image_id, path=path, status="failed", error_message=error_message
        )

    def get_status(self, image_id: str) -> str | None:
        row = self._fetchone("SELECT status FROM images WHERE image_id = ?", (image_id,))
        return row["status"] if row else None

    def get_image(self, image_id: str) -> dict | None:
        row = self._fetchone("SELECT * FROM images WHERE image_id = ?", (image_id,))
        return dict(row) if row else None

    def list_images(self, *, status: str | None = None) -> list[dict]:
        if status:
            rows = self._fetchall(
                "SELECT * FROM images WHERE status = ? ORDER BY image_id", (status,)
            )
        else:
            rows = self._fetchall("SELECT * FROM images ORDER BY image_id")
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.conn.close()
