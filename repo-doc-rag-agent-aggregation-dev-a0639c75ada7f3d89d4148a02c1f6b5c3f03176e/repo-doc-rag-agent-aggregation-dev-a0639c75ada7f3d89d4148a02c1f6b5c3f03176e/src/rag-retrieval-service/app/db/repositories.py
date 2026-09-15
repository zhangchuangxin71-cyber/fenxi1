from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.db.pool import PostgresPool


class DocumentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    doc_name: str
    doc_type: str | None = None
    doc_description: str = ""
    page_count: int = 0
    node_count: int = 0
    is_temporary: bool = False
    session_id: str | None = None


class PageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    document_name: str
    page_number: int
    content: str


class NodeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    node_id: str
    parent_node_id: str | None
    sibling_order: int
    level: int | None
    title: str
    summary: str
    start_page: int | None
    end_page: int | None
    child_count: int


def _scope_ids(doc_ids: list[str], temp_doc_ids: list[str]) -> list[str]:
    return list(dict.fromkeys([*doc_ids, *temp_doc_ids]))


def build_document_scope_query(
    *,
    user_id: str,
    kb_id: str,
    doc_ids: list[str],
    temp_doc_ids: list[str],
    session_id: str | None,
) -> tuple[str, list[Any]]:
    requested = _scope_ids(doc_ids, temp_doc_ids)
    sql = """
        SELECT DISTINCT d.doc_id::text, d.doc_name, d.doc_type,
               COALESCE(d.doc_description, ''), COALESCE(d.page_count, 0),
               COALESCE(d.node_count, 0), COALESCE(b.is_temporary, FALSE), b.session_id
        FROM documents d
        LEFT JOIN LATERAL (
            SELECT binding.is_temporary, binding.session_id
            FROM document_bindings binding
            WHERE binding.doc_id = d.doc_id
            ORDER BY binding.updated_at DESC, binding.created_at DESC,
                     binding.user_id, binding.kb_id
            LIMIT 1
        ) b ON TRUE
        WHERE d.status = 'ready' AND d.doc_id::text = ANY(%s)
        ORDER BY d.doc_name, d.doc_id::text
    """
    return sql, [requested]


def build_page_query(
    *,
    user_id: str,
    kb_id: str,
    doc_id: str,
    pages: list[int] | None,
    session_id: str | None = None,
) -> tuple[str, list[Any]]:
    where = ["d.status = 'ready'", "d.doc_id::text = %s"]
    params: list[Any] = [doc_id]
    normalized_pages = sorted({int(page) for page in (pages or []) if int(page) > 0})
    if pages is not None:
        where.append("p.page_number = ANY(%s)")
        params.append(normalized_pages)
    sql = f"""
        SELECT DISTINCT d.doc_id::text, d.doc_name, p.page_number, COALESCE(p.content, '')
        FROM doc_pages p
        JOIN documents d ON d.doc_id = p.doc_id
        WHERE {" AND ".join(where)}
        ORDER BY p.page_number
    """
    return sql, params


def build_document_meta_query(
    *,
    user_id: str,
    kb_id: str,
    doc_ids: list[str],
    temp_doc_ids: list[str],
    session_id: str | None,
) -> tuple[str, list[Any]]:
    requested = _scope_ids(doc_ids, temp_doc_ids)
    sql = """
        SELECT d.doc_id::text, d.doc_name, d.doc_type,
               COALESCE(d.doc_description, ''), d.status, d.page_count,
               d.node_count, d.created_at, d.updated_at, COALESCE(b.kb_id, ''),
               COALESCE(b.user_id, ''), COALESCE(b.is_temporary, FALSE),
               b.created_at AS bound_at
        FROM documents d
        LEFT JOIN LATERAL (
            SELECT binding.kb_id, binding.user_id, binding.is_temporary,
                   binding.created_at
            FROM document_bindings binding
            WHERE binding.doc_id = d.doc_id
            ORDER BY binding.updated_at DESC, binding.created_at DESC,
                     binding.user_id, binding.kb_id
            LIMIT 1
        ) b ON TRUE
        WHERE d.doc_id::text = ANY(%s)
        ORDER BY d.doc_name, d.doc_id
    """
    return sql, [requested]


def build_document_raw_queries() -> tuple[str, str, str]:
    document_sql = """
        SELECT d.doc_id::text, d.doc_name, d.doc_type,
               COALESCE(d.doc_description, ''), d.status, d.page_count,
               d.line_count, d.node_count, COALESCE(b.is_temporary, FALSE),
               d.raw, d.file_oss_key, b.user_id, b.kb_id
        FROM documents d
        LEFT JOIN LATERAL (
            SELECT binding.is_temporary, binding.user_id, binding.kb_id
            FROM document_bindings binding
            WHERE binding.doc_id = d.doc_id
            ORDER BY binding.updated_at DESC, binding.created_at DESC,
                     binding.user_id, binding.kb_id
            LIMIT 1
        ) b ON TRUE
        WHERE d.doc_id::text = %s
        LIMIT 1
    """
    pages_sql = """
        SELECT page_number, content
        FROM doc_pages
        WHERE doc_id::text = %s
        ORDER BY page_number
    """
    nodes_sql = """
        SELECT node_id, parent_node_id, level, title, text, summary,
               paragraph_index, start_index, end_index, child_count
        FROM doc_nodes
        WHERE doc_id::text = %s
        ORDER BY COALESCE(parent_node_id, ''), sibling_order, id
    """
    return document_sql, pages_sql, nodes_sql


class RetrievalRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def fetch_scope(
        self,
        *,
        user_id: str,
        kb_id: str,
        doc_ids: list[str],
        temp_doc_ids: list[str],
        session_id: str | None,
    ) -> tuple[list[DocumentProfile], list[str]]:
        sql, params = build_document_scope_query(
            user_id=user_id,
            kb_id=kb_id,
            doc_ids=doc_ids,
            temp_doc_ids=temp_doc_ids,
            session_id=session_id,
        )
        with self._pool.transaction(read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        profiles = [
            DocumentProfile(
                doc_id=str(row[0]),
                doc_name=str(row[1] or row[0]),
                doc_type=str(row[2]) if row[2] else None,
                doc_description=str(row[3] or ""),
                page_count=int(row[4] or 0),
                node_count=int(row[5] or 0),
                is_temporary=bool(row[6]),
                session_id=str(row[7]) if row[7] else None,
            )
            for row in rows
        ]
        requested = _scope_ids(doc_ids, temp_doc_ids)
        found = {profile.doc_id for profile in profiles}
        return profiles, [doc_id for doc_id in requested if doc_id not in found]

    def fetch_pages(
        self,
        *,
        user_id: str,
        kb_id: str,
        doc_id: str,
        pages: list[int] | None = None,
        session_id: str | None = None,
    ) -> list[PageRecord]:
        sql, params = build_page_query(
            user_id=user_id,
            kb_id=kb_id,
            doc_id=doc_id,
            pages=pages,
            session_id=session_id,
        )
        with self._pool.transaction(read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        return [
            PageRecord(
                doc_id=str(row[0]),
                document_name=str(row[1] or row[0]),
                page_number=int(row[2]),
                content=str(row[3] or ""),
            )
            for row in rows
        ]

    def fetch_nodes(
        self,
        *,
        user_id: str,
        kb_id: str,
        doc_id: str,
        session_id: str | None = None,
    ) -> list[NodeRecord]:
        sql = """
            SELECT DISTINCT n.doc_id::text, n.node_id, n.parent_node_id, n.sibling_order,
                   n.level, COALESCE(n.title, ''), COALESCE(n.summary, ''),
                   n.start_index, n.end_index, n.child_count, n.id
            FROM doc_nodes n
            JOIN documents d ON d.doc_id = n.doc_id
            WHERE d.status = 'ready' AND d.doc_id::text = %s
            ORDER BY n.level NULLS FIRST, n.parent_node_id NULLS FIRST,
                     n.sibling_order, n.id
        """
        params: list[Any] = [doc_id]
        with self._pool.transaction(read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        return [
            NodeRecord(
                doc_id=str(row[0]),
                node_id=str(row[1]),
                parent_node_id=str(row[2]) if row[2] is not None else None,
                sibling_order=int(row[3] or 0),
                level=int(row[4]) if row[4] is not None else None,
                title=str(row[5] or ""),
                summary=str(row[6] or ""),
                start_page=int(row[7]) if row[7] is not None else None,
                end_page=int(row[8]) if row[8] is not None else None,
                child_count=int(row[9] or 0),
            )
            for row in rows
        ]

    def fetch_document_meta(
        self,
        *,
        user_id: str,
        kb_id: str,
        doc_ids: list[str],
        temp_doc_ids: list[str],
        session_id: str | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        permanent_ids = list(
            dict.fromkeys(str(doc_id).strip() for doc_id in doc_ids if str(doc_id).strip())
        )
        permanent_set = set(permanent_ids)
        temporary_ids = [
            doc_id
            for doc_id in dict.fromkeys(
                str(doc_id).strip() for doc_id in temp_doc_ids if str(doc_id).strip()
            )
            if doc_id not in permanent_set
        ]
        requested = [*permanent_ids, *temporary_ids]
        if not requested:
            return [], []
        sql, params = build_document_meta_query(
            user_id=user_id,
            kb_id=kb_id,
            doc_ids=permanent_ids,
            temp_doc_ids=temporary_ids,
            session_id=session_id,
        )
        with self._pool.transaction(read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        documents_by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            doc_id = str(row[0])
            documents_by_id[doc_id] = {
                "doc_id": doc_id,
                "doc_name": str(row[1] or doc_id),
                "doc_type": str(row[2]) if row[2] is not None else None,
                "doc_description": str(row[3] or ""),
                "status": str(row[4] or ""),
                "page_count": row[5],
                "node_count": row[6],
                "created_at": row[7],
                "updated_at": row[8],
                "kb_id": str(row[9]),
                "user_id": str(row[10]),
                "is_temporary": bool(row[11]),
                "bound_at": row[12],
            }
        documents = [documents_by_id[doc_id] for doc_id in requested if doc_id in documents_by_id]
        return documents, [doc_id for doc_id in requested if doc_id not in documents_by_id]

    def fetch_document_raw(self, *, user_id: str, kb_id: str, doc_id: str) -> dict[str, Any] | None:
        document_sql, pages_sql, nodes_sql = build_document_raw_queries()
        with self._pool.transaction(read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(document_sql, [doc_id])
                row = cursor.fetchone()
                if row is None:
                    return None
                cursor.execute(pages_sql, [doc_id])
                page_rows = cursor.fetchall()
                cursor.execute(nodes_sql, [doc_id])
                node_rows = cursor.fetchall()
        raw = _as_json_object(row[9])
        raw_mineru = raw.get("raw_mineru")
        raw_mineru_repair = raw.get("raw_mineru_repair")
        return {
            "doc_id": str(row[0]),
            "doc_name": str(row[1] or row[0]),
            "doc_type": str(row[2] or ""),
            "doc_description": str(row[3] or ""),
            "status": str(row[4] or ""),
            "page_count": row[5],
            "line_count": row[6],
            "node_count": row[7],
            "is_temporary": bool(row[8]),
            "pages": [
                {"page": int(page), "content": str(content or "")} for page, content in page_rows
            ],
            "structure": _rebuild_document_structure(node_rows),
            "raw_mineru": dict(raw_mineru) if isinstance(raw_mineru, Mapping) else None,
            "raw_mineru_repair": (
                dict(raw_mineru_repair) if isinstance(raw_mineru_repair, Mapping) else None
            ),
            "file_oss_key": str(row[10]) if row[10] else None,
            "_binding_user_id": str(row[11]) if row[11] else None,
            "_binding_kb_id": str(row[12]) if row[12] else None,
        }

    def fetch_document_raw_state(
        self, *, user_id: str, kb_id: str, doc_id: str
    ) -> dict[str, Any] | None:
        sql = """
            SELECT d.doc_id::text, d.raw, d.file_oss_key
            FROM documents d
            WHERE d.doc_id::text = %s
            LIMIT 1
        """
        with self._pool.transaction(read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, [doc_id])
                row = cursor.fetchone()
        if row is None:
            return None
        raw = _as_json_object(row[1])
        raw_mineru = raw.get("raw_mineru")
        repair = raw.get("raw_mineru_repair")
        return {
            "doc_id": str(row[0]),
            "raw_mineru": dict(raw_mineru) if isinstance(raw_mineru, Mapping) else None,
            "raw_mineru_repair": dict(repair) if isinstance(repair, Mapping) else None,
            "file_oss_key": str(row[2]) if row[2] else None,
        }


def _as_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _rebuild_document_structure(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    nodes_by_id: dict[str, dict[str, Any]] = {}
    children_by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for row in rows:
        node_id, parent_node_id, level, title, text, summary = row[:6]
        paragraph_index, start_index, end_index, child_count = row[6:]
        node: dict[str, Any] = {
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
        if not child_count:
            node.pop("nodes")
        nodes_by_id[str(node_id)] = node
        parent_key = str(parent_node_id) if parent_node_id is not None else None
        children_by_parent.setdefault(parent_key, []).append(node)
    for parent_id, children in children_by_parent.items():
        if parent_id is not None and parent_id in nodes_by_id:
            nodes_by_id[parent_id]["nodes"] = children
    return children_by_parent.get(None, [])
