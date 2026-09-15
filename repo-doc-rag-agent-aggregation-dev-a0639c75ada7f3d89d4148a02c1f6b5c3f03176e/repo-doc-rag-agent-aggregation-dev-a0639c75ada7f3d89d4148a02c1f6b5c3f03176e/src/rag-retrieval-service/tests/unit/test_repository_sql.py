from __future__ import annotations

from app.db.repositories import (
    RetrievalRepository,
    build_document_meta_query,
    build_document_raw_queries,
    build_document_scope_query,
    build_page_query,
)


def test_scope_query_uses_only_requested_document_ids_and_ready_status() -> None:
    sql, params = build_document_scope_query(
        user_id="u1",
        kb_id="kb1",
        doc_ids=["d1"],
        temp_doc_ids=["t1"],
        session_id="s1",
    )

    normalized = " ".join(sql.split()).lower()
    assert "b.user_id = %s" not in normalized
    assert "b.kb_id = %s" not in normalized
    assert "d.doc_id::text = any(%s)" in normalized
    assert "d.status = 'ready'" in normalized
    assert "left join lateral" in normalized
    assert "limit 1" in normalized
    assert params == [["d1", "t1"]]


def test_page_query_uses_explicit_non_contiguous_array_without_limit() -> None:
    sql, params = build_page_query(
        user_id="u1",
        kb_id="kb1",
        doc_id="d1",
        pages=[8, 2, 8, 12],
        session_id="s1",
    )

    normalized = " ".join(sql.split()).lower()
    assert "{" not in normalized
    assert "p.page_number = any(%s)" in normalized
    assert "limit" not in normalized
    assert "document_bindings" not in normalized
    assert "user_id" not in normalized
    assert "kb_id" not in normalized
    assert params == ["d1", [2, 8, 12]]


def test_page_query_ignores_user_and_knowledge_base() -> None:
    sql, _ = build_page_query(user_id="u1", kb_id="kb1", doc_id="d1", pages=None, session_id=None)

    normalized = " ".join(sql.split()).lower()
    assert "user_id" not in normalized
    assert "kb_id" not in normalized
    assert "document_bindings" not in normalized
    assert "select distinct" in normalized


def test_scope_query_treats_permanent_and_temporary_ids_as_one_doc_id_set() -> None:
    sql, params = build_document_scope_query(
        user_id="u1",
        kb_id="kb1",
        doc_ids=["regular"],
        temp_doc_ids=["temporary"],
        session_id=None,
    )

    normalized = " ".join(sql.split()).lower()
    assert "not b.is_temporary" not in normalized
    assert "b.is_temporary and" not in normalized
    assert params == [["regular", "temporary"]]


def test_scope_query_ignores_session_and_caller_scope() -> None:
    sql, params = build_document_scope_query(
        user_id="u1",
        kb_id="kb1",
        doc_ids=[],
        temp_doc_ids=["temporary"],
        session_id="session-1",
    )

    normalized = " ".join(sql.split()).lower()
    assert "b.is_temporary" in normalized
    assert "b.session_id = %s" not in normalized
    assert "b.user_id = %s" not in normalized
    assert "b.kb_id = %s" not in normalized
    assert params == [["temporary"]]


def test_document_meta_query_uses_only_doc_ids_and_selects_one_binding() -> None:
    sql, params = build_document_meta_query(
        user_id="u1",
        kb_id="kb1",
        doc_ids=["d1", "shared"],
        temp_doc_ids=["t1"],
        session_id="session-1",
    )

    normalized = " ".join(sql.split()).lower()
    assert "b.user_id = %s" not in normalized
    assert "b.kb_id = %s" not in normalized
    assert "not b.is_temporary" not in normalized
    assert "d.doc_id::text = any(%s)" in normalized
    assert "left join lateral" in normalized
    assert "limit 1" in normalized
    assert params == [["d1", "shared", "t1"]]


def test_node_query_uses_only_doc_id() -> None:
    captured: dict[str, object] = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(self, sql: str, params: list[object]) -> None:
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self) -> list[object]:
            return []

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    class Transaction:
        def __enter__(self) -> Connection:
            return Connection()

        def __exit__(self, *_: object) -> None:
            return None

    class Pool:
        def transaction(self, *, read_only: bool) -> Transaction:
            assert read_only is True
            return Transaction()

    repository = RetrievalRepository(Pool())

    assert (
        repository.fetch_nodes(
            user_id="u1",
            kb_id="kb1",
            doc_id="d1",
            session_id="another-session",
        )
        == []
    )
    normalized = " ".join(str(captured["sql"]).split()).lower()
    assert "session_id" not in normalized
    assert "user_id" not in normalized
    assert "kb_id" not in normalized
    assert "document_bindings" not in normalized
    assert captured["params"] == ["d1"]


def test_document_raw_queries_use_doc_id_and_select_one_binding() -> None:
    document_query, pages_query, nodes_query = build_document_raw_queries()

    normalized = " ".join(document_query.split()).lower()
    assert "b.user_id = %s" not in normalized
    assert "b.kb_id = %s" not in normalized
    assert "d.doc_id::text = %s" in normalized
    assert "left join lateral" in normalized
    assert "binding.user_id" in normalized
    assert "binding.kb_id" in normalized
    assert "limit 1" in normalized
    assert "order by page_number" in pages_query.lower()
    assert "sibling_order" in nodes_query.lower()


def test_document_raw_state_query_uses_only_doc_id() -> None:
    captured: dict[str, object] = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(self, sql: str, params: list[object]) -> None:
            captured["sql"] = sql
            captured["params"] = params

        def fetchone(self):
            return None

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    class Transaction:
        def __enter__(self) -> Connection:
            return Connection()

        def __exit__(self, *_: object) -> None:
            return None

    class Pool:
        def transaction(self, *, read_only: bool) -> Transaction:
            assert read_only is True
            return Transaction()

    repository = RetrievalRepository(Pool())

    assert repository.fetch_document_raw_state(user_id="u1", kb_id="kb1", doc_id="d1") is None
    normalized = " ".join(str(captured["sql"]).split()).lower()
    assert "user_id" not in normalized
    assert "kb_id" not in normalized
    assert "document_bindings" not in normalized
    assert captured["params"] == ["d1"]
