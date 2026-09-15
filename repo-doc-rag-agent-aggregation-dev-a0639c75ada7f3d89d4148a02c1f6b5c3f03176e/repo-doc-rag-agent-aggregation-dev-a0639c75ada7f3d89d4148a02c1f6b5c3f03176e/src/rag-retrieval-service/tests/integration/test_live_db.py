from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.db.pool import PostgresPool
from app.db.repositories import RetrievalRepository


def _dsn() -> str:
    direct = os.getenv("POSTGRES_DSN", "")
    if direct:
        return direct
    env_path = os.getenv("RAG_TEST_ENV_FILE", "")
    if env_path and Path(env_path).is_file():
        return str(dotenv_values(env_path).get("POSTGRES_DSN") or "")
    return ""


@pytest.fixture
def live_repository():
    dsn = _dsn()
    if not dsn:
        pytest.skip("POSTGRES_DSN or RAG_TEST_ENV_FILE is not configured")
    pool = PostgresPool(
        dsn=dsn,
        min_size=1,
        max_size=3,
        acquire_timeout_seconds=2,
        query_timeout_seconds=10,
    )
    pool.open()
    pool.ping()
    try:
        yield RetrievalRepository(pool), pool
    finally:
        pool.close()


def _ready_document_ids(pool: PostgresPool, *, limit: int = 3) -> list[str]:
    with pool.transaction(read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT doc_id::text
                FROM documents
                WHERE status = 'ready'
                ORDER BY doc_id
                LIMIT %s
                """,
                [limit],
            )
            return [str(row[0]) for row in cursor.fetchall()]


@pytest.mark.live_db
def test_live_scope_uses_doc_ids_without_user_or_kb_isolation(live_repository) -> None:
    repository, pool = live_repository
    doc_ids = _ready_document_ids(pool)
    assert doc_ids

    profiles, missing = repository.fetch_scope(
        user_id="not-the-document-owner",
        kb_id="not-the-document-kb",
        doc_ids=doc_ids,
        temp_doc_ids=[],
        session_id=None,
    )
    same_profiles, same_missing = repository.fetch_scope(
        user_id="another-user",
        kb_id="another-kb",
        doc_ids=doc_ids,
        temp_doc_ids=[],
        session_id=None,
    )

    assert missing == []
    assert same_missing == []
    assert [profile.doc_id for profile in profiles] == [profile.doc_id for profile in same_profiles]
    assert set(profile.doc_id for profile in profiles) == set(doc_ids)


@pytest.mark.live_db
def test_live_non_contiguous_pages_and_nodes_have_no_hidden_limit(live_repository) -> None:
    repository, pool = live_repository
    with pool.transaction(read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT doc_id::text
                FROM documents
                WHERE status = 'ready' AND page_count >= 3 AND node_count > 0
                ORDER BY doc_id
                LIMIT 1
                """
            )
            row = cursor.fetchone()
    assert row is not None
    profiles, _ = repository.fetch_scope(
        user_id="unrelated-user",
        kb_id="unrelated-kb",
        doc_ids=[str(row[0])],
        temp_doc_ids=[],
        session_id=None,
    )
    profile = next(item for item in profiles if item.page_count >= 3 and item.node_count > 0)
    wanted = [1, profile.page_count, 2]
    pages = repository.fetch_pages(
        user_id="eval-user",
        kb_id="eval-rag-kb",
        doc_id=profile.doc_id,
        pages=wanted,
        session_id=None,
    )
    nodes = repository.fetch_nodes(
        user_id="eval-user",
        kb_id="eval-rag-kb",
        doc_id=profile.doc_id,
        session_id=None,
    )

    assert [page.page_number for page in pages] == sorted(set(wanted))
    assert len(nodes) == profile.node_count
    assert any(node.summary for node in nodes)
    assert all(
        node.start_page is None
        or node.end_page is None
        or (1 <= node.start_page <= node.end_page <= profile.page_count)
        for node in nodes
    )
