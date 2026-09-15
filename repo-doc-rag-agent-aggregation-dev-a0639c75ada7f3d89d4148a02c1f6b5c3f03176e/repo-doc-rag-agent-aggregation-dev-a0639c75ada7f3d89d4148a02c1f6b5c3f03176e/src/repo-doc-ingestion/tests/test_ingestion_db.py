from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _connect_or_skip():
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        pytest.skip("POSTGRES_DSN is not configured")
    psycopg = pytest.importorskip("psycopg")
    try:
        return psycopg.connect(dsn)
    except Exception as exc:  # pragma: no cover - depends on local postgres
        pytest.skip(f"PostgreSQL is not available: {exc}")


def test_postgres_store_allows_html_original_doc_type() -> None:
    from app.storage.postgres_store import SUPPORTED_DOC_TYPES

    assert "html" in SUPPORTED_DOC_TYPES


def test_persist_parsed_txt_document_to_postgres(tmp_path: Path) -> None:
    """解析结果应能落到 documents/doc_nodes/doc_pages 三张表。"""
    from app.parser.multiformat_parser import parse_document_to_structure
    from app.storage import PageIndexClient

    source = tmp_path / "故事.txt"
    source.write_text(
        "第一章 故事开头\n\n这是一个用于 PostgreSQL 落库测试的短故事。\n\n第二章 故事结尾\n\n测试应生成页面和节点。",
        encoding="utf-8",
    )

    doc_id = str(uuid.uuid4())
    payload = parse_document_to_structure(
        source,
        generate_summary=False,
        generate_doc_description=False,
    )
    payload["id"] = doc_id
    payload["doc_id"] = doc_id
    payload["doc_name"] = f"pytest-{source.name}"

    conn = _connect_or_skip()
    try:
        client = PageIndexClient(postgres_dsn=os.getenv("POSTGRES_DSN"))
        client.documents[doc_id] = payload
        client._save_doc(doc_id)

        with conn.cursor() as cur:
            cur.execute(
                """
                select doc_name, doc_type, page_count, node_count
                from documents
                where doc_id = %s
                """,
                (doc_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == f"pytest-{source.name}"
            assert row[1] == "txt"
            assert int(row[2] or 0) > 0
            assert int(row[3] or 0) > 0

            cur.execute(
                """
                select column_name
                from information_schema.columns
                where table_name = 'documents'
                  and column_name = any(%s)
                """,
                (["user_id", "session_id", "is_temporary"],),
            )
            assert cur.fetchall() == []

            cur.execute("select count(*) from doc_pages where doc_id = %s", (doc_id,))
            assert int(cur.fetchone()[0]) > 0

            cur.execute("select count(*) from doc_nodes where doc_id = %s", (doc_id,))
            assert int(cur.fetchone()[0]) > 0
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from documents where doc_id = %s", (doc_id,))
        conn.commit()
        conn.close()
