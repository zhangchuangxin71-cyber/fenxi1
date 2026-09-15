from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.storage.postgres_store import PostgresWorkspaceStore, is_complete_raw_mineru


def test_raw_mineru_completeness_requires_all_three_typed_fields() -> None:
    complete = {
        "md_content": "# title",
        "content_list": [],
        "middle_json": {},
    }

    assert is_complete_raw_mineru(complete) is True
    assert is_complete_raw_mineru(None) is False
    assert is_complete_raw_mineru({"md_content": "", "content_list": []}) is False
    assert is_complete_raw_mineru({**complete, "content_list": {}}) is False
    assert is_complete_raw_mineru({**complete, "middle_json": []}) is False


def test_patch_raw_mineru_updates_only_nested_raw_field() -> None:
    class Cursor:
        rowcount = 1

        def __init__(self) -> None:
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, sql, params):
            self.statements.append((sql, params))

    class Connection:
        def __init__(self) -> None:
            self.cursor_value = Cursor()
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return self.cursor_value

        def commit(self):
            self.committed = True

    store = PostgresWorkspaceStore.__new__(PostgresWorkspaceStore)
    connection = Connection()
    store._connect = lambda: connection
    raw_mineru = {"md_content": "# repaired", "content_list": [], "middle_json": {}}

    store.patch_raw_mineru("doc-1", raw_mineru)

    raw_sql, raw_params = connection.cursor_value.statements[0]
    repair_sql, repair_params = connection.cursor_value.statements[1]
    assert "jsonb_set" in raw_sql
    assert "updated_at" not in raw_sql
    assert "doc_description" not in raw_sql
    assert raw_params[1] == "doc-1"
    assert "raw_mineru_repair" in repair_sql
    assert "SET updated_at" not in repair_sql
    assert repair_params == ("doc-1", None, None)
    assert connection.committed is True

    owned_connection = Connection()
    store._connect = lambda: owned_connection
    store.patch_raw_mineru(
        "doc-1",
        raw_mineru,
        repair_id="repair-1",
    )

    owned_raw_sql, owned_raw_params = owned_connection.cursor_value.statements[0]
    owned_repair_sql, owned_repair_params = owned_connection.cursor_value.statements[1]
    assert "WHERE doc_id = %s" in owned_raw_sql
    ownership_predicate = "AND (%s IS NULL OR raw -> 'raw_mineru_repair' ->> 'repair_id' = %s)"
    assert ownership_predicate in owned_raw_sql
    assert owned_raw_sql.index(ownership_predicate) > owned_raw_sql.index("WHERE doc_id = %s")
    assert owned_raw_params[2] == "repair-1"
    assert "repair_id" in owned_repair_sql
    assert owned_repair_params == ("doc-1", "repair-1", "repair-1")
