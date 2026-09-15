from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import numpy as np

from ..metadata_paths import (
    HYBRID_PATH_FIELDS,
    normalize_record_paths,
    portable_path_value,
)


class MetadataStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _execute(self, sql: str, params: tuple = (), *, commit: bool = False):
        with self._lock:
            cursor = self.conn.execute(sql, params)
            if commit:
                self.conn.commit()
            return cursor

    def _normalize_record_paths(self, record: dict) -> dict:
        return normalize_record_paths(record, HYBRID_PATH_FIELDS)

    @staticmethod
    def _portable_path_value(value: str | None) -> str | None:
        return portable_path_value(value)

    def _init_schema(self):
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                path TEXT,
                duration REAL,
                caption_tags TEXT,
                caption_description TEXT,
                caption_payload TEXT,
                caption_model TEXT,
                caption_updated_at TEXT
            )
            """,
            commit=True,
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS search_documents (
                video_id TEXT PRIMARY KEY,
                path TEXT,
                duration REAL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                description TEXT NOT NULL DEFAULT '',
                caption_text TEXT NOT NULL DEFAULT '',
                sparse_tags TEXT NOT NULL DEFAULT '',
                sparse_description TEXT NOT NULL DEFAULT '',
                sparse_caption TEXT NOT NULL DEFAULT '',
                search_payload TEXT,
                caption_model TEXT,
                caption_updated_at TEXT,
                embedding_model TEXT,
                embedding_dim INTEGER,
                embedding_blob BLOB,
                embedding_updated_at TEXT
            )
            """,
            commit=True,
        )
        self._execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                video_id UNINDEXED,
                tags_terms,
                description_terms,
                caption_terms,
                tokenize='unicode61 remove_diacritics 0'
            )
            """,
            commit=True,
        )

    def upsert_video_caption_metadata(
        self,
        video_id: str,
        *,
        path: str | None = None,
        duration: float | None = None,
        tags_json: str,
        description: str,
        payload_json: str,
        caption_model: str | None = None,
        caption_updated_at: str | None = None,
    ):
        path = self._portable_path_value(path)
        self._execute(
            """
            INSERT INTO videos(
                video_id,
                path,
                duration,
                caption_tags,
                caption_description,
                caption_payload,
                caption_model,
                caption_updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                path=COALESCE(excluded.path, videos.path),
                duration=COALESCE(excluded.duration, videos.duration),
                caption_tags=excluded.caption_tags,
                caption_description=excluded.caption_description,
                caption_payload=excluded.caption_payload,
                caption_model=COALESCE(excluded.caption_model, videos.caption_model),
                caption_updated_at=COALESCE(excluded.caption_updated_at, videos.caption_updated_at)
            """,
            (
                video_id,
                path,
                duration,
                tags_json,
                description,
                payload_json,
                caption_model,
                caption_updated_at,
            ),
            commit=True,
        )

    def get_video_records(self, video_ids: list[str]) -> list[dict]:
        if not video_ids:
            return []
        placeholders = ", ".join("?" for _ in video_ids)
        rows = self._execute(
            f"""
            SELECT
                video_id,
                path,
                duration,
                caption_tags,
                caption_description,
                caption_payload,
                caption_model,
                caption_updated_at
            FROM videos
            WHERE video_id IN ({placeholders})
            """,
            tuple(video_ids),
        ).fetchall()
        return [self._normalize_record_paths(dict(row)) for row in rows]

    def list_video_records(self) -> list[dict]:
        rows = self._execute(
            """
            SELECT
                video_id,
                path,
                duration,
                caption_tags,
                caption_description,
                caption_payload,
                caption_model,
                caption_updated_at
            FROM videos
            ORDER BY video_id
            """
        ).fetchall()
        return [self._normalize_record_paths(dict(row)) for row in rows]

    def upsert_search_document(
        self,
        video_id: str,
        *,
        path: str | None = None,
        duration: float | None = None,
        tags_json: str,
        description: str,
        caption_text: str,
        sparse_tags: str,
        sparse_description: str,
        sparse_caption: str,
        search_payload: str | None = None,
        caption_model: str | None = None,
        caption_updated_at: str | None = None,
        embedding_model: str | None = None,
        embedding_vector: np.ndarray | None = None,
        embedding_updated_at: str | None = None,
    ) -> None:
        path = self._portable_path_value(path)
        embedding_dim = None
        embedding_blob = None
        if embedding_vector is not None:
            vector = np.asarray(embedding_vector, dtype=np.float32)
            embedding_dim = int(vector.shape[0])
            embedding_blob = sqlite3.Binary(vector.tobytes())

        self._execute(
            """
            INSERT INTO search_documents(
                video_id,
                path,
                duration,
                tags_json,
                description,
                caption_text,
                sparse_tags,
                sparse_description,
                sparse_caption,
                search_payload,
                caption_model,
                caption_updated_at,
                embedding_model,
                embedding_dim,
                embedding_blob,
                embedding_updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                path=COALESCE(excluded.path, search_documents.path),
                duration=COALESCE(excluded.duration, search_documents.duration),
                tags_json=excluded.tags_json,
                description=excluded.description,
                caption_text=excluded.caption_text,
                sparse_tags=excluded.sparse_tags,
                sparse_description=excluded.sparse_description,
                sparse_caption=excluded.sparse_caption,
                search_payload=COALESCE(excluded.search_payload, search_documents.search_payload),
                caption_model=COALESCE(excluded.caption_model, search_documents.caption_model),
                caption_updated_at=COALESCE(excluded.caption_updated_at, search_documents.caption_updated_at),
                embedding_model=COALESCE(excluded.embedding_model, search_documents.embedding_model),
                embedding_dim=COALESCE(excluded.embedding_dim, search_documents.embedding_dim),
                embedding_blob=COALESCE(excluded.embedding_blob, search_documents.embedding_blob),
                embedding_updated_at=COALESCE(excluded.embedding_updated_at, search_documents.embedding_updated_at)
            """,
            (
                video_id,
                path,
                duration,
                tags_json,
                description,
                caption_text,
                sparse_tags,
                sparse_description,
                sparse_caption,
                search_payload,
                caption_model,
                caption_updated_at,
                embedding_model,
                embedding_dim,
                embedding_blob,
                embedding_updated_at,
            ),
            commit=True,
        )
        self._refresh_search_fts_row(
            video_id=video_id,
            sparse_tags=sparse_tags,
            sparse_description=sparse_description,
            sparse_caption=sparse_caption,
        )

    def _refresh_search_fts_row(
        self,
        *,
        video_id: str,
        sparse_tags: str,
        sparse_description: str,
        sparse_caption: str,
    ) -> None:
        self._execute("DELETE FROM search_fts WHERE video_id = ?", (video_id,), commit=False)
        self._execute(
            """
            INSERT INTO search_fts(video_id, tags_terms, description_terms, caption_terms)
            VALUES(?, ?, ?, ?)
            """,
            (video_id, sparse_tags, sparse_description, sparse_caption),
            commit=True,
        )

    def get_search_document_embedding_state(self, video_id: str) -> dict | None:
        row = self._execute(
            """
            SELECT embedding_blob, caption_updated_at, embedding_updated_at
            FROM search_documents
            WHERE video_id = ?
            """,
            (video_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_search_documents(self, *, require_embeddings: bool = False) -> list[dict]:
        where_clause = "WHERE embedding_blob IS NOT NULL" if require_embeddings else ""
        rows = self._execute(
            f"""
            SELECT
                video_id,
                path,
                duration,
                tags_json,
                description,
                caption_text,
                sparse_tags,
                sparse_description,
                sparse_caption,
                search_payload,
                caption_model,
                caption_updated_at,
                embedding_model,
                embedding_dim,
                embedding_blob,
                embedding_updated_at
            FROM search_documents
            {where_clause}
            ORDER BY video_id
            """
        ).fetchall()
        return [self._row_to_search_document(dict(row)) for row in rows]

    def search_sparse(self, match_query: str, *, limit: int) -> list[dict]:
        if not match_query.strip():
            return []
        rows = self._execute(
            """
            SELECT
                doc.video_id,
                doc.path,
                doc.duration,
                doc.tags_json,
                doc.description,
                doc.caption_text,
                doc.search_payload,
                doc.caption_model,
                doc.caption_updated_at,
                doc.embedding_model,
                doc.embedding_dim,
                bm25(search_fts, 5.0, 2.0, 1.0) AS bm25_score
            FROM search_fts
            JOIN search_documents AS doc ON doc.video_id = search_fts.video_id
            WHERE search_fts MATCH ?
            ORDER BY bm25(search_fts, 5.0, 2.0, 1.0)
            LIMIT ?
            """,
            (match_query, limit),
        ).fetchall()
        return [self._row_to_search_document(dict(row)) for row in rows]

    def _row_to_search_document(self, row: dict) -> dict:
        row = self._normalize_record_paths(row)
        tags_value = row.get("tags_json")
        if isinstance(tags_value, str):
            try:
                row["tags"] = json.loads(tags_value)
            except Exception:
                row["tags"] = []
        else:
            row["tags"] = []

        embedding_blob = row.get("embedding_blob")
        embedding_dim = row.get("embedding_dim")
        if embedding_blob is not None and embedding_dim:
            row["embedding_vector"] = np.frombuffer(
                embedding_blob, dtype=np.float32, count=int(embedding_dim)
            ).copy()
        else:
            row["embedding_vector"] = None
        return row

    def close(self):
        with self._lock:
            self.conn.close()
