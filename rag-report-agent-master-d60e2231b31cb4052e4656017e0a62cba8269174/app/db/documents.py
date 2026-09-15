import asyncio
import logging

import psycopg

from app.config import settings

logger = logging.getLogger(__name__)


def load_doc_meta(kb_id: str, doc_ids: list[str], user_id: str | None = None) -> dict[str, dict]:
    if not doc_ids:
        return {}
    with psycopg.connect(settings.pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.doc_id::text, d.doc_name, d.doc_description, b.created_at
                FROM documents d
                JOIN document_bindings b ON b.doc_id = d.doc_id
                WHERE d.doc_id::text = ANY(%s)
                  AND d.status = 'ready'
                  AND b.kb_id = %s
                ORDER BY b.created_at DESC
                """,
                (doc_ids, kb_id),
            )
            rows = cur.fetchall()
    return {
        row[0]: {
            "doc_name": row[1] or "",
            "doc_description": row[2] or "",
            "binding_created_at": row[3],
        }
        for row in rows
    }


async def load_doc_meta_async(kb_id: str, doc_ids: list[str], user_id: str | None = None) -> dict[str, dict]:
    try:
        return await asyncio.to_thread(load_doc_meta, kb_id, doc_ids, user_id)
    except Exception:
        logger.exception("load_doc_meta failed")
        return {}
