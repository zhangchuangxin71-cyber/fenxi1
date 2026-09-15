from __future__ import annotations

import sqlite3
from pathlib import Path

from ..metadata_paths import (
    CLIP_PATH_FIELDS,
    normalize_record_paths,
    portable_path_value,
)


class MetadataStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._init_schema()

    def _configure_connection(self):
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                path TEXT,
                duration REAL,
                embedding_path TEXT,
                embedding_metadata_path TEXT,
                embedding_norm REAL,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS segments (
                segment_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                start_time REAL,
                end_time REAL,
                path TEXT,
                embedding_path TEXT,
                embedding_metadata_path TEXT,
                embedding_norm REAL,
                importance_score REAL,
                motion_score REAL,
                visual_diversity_score REAL,
                genericness_score REAL,
                representative_rank INTEGER,
                is_representative INTEGER DEFAULT 0,
                FOREIGN KEY(video_id) REFERENCES videos(video_id)
            );

            CREATE TABLE IF NOT EXISTS frames (
                frame_id TEXT PRIMARY KEY,
                segment_id TEXT NOT NULL,
                timestamp REAL,
                frame_path TEXT,
                embedding_path TEXT,
                embedding_metadata_path TEXT,
                frame_index INTEGER,
                width INTEGER,
                height INTEGER,
                embedding_norm REAL,
                FOREIGN KEY(segment_id) REFERENCES segments(segment_id)
            );

            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                video_id TEXT,
                stage TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                max_retry INTEGER DEFAULT 2,
                retry_count INTEGER DEFAULT 0,
                updated_at TEXT,
                error_message TEXT,
                UNIQUE(video_id, stage)
            );

            CREATE INDEX IF NOT EXISTS idx_segments_video_id
            ON segments(video_id);

            CREATE INDEX IF NOT EXISTS idx_frames_segment_id
            ON frames(segment_id);

            CREATE INDEX IF NOT EXISTS idx_frames_timestamp
            ON frames(timestamp);

            CREATE INDEX IF NOT EXISTS idx_tasks_video_id
            ON tasks(video_id);

            CREATE INDEX IF NOT EXISTS idx_tasks_status
            ON tasks(status);
            """
        )
        self._ensure_column("videos", "status", "TEXT DEFAULT 'pending'")
        self._ensure_column("videos", "error_message", "TEXT")
        self._ensure_column("videos", "updated_at", "TEXT")
        self._ensure_column("videos", "embedding_path", "TEXT")
        self._ensure_column("videos", "embedding_metadata_path", "TEXT")
        self._ensure_column("videos", "embedding_norm", "REAL")
        self._ensure_column("tasks", "max_retry", "INTEGER DEFAULT 2")
        self._ensure_column("tasks", "retry_count", "INTEGER DEFAULT 0")
        self._ensure_column("tasks", "updated_at", "TEXT")
        self._ensure_column("tasks", "error_message", "TEXT")
        self._ensure_column("frames", "embedding_metadata_path", "TEXT")
        self._ensure_column("segments", "embedding_path", "TEXT")
        self._ensure_column("segments", "embedding_metadata_path", "TEXT")
        self._ensure_column("segments", "embedding_norm", "REAL")
        self._ensure_column("segments", "importance_score", "REAL")
        self._ensure_column("segments", "motion_score", "REAL")
        self._ensure_column("segments", "visual_diversity_score", "REAL")
        self._ensure_column("segments", "genericness_score", "REAL")
        self._ensure_column("segments", "representative_rank", "INTEGER")
        self._ensure_column("segments", "is_representative", "INTEGER DEFAULT 0")
        self.conn.commit()

    def _execute(self, sql: str, params: tuple = (), *, commit: bool = False):
        result = self.conn.execute(sql, params)
        if commit:
            self.conn.commit()
        return result

    def _normalize_record_paths(self, record: dict) -> dict:
        return normalize_record_paths(record, CLIP_PATH_FIELDS)

    @staticmethod
    def _portable_path_value(value: str | None) -> str | None:
        return portable_path_value(value)

    def _fetchone_dict(self, sql: str, params: tuple = ()) -> dict | None:
        row = self._execute(sql, params).fetchone()
        return self._normalize_record_paths(dict(row)) if row else None

    def _fetchall_dicts(self, sql: str, params: tuple | list = ()) -> list[dict]:
        return [
            self._normalize_record_paths(dict(row)) for row in self._execute(sql, params).fetchall()
        ]

    @staticmethod
    def _sort_by_order(records: list[dict], key: str, values: list[str]) -> list[dict]:
        order = {value: idx for idx, value in enumerate(values)}
        records.sort(key=lambda item: order[item[key]])
        return records

    def _ensure_column(self, table_name: str, column_name: str, definition: str):
        rows = self._execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row["name"] for row in rows}
        if column_name not in existing:
            self._execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def upsert_video(
        self,
        video_id: str,
        duration: float,
        path: str,
        embedding_path: str | None = None,
        embedding_metadata_path: str | None = None,
        embedding_norm: float | None = None,
        status: str = "pending",
        error_message: str | None = None,
        updated_at: str | None = None,
    ):
        path = self._portable_path_value(path)
        embedding_path = self._portable_path_value(embedding_path)
        embedding_metadata_path = self._portable_path_value(embedding_metadata_path)
        self._execute(
            """
            INSERT INTO videos(
                video_id,
                path,
                duration,
                embedding_path,
                embedding_metadata_path,
                embedding_norm,
                status,
                error_message,
                updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                path=excluded.path,
                duration=excluded.duration,
                embedding_path=COALESCE(excluded.embedding_path, videos.embedding_path),
                embedding_metadata_path=COALESCE(excluded.embedding_metadata_path, videos.embedding_metadata_path),
                embedding_norm=COALESCE(excluded.embedding_norm, videos.embedding_norm),
                status=excluded.status,
                error_message=excluded.error_message,
                updated_at=excluded.updated_at
            """,
            (
                video_id,
                path,
                duration,
                embedding_path,
                embedding_metadata_path,
                embedding_norm,
                status,
                error_message,
                updated_at,
            ),
            commit=True,
        )

    def update_video_embedding(
        self,
        video_id: str,
        embedding_path: str,
        embedding_metadata_path: str,
        embedding_norm: float,
    ):
        embedding_path = self._portable_path_value(embedding_path)
        embedding_metadata_path = self._portable_path_value(embedding_metadata_path)
        self._execute(
            """
            UPDATE videos
            SET embedding_path = ?,
                embedding_metadata_path = ?,
                embedding_norm = ?
            WHERE video_id = ?
            """,
            (embedding_path, embedding_metadata_path, embedding_norm, video_id),
            commit=True,
        )

    def get_video_status(self, video_id: str) -> str | None:
        row = self._execute("SELECT status FROM videos WHERE video_id = ?", (video_id,)).fetchone()
        if row is None:
            return None
        return row["status"]

    def make_task_id(self, video_id: str, stage: str) -> str:
        return f"{video_id}:{stage}"

    def ensure_task(
        self,
        video_id: str,
        stage: str,
        status: str = "pending",
        max_retry: int = 2,
        retry_count: int = 0,
        updated_at: str | None = None,
        error_message: str | None = None,
    ):
        task_id = self.make_task_id(video_id, stage)
        self._execute(
            """
            INSERT INTO tasks(task_id, video_id, stage, status, max_retry, retry_count, updated_at, error_message)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO NOTHING
            """,
            (task_id, video_id, stage, status, max_retry, retry_count, updated_at, error_message),
            commit=True,
        )

    def ensure_video_tasks(
        self,
        video_id: str,
        stages: list[str] | tuple[str, ...],
        max_retries: dict[str, int] | None = None,
        updated_at: str | None = None,
    ):
        for stage in stages:
            self.ensure_task(
                video_id=video_id,
                stage=stage,
                max_retry=(max_retries or {}).get(stage, 2),
                updated_at=updated_at,
            )

    def get_task(self, video_id: str, stage: str) -> dict | None:
        return self._fetchone_dict(
            """
            SELECT task_id, video_id, stage, status, max_retry, retry_count, updated_at, error_message
            FROM tasks
            WHERE video_id = ? AND stage = ?
            """,
            (video_id, stage),
        )

    def get_task_status(self, video_id: str, stage: str) -> str | None:
        row = self.get_task(video_id=video_id, stage=stage)
        if row is None:
            return None
        return row["status"]

    def update_task(
        self,
        video_id: str,
        stage: str,
        status: str,
        updated_at: str | None = None,
        error_message: str | None = None,
        increment_retry: bool = False,
    ):
        self.ensure_task(video_id=video_id, stage=stage)
        if increment_retry:
            self._execute(
                """
                UPDATE tasks
                SET status = ?,
                    max_retry = COALESCE(max_retry, 2),
                    retry_count = retry_count + 1,
                    updated_at = ?,
                    error_message = ?
                WHERE video_id = ? AND stage = ?
                """,
                (status, updated_at, error_message, video_id, stage),
                commit=True,
            )
        else:
            self._execute(
                """
                UPDATE tasks
                SET status = ?,
                    max_retry = COALESCE(max_retry, 2),
                    updated_at = ?,
                    error_message = ?
                WHERE video_id = ? AND stage = ?
                """,
                (status, updated_at, error_message, video_id, stage),
                commit=True,
            )

    def reset_video_tasks(
        self,
        video_id: str,
        stages: list[str] | tuple[str, ...],
        updated_at: str | None = None,
    ):
        for stage in stages:
            self.update_task(
                video_id=video_id,
                stage=stage,
                status="pending",
                updated_at=updated_at,
                error_message=None,
            )

    def mark_video_status(
        self,
        video_id: str,
        status: str,
        error_message: str | None = None,
        updated_at: str | None = None,
    ):
        self._execute(
            """
            UPDATE videos
            SET status = ?,
                error_message = ?,
                updated_at = ?
            WHERE video_id = ?
            """,
            (status, error_message, updated_at, video_id),
            commit=True,
        )

    def upsert_segment(
        self,
        segment_id: str,
        video_id: str,
        start_time: float,
        end_time: float,
        path: str,
        embedding_path: str | None = None,
        embedding_metadata_path: str | None = None,
        embedding_norm: float | None = None,
    ):
        path = self._portable_path_value(path)
        embedding_path = self._portable_path_value(embedding_path)
        embedding_metadata_path = self._portable_path_value(embedding_metadata_path)
        self._execute(
            """
            INSERT INTO segments(segment_id, video_id, start_time, end_time, path, embedding_path, embedding_metadata_path, embedding_norm)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(segment_id) DO UPDATE SET
                video_id=excluded.video_id,
                start_time=excluded.start_time,
                end_time=excluded.end_time,
                path=excluded.path,
                embedding_path=COALESCE(excluded.embedding_path, segments.embedding_path),
                embedding_metadata_path=COALESCE(excluded.embedding_metadata_path, segments.embedding_metadata_path),
                embedding_norm=COALESCE(excluded.embedding_norm, segments.embedding_norm)
            """,
            (
                segment_id,
                video_id,
                start_time,
                end_time,
                path,
                embedding_path,
                embedding_metadata_path,
                embedding_norm,
            ),
            commit=True,
        )

    def update_segment_embedding(
        self,
        segment_id: str,
        embedding_path: str,
        embedding_metadata_path: str,
        embedding_norm: float,
        importance_score: float | None = None,
        motion_score: float | None = None,
        visual_diversity_score: float | None = None,
    ):
        embedding_path = self._portable_path_value(embedding_path)
        embedding_metadata_path = self._portable_path_value(embedding_metadata_path)
        self._execute(
            """
            UPDATE segments
            SET embedding_path = ?,
                embedding_metadata_path = ?,
                embedding_norm = ?,
                importance_score = COALESCE(?, importance_score),
                motion_score = COALESCE(?, motion_score),
                visual_diversity_score = COALESCE(?, visual_diversity_score)
            WHERE segment_id = ?
            """,
            (
                embedding_path,
                embedding_metadata_path,
                embedding_norm,
                importance_score,
                motion_score,
                visual_diversity_score,
                segment_id,
            ),
            commit=True,
        )

    def update_segment_representative_flags(
        self,
        video_id: str,
        selected_segment_ids: list[str],
    ):
        self._execute(
            """
            UPDATE segments
            SET is_representative = 0,
                representative_rank = NULL
            WHERE video_id = ?
            """,
            (video_id,),
        )
        for rank, segment_id in enumerate(selected_segment_ids, start=1):
            self._execute(
                """
                UPDATE segments
                SET is_representative = 1,
                    representative_rank = ?
                WHERE segment_id = ?
                """,
                (rank, segment_id),
            )
        self.conn.commit()

    def upsert_frame(
        self,
        frame_id: str,
        segment_id: str,
        timestamp: float,
        frame_path: str,
        embedding_path: str,
        embedding_metadata_path: str,
        frame_index: int,
        width: int,
        height: int,
        embedding_norm: float,
    ):
        frame_path = self._portable_path_value(frame_path)
        embedding_path = self._portable_path_value(embedding_path)
        embedding_metadata_path = self._portable_path_value(embedding_metadata_path)
        self._execute(
            """
            INSERT OR REPLACE INTO frames(
                frame_id,
                segment_id,
                timestamp,
                frame_path,
                embedding_path,
                embedding_metadata_path,
                frame_index,
                width,
                height,
                embedding_norm
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                frame_id,
                segment_id,
                timestamp,
                frame_path,
                embedding_path,
                embedding_metadata_path,
                frame_index,
                width,
                height,
                embedding_norm,
            ),
            commit=True,
        )

    def update_segment_genericness(
        self,
        segment_id: str,
        genericness_score: float,
    ):
        self._execute(
            """
            UPDATE segments
            SET genericness_score = ?
            WHERE segment_id = ?
            """,
            (genericness_score, segment_id),
            commit=True,
        )

    def clear_video_segments_and_frames(self, video_id: str):
        self._execute(
            """
            DELETE FROM frames
            WHERE segment_id IN (
                SELECT segment_id FROM segments WHERE video_id = ?
            )
            """,
            (video_id,),
        )
        self._execute(
            "DELETE FROM segments WHERE video_id = ?",
            (video_id,),
        )
        self.conn.commit()

    def get_frame_records(self, frame_ids: list[str]) -> list[dict]:
        if not frame_ids:
            return []
        placeholders = ",".join("?" for _ in frame_ids)
        return self._sort_by_order(
            self._fetchall_dicts(
                f"""
                SELECT
                    f.frame_id,
                    s.video_id,
                    f.segment_id,
                    f.timestamp,
                    f.frame_index,
                    f.frame_path,
                    f.embedding_path,
                    f.embedding_metadata_path,
                    f.width,
                    f.height,
                    f.embedding_norm,
                    s.start_time AS segment_start,
                    s.end_time AS segment_end,
                    s.motion_score,
                    v.path AS video_path,
                    v.duration AS video_duration
                FROM frames f
                JOIN segments s ON s.segment_id = f.segment_id
                JOIN videos v ON v.video_id = s.video_id
                WHERE f.frame_id IN ({placeholders})
                """,
                frame_ids,
            ),
            "frame_id",
            frame_ids,
        )

    def get_all_frame_records(self) -> list[dict]:
        return self._fetchall_dicts(
            """
            SELECT
                f.frame_id,
                s.video_id,
                f.segment_id,
                f.timestamp,
                f.frame_index,
                f.frame_path,
                f.embedding_path,
                f.embedding_metadata_path,
                f.width,
                f.height,
                f.embedding_norm,
                s.start_time AS segment_start,
                s.end_time AS segment_end,
                s.motion_score,
                s.genericness_score,
                v.path AS video_path,
                v.duration AS video_duration
            FROM frames f
            JOIN segments s ON s.segment_id = f.segment_id
            JOIN videos v ON v.video_id = s.video_id
            ORDER BY s.video_id, f.timestamp, f.frame_index
            """
        )

    def get_segment_records(self, segment_ids: list[str]) -> list[dict]:
        if not segment_ids:
            return []
        placeholders = ",".join("?" for _ in segment_ids)
        return self._sort_by_order(
            self._fetchall_dicts(
                f"""
                SELECT
                    s.segment_id,
                    s.video_id,
                    s.start_time,
                    s.end_time,
                    s.path,
                    s.embedding_path,
                    s.embedding_metadata_path,
                    s.embedding_norm,
                    s.importance_score,
                    s.motion_score,
                    s.visual_diversity_score,
                    s.genericness_score,
                    s.representative_rank,
                    s.is_representative,
                    v.path AS video_path,
                    v.duration AS video_duration
                FROM segments s
                JOIN videos v ON v.video_id = s.video_id
                WHERE s.segment_id IN ({placeholders})
                """,
                segment_ids,
            ),
            "segment_id",
            segment_ids,
        )

    def get_segment_records_by_video_ids(self, video_ids: list[str]) -> list[dict]:
        if not video_ids:
            return []
        placeholders = ",".join("?" for _ in video_ids)
        records = self._fetchall_dicts(
            f"""
            SELECT
                s.segment_id,
                s.video_id,
                s.start_time,
                s.end_time,
                s.path,
                s.embedding_path,
                s.embedding_metadata_path,
                s.embedding_norm,
                s.importance_score,
                s.motion_score,
                s.visual_diversity_score,
                s.genericness_score,
                s.representative_rank,
                s.is_representative,
                v.path AS video_path,
                v.duration AS video_duration
            FROM segments s
            JOIN videos v ON v.video_id = s.video_id
            WHERE s.video_id IN ({placeholders})
            ORDER BY s.video_id, s.start_time
            """,
            video_ids,
        )
        order = {video_id: idx for idx, video_id in enumerate(video_ids)}
        records.sort(key=lambda item: (order[item["video_id"]], float(item["start_time"] or 0.0)))
        return records

    def get_representative_segment_records_by_video_ids(self, video_ids: list[str]) -> list[dict]:
        if not video_ids:
            return []
        placeholders = ",".join("?" for _ in video_ids)
        records = self._fetchall_dicts(
            f"""
            SELECT
                s.segment_id,
                s.video_id,
                s.start_time,
                s.end_time,
                s.path,
                s.embedding_path,
                s.embedding_metadata_path,
                s.embedding_norm,
                s.importance_score,
                s.motion_score,
                s.visual_diversity_score,
                s.genericness_score,
                s.representative_rank,
                s.is_representative,
                v.path AS video_path,
                v.duration AS video_duration
            FROM segments s
            JOIN videos v ON v.video_id = s.video_id
            WHERE s.video_id IN ({placeholders})
              AND COALESCE(s.is_representative, 0) = 1
            ORDER BY s.video_id, s.representative_rank, s.start_time
            """,
            video_ids,
        )
        order = {video_id: idx for idx, video_id in enumerate(video_ids)}
        records.sort(
            key=lambda item: (
                order[item["video_id"]],
                int(item.get("representative_rank") or 999999),
            )
        )
        return records

    def get_frame_records_by_segment_ids(self, segment_ids: list[str]) -> list[dict]:
        if not segment_ids:
            return []
        placeholders = ",".join("?" for _ in segment_ids)
        return self._fetchall_dicts(
            f"""
            SELECT
                f.frame_id,
                s.video_id,
                f.segment_id,
                f.timestamp,
                f.frame_index,
                f.frame_path,
                f.embedding_path,
                f.embedding_metadata_path,
                f.width,
                f.height,
                f.embedding_norm,
                s.start_time AS segment_start,
                s.end_time AS segment_end,
                s.motion_score,
                v.path AS video_path,
                v.duration AS video_duration
            FROM frames f
            JOIN segments s ON s.segment_id = f.segment_id
            JOIN videos v ON v.video_id = s.video_id
            WHERE f.segment_id IN ({placeholders})
            ORDER BY f.timestamp, f.frame_index
            """,
            segment_ids,
        )

    def get_video_records(self, video_ids: list[str]) -> list[dict]:
        if not video_ids:
            return []
        placeholders = ",".join("?" for _ in video_ids)
        return self._sort_by_order(
            self._fetchall_dicts(
                f"""
            SELECT
                video_id,
                path,
                duration,
                embedding_path,
                embedding_metadata_path,
                embedding_norm,
                status,
                error_message,
                updated_at
            FROM videos
            WHERE video_id IN ({placeholders})
                """,
                video_ids,
            ),
            "video_id",
            video_ids,
        )

    def close(self):
        self.conn.close()
