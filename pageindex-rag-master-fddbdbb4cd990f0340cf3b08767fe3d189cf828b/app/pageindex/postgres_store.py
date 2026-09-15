from __future__ import annotations

import json
import os
from typing import Any

from .storage_base import WorkspaceStore, make_meta_entry


class PostgresWorkspaceStore(WorkspaceStore):
    """Workspace store backed by PostgreSQL tables."""

    def __init__(
        self,
        dsn: str | None = None,
        auto_init_tables: bool = True,
        user_id: str | None = None,
        session_id: str | None = None,
        scope_by_session: bool | None = None,
    ):
        self.dsn = dsn or os.getenv("POSTGRES_DSN") or ""
        if not self.dsn:
            raise ValueError("PostgreSQL DSN is required for postgres backend.")
        self.user_id = str(user_id or os.getenv("PAGEINDEX_USER_ID") or os.getenv("DA_USER_ID") or "system").strip() or "system"
        raw_disable_user_scope = str(
            os.getenv("PAGEINDEX_DISABLE_USER_SCOPE") or os.getenv("DA_DISABLE_USER_SCOPE") or ""
        ).strip().lower()
        self.disable_user_scope = self.user_id == "*" or raw_disable_user_scope in {"1", "true", "yes", "on"}
        raw_session = str(session_id or os.getenv("PAGEINDEX_SESSION_ID") or os.getenv("DA_SESSION_ID") or "").strip()
        self.session_id = raw_session or None
        if scope_by_session is None:
            raw_scope = str(os.getenv("PAGEINDEX_SCOPE_BY_SESSION") or os.getenv("DA_SCOPE_BY_SESSION") or "").strip().lower()
            self.scope_by_session = raw_scope in {"1", "true", "yes", "on"}
        else:
            self.scope_by_session = bool(scope_by_session)
        self._driver = self._resolve_driver()
        if auto_init_tables:
            self._init_tables()

    def _scope_clause(self, *, include_where: bool = True, table_alias: str | None = None) -> tuple[str, list[Any]]:
        prefix_col = f"{table_alias}." if table_alias else ""
        clauses: list[str] = []
        params: list[Any] = []
        if not self.disable_user_scope:
            clauses.append(f"{prefix_col}user_id = %s")
            params.append(self.user_id)
        if self.scope_by_session and self.session_id:
            clauses.append(f"{prefix_col}session_id = %s")
            params.append(self.session_id)
        if not clauses:
            return "", []
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

    def _init_tables(self) -> None:
        sql = """
        CREATE TABLE IF NOT EXISTS documents (
          doc_id uuid PRIMARY KEY,
          user_id varchar(64) NOT NULL DEFAULT 'system',
          session_id varchar(64),
          doc_name varchar(512) NOT NULL,
          doc_type varchar(16) NOT NULL CHECK (doc_type IN ('pdf', 'md', 'docx', 'txt')),
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
          is_temporary boolean NOT NULL DEFAULT false,
          raw jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_documents_user_id ON documents(user_id);
        CREATE INDEX IF NOT EXISTS idx_documents_session_id ON documents(session_id) WHERE session_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

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
        ALTER TABLE doc_nodes ADD COLUMN IF NOT EXISTS sibling_order integer NOT NULL DEFAULT 0;
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()

    def load_meta(self) -> dict[str, dict[str, Any]]:
        scope_sql, scope_params = self._scope_clause(include_where=True)
        sql = f"""
        SELECT
          doc_id::text, doc_type, doc_name, doc_description, file_path, page_count, line_count,
          file_size, file_mtime, file_head_md5, file_md5, content_hash
        FROM documents
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
                    }
                    meta = make_meta_entry(doc)
                    meta["file_size"] = file_size
                    meta["file_mtime"] = file_mtime
                    meta["file_head_md5"] = file_head_md5
                    meta["file_md5"] = file_md5
                    meta["content_hash"] = content_hash
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

    def save_doc(self, doc_id: str, doc: dict[str, Any]) -> None:
        doc_type = str(doc.get("type") or "").lower()
        if doc_type not in {"pdf", "md", "docx", "txt"}:
            raise ValueError(f"Unsupported doc type for postgres backend: {doc_type}")

        node_rows = self._flatten_nodes(doc.get("structure"))
        page_rows = self._normalize_pages(doc.get("pages"))

        upsert_doc_sql = """
        INSERT INTO documents (
          doc_id, user_id, session_id, doc_name, doc_type, doc_description, file_path, file_oss_key,
          file_size, file_mtime, file_head_md5, file_md5, content_hash,
          paragraph_count, page_count, line_count, node_count, raw
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
        )
        ON CONFLICT (doc_id) DO UPDATE SET
          user_id = EXCLUDED.user_id,
          session_id = EXCLUDED.session_id,
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

        doc_params = (
            doc_id,
            self.user_id,
            self.session_id,
            doc.get("doc_name") or doc.get("path") or str(doc_id),
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
            json.dumps(doc, ensure_ascii=False),
        )

        insert_node_sql = """
        INSERT INTO doc_nodes (
          doc_id, node_id, parent_node_id, sibling_order, level, title, text, summary,
          paragraph_index, start_index, end_index, child_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        insert_page_sql = "INSERT INTO doc_pages (doc_id, page_number, content) VALUES (%s, %s, %s)"

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(upsert_doc_sql, doc_params)
                cur.execute("DELETE FROM doc_nodes WHERE doc_id = %s", (doc_id,))
                cur.execute("DELETE FROM doc_pages WHERE doc_id = %s", (doc_id,))
                if node_rows:
                    cur.executemany(
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
                    cur.executemany(insert_page_sql, [(doc_id, p, c) for p, c in page_rows])
            conn.commit()

    def _rebuild_structure_from_nodes(self, doc_id: str) -> list[dict[str, Any]]:
        scope_sql, scope_params = self._scope_clause(include_where=False, table_alias="d")
        sql = f"""
        SELECT node_id, parent_node_id, level, title, text, summary, paragraph_index, start_index, end_index, child_count
        FROM doc_nodes n
        JOIN documents d ON d.doc_id = n.doc_id
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
        scope_sql, scope_params = self._scope_clause(include_where=False, table_alias="d")
        sql = f"""
        SELECT p.page_number, p.content
        FROM doc_pages p
        JOIN documents d ON d.doc_id = p.doc_id
        WHERE p.doc_id = %s {scope_sql}
        ORDER BY p.page_number
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple([doc_id, *scope_params]))
                rows = cur.fetchall()
        return [{"page": int(p), "content": c or ""} for p, c in rows]

    def load_full_doc(self, doc_id: str) -> dict[str, Any] | None:
        scope_sql, scope_params = self._scope_clause(include_where=False)
        sql = f"""
        SELECT
          doc_id::text, doc_type, doc_name, doc_description, file_path, page_count, line_count,
          file_size, file_mtime, file_head_md5, file_md5, content_hash, raw
        FROM documents
        WHERE doc_id = %s {scope_sql}
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
            raw,
        ) = row
        full: dict[str, Any] = {
            "id": rid,
            "type": doc_type or "",
            "doc_name": doc_name or "",
            "doc_description": doc_description or "",
            "path": file_path or "",
        }
        if raw:
            if isinstance(raw, str):
                parsed = json.loads(raw)
            else:
                parsed = raw
            if isinstance(parsed, dict):
                full.update(parsed)
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
        if structure and not full.get("structure"):
            full["structure"] = structure
        pages = self._rebuild_pages(doc_id)
        if pages and not full.get("pages"):
            full["pages"] = pages
        return full

    def delete_doc(self, doc_id: str) -> None:
        scope_sql, scope_params = self._scope_clause(include_where=False)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM documents WHERE doc_id = %s {scope_sql}", tuple([doc_id, *scope_params]))
            conn.commit()
