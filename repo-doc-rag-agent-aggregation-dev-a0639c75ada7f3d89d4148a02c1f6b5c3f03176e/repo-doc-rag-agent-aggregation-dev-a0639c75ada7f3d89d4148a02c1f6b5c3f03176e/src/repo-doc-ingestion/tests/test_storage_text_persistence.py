from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.storage.client import PageIndexClient
from app.storage.postgres_store import PostgresWorkspaceStore


class _CaptureStore:
    def __init__(self) -> None:
        self.saved: dict[str, object] | None = None

    def save_doc(self, doc_id: str, doc: dict[str, object]) -> None:
        self.saved = {"doc_id": doc_id, "doc": doc}


def test_postgres_store_never_scopes_documents_by_session() -> None:
    store = PostgresWorkspaceStore(
        dsn="postgresql://unused",
        auto_init_tables=False,
        user_id="user-1",
        kb_id="kb-1",
        session_id="session-1",
        scope_by_session=True,
    )

    sql, params = store._scope_clause(table_alias="b")

    assert "b.user_id = %s" in sql
    assert "b.kb_id = %s" in sql
    assert "session_id" not in sql
    assert params == ["user-1", "kb-1"]


def test_pageindex_client_keeps_node_text_for_store_save() -> None:
    store = _CaptureStore()
    client = PageIndexClient.__new__(PageIndexClient)
    client._store = store
    client.documents = {
        "doc-1": {
            "doc_id": "doc-1",
            "type": "txt",
            "structure": [
                {
                    "node_id": "n1",
                    "title": "节点",
                    "text": "这是应该写入 doc_nodes.text 的正文。",
                }
            ],
            "pages": [{"page": 1, "content": "这是页面正文。"}],
        }
    }

    client._save_doc("doc-1")

    assert store.saved is not None
    saved_doc = store.saved["doc"]
    assert isinstance(saved_doc, dict)
    assert saved_doc["structure"][0]["text"] == "这是应该写入 doc_nodes.text 的正文。"


def test_postgres_store_flattens_node_text_but_slims_raw_payload() -> None:
    doc = {
        "doc_id": "doc-1",
        "type": "txt",
        "session_id": "session-1",
        "is_temporary": True,
        "structure": [
            {
                "node_id": "root",
                "title": "根节点",
                "text": "父节点正文",
                "nodes": [
                    {
                        "node_id": "leaf",
                        "title": "叶子节点",
                        "text": "叶子节点正文",
                    }
                ],
            }
        ],
        "pages": [{"page": 1, "content": "页面正文"}],
        "raw_mineru": {
            "md_content": "# 根节点\n\n页面正文",
            "content_list": [{"type": "text", "text": "页面正文", "page_idx": 0}],
            "middle_json": {"_backend": "pipeline", "_version_name": "3.3.0"},
        },
    }

    node_rows = PostgresWorkspaceStore._flatten_nodes(doc["structure"])
    raw_doc = PostgresWorkspaceStore._slim_raw_doc(doc)

    assert [row["text"] for row in node_rows] == ["父节点正文", "叶子节点正文"]
    assert "session_id" not in raw_doc
    assert "is_temporary" not in raw_doc
    assert "pages" not in raw_doc
    assert "text" not in raw_doc["structure"][0]
    assert "text" not in raw_doc["structure"][0]["nodes"][0]
    assert raw_doc["raw_mineru"] == doc["raw_mineru"]
