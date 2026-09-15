from __future__ import annotations

import json
import os
from typing import Any, Mapping

from .errors import DuplicateDocumentError
from .storage_base import WorkspaceStore, make_meta_entry

SUPPORTED_DOC_TYPES = {"pdf", "md", "markdown", "docx", "txt", "html", "xlsx", "pptx"}


def is_complete_raw_mineru(value: Any) -> bool:
    """Return whether a stored MinerU payload has the complete public shape."""
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("md_content"), str)
        and isinstance(value.get("content_list"), list)
        and isinstance(value.get("middle_json"), Mapping)
    )


def _remove_fields(value: Any, *, fields: set[str]) -> Any:
    if isinstance(value, list):
        return [_remove_fields(item, fields=fields) for item in value]
    if isinstance(value, dict):
        return {
            key: _remove_fields(item, fields=fields)
            for key, item in value.items()
            if key not in fields
        }
    return value


class PostgresWorkspaceStore(WorkspaceStore):
    """Workspace store backed by PostgreSQL tables."""

    def __init__(
        self,
        dsn: str | None = None,
        auto_init_tables: bool = True,
        user_id: str | None = None,
        kb_id: str | None = None,
        session_id: str | None = None,
        scope_by_session: bool | None = None,
    ):
        self.dsn = dsn or os.getenv("POSTGRES_DSN") or ""
        if not self.dsn:
            raise ValueError("PostgreSQL DSN is required for postgres backend.")
        self.user_id = str(user_id or os.getenv("PAGEINDEX_USER_ID") or os.getenv("DA_USER_ID") or "system").strip() or "system"
        self.kb_id = str(kb_id or os.getenv("PAGEINDEX_KB_ID") or os.getenv("DA_KB_ID") or "default").strip() or "default"
        raw_session = str(session_id or os.getenv("PAGEINDEX_SESSION_ID") or os.getenv("DA_SESSION_ID") or "").strip()
        self.session_id = raw_session or None
        self.db_batch_size = max(1, self._env_int("INGEST_DB_BATCH_SIZE", 500))
        # Retained as an inert compatibility attribute. Document visibility is
        # defined only by user_id + kb_id; session_id is upload metadata.
        self.scope_by_session = False
        self._driver = self._resolve_driver()
        if auto_init_tables:
            self._init_tables()

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        raw = str(os.getenv(name, "") or "").strip()
        if not raw:
            return int(default)
        try:
            return int(raw)
        except Exception:
            return int(default)

    def _scope_clause(self, *, include_where: bool = True, table_alias: str | None = None) -> tuple[str, list[Any]]:
        prefix_col = f"{table_alias}." if table_alias else ""
        clauses: list[str] = [f"{prefix_col}user_id = %s"]
        clauses.append(f"{prefix_col}kb_id = %s")
        params: list[Any] = [self.user_id, self.kb_id]
        prefix = "WHERE " if include_where else " AND "
        return f"{prefix}{' AND '.join(clauses)}", params

    @staticmethod
    def _resolve_driver() -> str:
        try:
            import psycopg  # noqa: F401

            return "psycopg"
        except Exception:
            pass
        try:
            import psycopg2  # noqa: F401

            return "psycopg2"
        except Exception:
            pass
        raise RuntimeError(
            "No PostgreSQL driver found. Install one of: `pip install psycopg[binary]` "
            "or `pip install psycopg2-binary`."
        )

    def _connect(self):
        if self._driver == "psycopg":
            import psycopg

            return psycopg.connect(self.dsn)
        import psycopg2

        return psycopg2.connect(self.dsn)

    def _execute_values(
        self,
        cur: Any,
        insert_sql: str,
        rows: list[tuple[Any, ...]],
        *,
        batch_size: int | None = None,
    ) -> None:
        """Insert rows in bounded batches inside the caller's transaction."""
        if not rows:
            return
        batch_size = max(1, int(batch_size or self.db_batch_size))
        if self._driver == "psycopg2":
            from psycopg2.extras import execute_values

            for start in range(0, len(rows), batch_size):
                execute_values(cur, insert_sql, rows[start : start + batch_size], page_size=batch_size)
            return

        # psycopg v3 does not expose psycopg2.extras.execute_values. Build the
        # VALUES placeholders per batch to keep one round trip per batch.
        value_placeholder = "(" + ", ".join(["%s"] * len(rows[0])) + ")"
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            placeholders = ", ".join([value_placeholder] * len(batch))
            params = [value for row in batch for value in row]
            cur.execute(insert_sql % placeholders, params)

    def _init_tables(self) -> None:
        sql = """
        CREATE TABLE IF NOT EXISTS documents (
          doc_id uuid PRIMARY KEY,
          doc_name varchar(512) NOT NULL,
          doc_type varchar(16) NOT NULL CHECK (doc_type IN ('pdf', 'md', 'markdown', 'docx', 'txt', 'html', 'xlsx', 'pptx')),
          doc_description text,
          file_path text,
          file_oss_key varchar(512),
          file_size bigint,
          file_mtime double precision,
          file_head_md5 varchar(64),
          file_md5 varchar(64),
          content_hash varchar(128),
          paragraph_count integer,
          page_count integer,
          line_count integer,
          node_count integer,
          status varchar(32) NOT NULL DEFAULT 'ready',
          raw jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

        CREATE TABLE IF NOT EXISTS document_bindings (
          doc_id uuid NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
          user_id varchar(64) NOT NULL,
          kb_id varchar(128) NOT NULL,
          session_id varchar(64),
          is_temporary boolean NOT NULL DEFAULT false,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (doc_id, user_id, kb_id)
        );
        CREATE INDEX IF NOT EXISTS idx_document_bindings_user_kb ON document_bindings(user_id, kb_id);
        CREATE INDEX IF NOT EXISTS idx_document_bindings_session ON document_bindings(session_id) WHERE session_id IS NOT NULL;
        DO $$
        BEGIN
          IF to_regclass('public.ingestion_tasks') IS NOT NULL THEN
            EXECUTE $backfill$
              INSERT INTO document_bindings (doc_id, user_id, kb_id, session_id, is_temporary)
              SELECT
                d.doc_id,
                t.user_id,
                t.kb_id,
                t.session_id,
                t.is_temp
              FROM documents d
              JOIN LATERAL (
                SELECT user_id, kb_id, session_id, is_temp
                FROM ingestion_tasks it
                WHERE it.doc_id = d.doc_id::text
                   OR it.persisted_doc_id = d.doc_id::text
                   OR it.persisted_doc_ids @> to_jsonb(ARRAY[d.doc_id::text])
                ORDER BY it.updated_at DESC, it.created_at DESC
                LIMIT 1
              ) t ON TRUE
              ON CONFLICT (doc_id, user_id, kb_id) DO NOTHING
            $backfill$;
          END IF;
        END $$;

        CREATE TABLE IF NOT EXISTS doc_nodes (
          id bigserial PRIMARY KEY,
          doc_id uuid NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
          node_id varchar(64) NOT NULL,
          parent_node_id varchar(64),
          sibling_order integer NOT NULL DEFAULT 0,
          level smallint,
          title text,
          text text,
          summary text,
          paragraph_index integer,
          start_index integer,
          end_index integer,
          child_count smallint NOT NULL DEFAULT 0,
          CONSTRAINT uq_doc_node UNIQUE (doc_id, node_id)
        );
        CREATE INDEX IF NOT EXISTS idx_doc_nodes_doc_id ON doc_nodes(doc_id);
        CREATE INDEX IF NOT EXISTS idx_doc_nodes_parent ON doc_nodes(doc_id, parent_node_id);

        CREATE TABLE IF NOT EXISTS doc_pages (
          id bigserial PRIMARY KEY,
          doc_id uuid NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
          page_number integer NOT NULL,
          content text,
          CONSTRAINT uq_doc_page UNIQUE (doc_id, page_number)
        );
        CREATE INDEX IF NOT EXISTS idx_doc_pages_doc_id ON doc_pages(doc_id);

        ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_size bigint;
        ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_mtime double precision;
        ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_head_md5 varchar(64);
        ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_md5 varchar(64);
        ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash varchar(128);
        DROP INDEX IF EXISTS idx_documents_user_id;
        DROP INDEX IF EXISTS idx_documents_session_id;
        ALTER TABLE documents DROP COLUMN IF EXISTS user_id;
        ALTER TABLE documents DROP COLUMN IF EXISTS session_id;
        ALTER TABLE documents DROP COLUMN IF EXISTS is_temporary;
        ALTER TABLE doc_nodes ADD COLUMN IF NOT EXISTS sibling_order integer NOT NULL DEFAULT 0;
        ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_doc_type_check;
        ALTER TABLE documents ADD CONSTRAINT documents_doc_type_check
          CHECK (doc_type IN ('pdf', 'md', 'markdown', 'docx', 'txt', 'html', 'xlsx', 'pptx'));
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()

    def load_meta(self) -> dict[str, dict[str, Any]]:
        scope_sql, scope_params = self._scope_clause(include_where=True, table_alias="b")
        sql = f"""
        SELECT
          d.doc_id::text, d.doc_type, d.doc_name, d.doc_description, d.file_path, d.page_count, d.line_count,
          d.file_size, d.file_mtime, d.file_head_md5, d.file_md5, d.content_hash, b.session_id, b.is_temporary
        FROM documents d
        JOIN document_bindings b ON b.doc_id = d.doc_id
        {scope_sql}
        """
        out: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(scope_params))
                for row in cur.fetchall():
                    (
                        doc_id,
                        doc_type,
                        doc_name,
                        doc_description,
                        file_path,
                        page_count,
                        line_count,
                        file_size,
                        file_mtime,
                        file_head_md5,
                        file_md5,
                        content_hash,
                        session_id,
                        is_temporary,
                    ) = row
                    doc = {
                        "id": doc_id,
                        "type": doc_type or "",
                        "doc_name": doc_name or "",
                        "doc_description": doc_description or "",
                        "path": file_path or "",
                        "page_count": page_count,
                        "line_count": line_count,
                        "file_size": file_size,
                        "file_mtime": file_mtime,
                        "file_head_md5": file_head_md5,
                        "file_md5": file_md5,
                        "content_hash": content_hash,
                        "session_id": session_id,
                        "is_temporary": bool(is_temporary),
                    }
                    meta = make_meta_entry(doc)
                    meta["file_size"] = file_size
                    meta["file_mtime"] = file_mtime
                    meta["file_head_md5"] = file_head_md5
                    meta["file_md5"] = file_md5
                    meta["content_hash"] = content_hash
                    meta["session_id"] = session_id
                    meta["is_temporary"] = bool(is_temporary)
                    out[str(doc_id)] = meta
        return out

    @staticmethod
    def _flatten_nodes(structure: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seq = {"n": 0}

        def walk(nodes: list[dict[str, Any]], parent_id: str | None, depth: int) -> None:
            for idx, node in enumerate(nodes):
                seq["n"] += 1
                raw_node_id = node.get("node_id")
                node_id = str(raw_node_id) if raw_node_id is not None else f"auto_{seq['n']}"
                children = node.get("nodes") or []
                level = node.get("level", depth)
                rows.append(
                    {
                        "node_id": node_id,
                        "parent_node_id": parent_id,
                        "sibling_order": idx,
                        "level": level,
                        "title": node.get("title"),
                        "text": node.get("text"),
                        "summary": node.get("summary"),
                        "paragraph_index": node.get("paragraph_index"),
                        "start_index": node.get("start_index"),
                        "end_index": node.get("end_index"),
                        "child_count": len(children),
                    }
                )
                if children:
                    walk(children, node_id, depth + 1)

        if isinstance(structure, list):
            walk(structure, None, 1)
        return rows

    @staticmethod
    def _slim_raw_doc(doc: dict[str, Any]) -> dict[str, Any]:
        raw_doc = dict(doc)
        # Scope metadata belongs to document_bindings, not shared document content.
        raw_doc.pop("session_id", None)
        raw_doc.pop("is_temporary", None)
        # Pages are normalized into doc_pages; keeping them in raw duplicates full text.
        raw_doc.pop("pages", None)
        if raw_doc.get("structure"):
            raw_doc["structure"] = _remove_fields(raw_doc["structure"], fields={"text"})
        return raw_doc

    @staticmethod
    def _normalize_pages(pages: Any) -> list[tuple[int, str]]:
        if not isinstance(pages, list):
            return []
        out: list[tuple[int, str]] = []
        for item in pages:
            if not isinstance(item, dict):
                continue
            page = item.get("page")
            if page is None:
                continue
            out.append((int(page), item.get("content") or ""))
        return out

    def get_binding_doc_name(self, doc_id: str, user_id: str, kb_id: str) -> str | None:
        """Return the existing document name for a binding in the current scope."""
        sql = """
        SELECT d.doc_name
        FROM document_bindings b
        JOIN documents d ON d.doc_id = b.doc_id
        WHERE b.doc_id = %s AND b.user_id = %s AND b.kb_id = %s
        LIMIT 1
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (doc_id, user_id, kb_id))
                row = cur.fetchone()
        if not row:
            return None
        return str(row[0] or "") or None

    def has_complete_raw_mineru(self, doc_id: str) -> bool:
        """Check the MinerU raw payload without loading normalized pages or nodes."""
        sql = """
        SELECT raw -> 'raw_mineru'
        FROM documents
        WHERE doc_id = %s
        LIMIT 1
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (doc_id,))
                row = cur.fetchone()
        if not row:
            return False
        raw_mineru = row[0]
        if isinstance(raw_mineru, str):
            try:
                raw_mineru = json.loads(raw_mineru)
            except json.JSONDecodeError:
                return False
        return is_complete_raw_mineru(raw_mineru)

    def update_raw_mineru_repair(
        self,
        doc_id: str,
        repair_id: str,
        updates: Mapping[str, Any],
    ) -> None:
        """Patch repair state only for the currently claimed repair."""
        payload = dict(updates)
        sql = """
        UPDATE documents
        SET raw = jsonb_set(
          CASE WHEN jsonb_typeof(raw) = 'object' THEN raw ELSE '{}'::jsonb END,
          '{raw_mineru_repair}',
          (
            CASE
              WHEN jsonb_typeof(raw -> 'raw_mineru_repair') = 'object'
                THEN raw -> 'raw_mineru_repair'
              ELSE '{}'::jsonb
            END
            || %s::jsonb
          ),
          true
        )
        WHERE doc_id = %s
          AND raw -> 'raw_mineru_repair' ->> 'repair_id' = %s
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (json.dumps(payload, ensure_ascii=False), doc_id, repair_id),
                )
            conn.commit()

    def patch_raw_mineru(
        self,
        doc_id: str,
        raw_mineru: Mapping[str, Any],
        repair_id: str | None = None,
    ) -> None:
        """Patch only raw.raw_mineru, preserving every other document field."""
        if not is_complete_raw_mineru(raw_mineru):
            raise ValueError("raw_mineru payload is incomplete")
        sql = """
        UPDATE documents
        SET raw = jsonb_set(
          CASE
            WHEN jsonb_typeof(raw) = 'object' THEN raw
            ELSE '{}'::jsonb
          END,
          '{raw_mineru}',
          %s::jsonb,
          true
        )
        WHERE doc_id = %s
          AND (%s IS NULL OR raw -> 'raw_mineru_repair' ->> 'repair_id' = %s)
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (json.dumps(dict(raw_mineru), ensure_ascii=False), doc_id, repair_id, repair_id),
                )
                if cur.rowcount != 1:
                    raise KeyError(f"document not found: {doc_id}")
                cur.execute(
                    """
                    UPDATE documents
                    SET raw = jsonb_set(
                      raw,
                      '{raw_mineru_repair}',
                      (
                        CASE
                          WHEN jsonb_typeof(raw -> 'raw_mineru_repair') = 'object'
                            THEN raw -> 'raw_mineru_repair'
                          ELSE '{}'::jsonb
                        END
                        || jsonb_build_object(
                          'status', 'completed',
                          'retryable', false,
                          'updated_at', to_char(
                            NOW() AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                          )
                        )
                      ),
                      true
                    )
                    WHERE doc_id = %s
                      AND raw ? 'raw_mineru_repair'
                      AND (%s IS NULL OR raw -> 'raw_mineru_repair' ->> 'repair_id' = %s)
                    """,
                    (doc_id, repair_id, repair_id),
                )
            conn.commit()

    def save_doc(self, doc_id: str, doc: dict[str, Any]) -> None:
        doc_type = str(doc.get("type") or "").lower()
        if doc_type not in SUPPORTED_DOC_TYPES:
            raise ValueError(f"Unsupported doc type for postgres backend: {doc_type}")

        node_rows = self._flatten_nodes(doc.get("structure"))
        page_rows = self._normalize_pages(doc.get("pages"))
        raw_doc = self._slim_raw_doc(doc)

        upsert_doc_sql = """
        INSERT INTO documents (
          doc_id, doc_name, doc_type, doc_description, file_path, file_oss_key,
          file_size, file_mtime, file_head_md5, file_md5, content_hash,
          paragraph_count, page_count, line_count, node_count, raw
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
        )
        ON CONFLICT (doc_id) DO UPDATE SET
          doc_name = EXCLUDED.doc_name,
          doc_type = EXCLUDED.doc_type,
          doc_description = EXCLUDED.doc_description,
          file_path = EXCLUDED.file_path,
          file_oss_key = EXCLUDED.file_oss_key,
          file_size = EXCLUDED.file_size,
          file_mtime = EXCLUDED.file_mtime,
          file_head_md5 = EXCLUDED.file_head_md5,
          file_md5 = EXCLUDED.file_md5,
          content_hash = EXCLUDED.content_hash,
          paragraph_count = EXCLUDED.paragraph_count,
          page_count = EXCLUDED.page_count,
          line_count = EXCLUDED.line_count,
          node_count = EXCLUDED.node_count,
          raw = EXCLUDED.raw,
          updated_at = now()
        """
        duplicate_binding_sql = """
        SELECT d.doc_name
        FROM document_bindings b
        JOIN documents d ON d.doc_id = b.doc_id
        WHERE b.doc_id = %s AND b.user_id = %s AND b.kb_id = %s
        LIMIT 1
        """
        insert_binding_sql = """
        INSERT INTO document_bindings (
          doc_id, user_id, kb_id, session_id, is_temporary
        ) VALUES (
          %s, %s, %s, %s, %s
        )
        ON CONFLICT (doc_id, user_id, kb_id) DO NOTHING
        RETURNING doc_id
        """

        doc_name = doc.get("doc_name") or doc.get("path") or str(doc_id)
        doc_params = (
            doc_id,
            doc_name,
            doc_type,
            doc.get("doc_description"),
            doc.get("path"),
            doc.get("file_oss_key"),
            doc.get("file_size"),
            doc.get("file_mtime"),
            doc.get("file_head_md5"),
            doc.get("file_md5"),
            doc.get("content_hash"),
            doc.get("paragraph_count"),
            doc.get("page_count"),
            doc.get("line_count"),
            len(node_rows),
            json.dumps(raw_doc, ensure_ascii=False),
        )

        insert_node_sql = """
        INSERT INTO doc_nodes (
          doc_id, node_id, parent_node_id, sibling_order, level, title, text, summary,
          paragraph_index, start_index, end_index, child_count
        ) VALUES %s
        """
        insert_page_sql = "INSERT INTO doc_pages (doc_id, page_number, content) VALUES %s"

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(duplicate_binding_sql, (doc_id, self.user_id, self.kb_id))
                existing = cur.fetchone()
                if existing:
                    raise DuplicateDocumentError(existing[0])
                cur.execute(upsert_doc_sql, doc_params)
                cur.execute(
                    insert_binding_sql,
                    (
                        doc_id,
                        self.user_id,
                        self.kb_id,
                        doc.get("session_id") or self.session_id,
                        bool(doc.get("is_temporary")),
                    ),
                )
                if cur.fetchone() is None:
                    cur.execute(duplicate_binding_sql, (doc_id, self.user_id, self.kb_id))
                    existing = cur.fetchone()
                    raise DuplicateDocumentError(existing[0] if existing else doc_name)
                cur.execute("DELETE FROM doc_nodes WHERE doc_id = %s", (doc_id,))
                cur.execute("DELETE FROM doc_pages WHERE doc_id = %s", (doc_id,))
                if node_rows:
                    self._execute_values(
                        cur,
                        insert_node_sql,
                        [
                            (
                                doc_id,
                                row["node_id"],
                                row["parent_node_id"],
                                row["sibling_order"],
                                row["level"],
                                row["title"],
                                row["text"],
                                row["summary"],
                                row["paragraph_index"],
                                row["start_index"],
                                row["end_index"],
                                row["child_count"],
                            )
                            for row in node_rows
                        ],
                    )
                if page_rows:
                    self._execute_values(cur, insert_page_sql, [(doc_id, p, c) for p, c in page_rows])
            conn.commit()

    def _rebuild_structure_from_nodes(self, doc_id: str) -> list[dict[str, Any]]:
        scope_sql, scope_params = self._scope_clause(include_where=False, table_alias="b")
        sql = f"""
        SELECT node_id, parent_node_id, level, title, text, summary, paragraph_index, start_index, end_index, child_count
        FROM doc_nodes n
        JOIN documents d ON d.doc_id = n.doc_id
        JOIN document_bindings b ON b.doc_id = d.doc_id
        WHERE n.doc_id = %s {scope_sql}
        ORDER BY COALESCE(parent_node_id, ''), sibling_order, id
        """
        rows: list[tuple[Any, ...]] = []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple([doc_id, *scope_params]))
                rows = cur.fetchall()
        if not rows:
            return []

        nodes_by_id: dict[str, dict[str, Any]] = {}
        children_by_parent: dict[str | None, list[dict[str, Any]]] = {}
        for row in rows:
            node_id, parent_node_id, level, title, text, summary, paragraph_index, start_index, end_index, child_count = row
            node = {
                "node_id": str(node_id),
                "title": title,
                "text": text,
                "summary": summary,
                "paragraph_index": paragraph_index,
                "start_index": start_index,
                "end_index": end_index,
                "level": level,
                "nodes": [],
            }
            if child_count == 0:
                node.pop("nodes", None)
            nodes_by_id[str(node_id)] = node
            key = str(parent_node_id) if parent_node_id is not None else None
            children_by_parent.setdefault(key, []).append(node)

        for parent_id, children in children_by_parent.items():
            if parent_id is None:
                continue
            parent = nodes_by_id.get(parent_id)
            if parent is not None:
                parent["nodes"] = children
        return children_by_parent.get(None, [])

    def _rebuild_pages(self, doc_id: str) -> list[dict[str, Any]]:
        scope_sql, scope_params = self._scope_clause(include_where=False, table_alias="b")
        sql = f"""
        SELECT p.page_number, p.content
        FROM doc_pages p
        JOIN documents d ON d.doc_id = p.doc_id
        JOIN document_bindings b ON b.doc_id = d.doc_id
        WHERE p.doc_id = %s {scope_sql}
        ORDER BY p.page_number
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple([doc_id, *scope_params]))
                rows = cur.fetchall()
        return [{"page": int(p), "content": c or ""} for p, c in rows]

    def load_full_doc(self, doc_id: str) -> dict[str, Any] | None:
        scope_sql, scope_params = self._scope_clause(include_where=False, table_alias="b")
        sql = f"""
        SELECT
          d.doc_id::text, d.doc_type, d.doc_name, d.doc_description, d.file_path, d.page_count, d.line_count,
          d.file_size, d.file_mtime, d.file_head_md5, d.file_md5, d.content_hash, b.session_id, b.is_temporary, d.raw
        FROM documents d
        JOIN document_bindings b ON b.doc_id = d.doc_id
        WHERE d.doc_id = %s {scope_sql}
        """
        row = None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple([doc_id, *scope_params]))
                row = cur.fetchone()
        if not row:
            return None

        (
            rid,
            doc_type,
            doc_name,
            doc_description,
            file_path,
            page_count,
            line_count,
            file_size,
            file_mtime,
            file_head_md5,
            file_md5,
            content_hash,
            session_id,
            is_temporary,
            raw,
        ) = row
        full: dict[str, Any] = {
            "id": rid,
            "type": doc_type or "",
            "doc_name": doc_name or "",
            "doc_description": doc_description or "",
            "path": file_path or "",
            "session_id": session_id,
            "is_temporary": bool(is_temporary),
        }
        if raw:
            if isinstance(raw, str):
                parsed = json.loads(raw)
            else:
                parsed = raw
            if isinstance(parsed, dict):
                full.update(parsed)
        # Binding scope is authoritative even when an older raw payload still
        # contains these keys from before document_bindings was introduced.
        full["session_id"] = session_id
        full["is_temporary"] = bool(is_temporary)
        if page_count is not None:
            full["page_count"] = page_count
        if line_count is not None:
            full["line_count"] = line_count
        if file_size is not None:
            full["file_size"] = file_size
        if file_mtime is not None:
            full["file_mtime"] = file_mtime
        if file_head_md5:
            full["file_head_md5"] = file_head_md5
        if file_md5:
            full["file_md5"] = file_md5
        if content_hash:
            full["content_hash"] = content_hash
        structure = self._rebuild_structure_from_nodes(doc_id)
        if structure:
            full["structure"] = structure
        pages = self._rebuild_pages(doc_id)
        if pages:
            full["pages"] = pages
        return full

    def delete_doc(self, doc_id: str) -> None:
        scope_sql, scope_params = self._scope_clause(include_where=False)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM document_bindings WHERE doc_id = %s {scope_sql}", tuple([doc_id, *scope_params]))
                cur.execute(
                    """
                    DELETE FROM documents d
                    WHERE d.doc_id = %s
                      AND NOT EXISTS (SELECT 1 FROM document_bindings b WHERE b.doc_id = d.doc_id)
                    """,
                    (doc_id,),
                )
            conn.commit()
