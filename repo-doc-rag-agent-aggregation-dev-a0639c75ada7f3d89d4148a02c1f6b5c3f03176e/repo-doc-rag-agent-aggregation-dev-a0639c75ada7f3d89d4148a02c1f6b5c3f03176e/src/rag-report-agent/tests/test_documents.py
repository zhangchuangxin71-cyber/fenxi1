from app.db.documents import load_doc_meta


def test_load_doc_meta_filters_by_kb_without_user_binding(monkeypatch):
    captured = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self):
            return [("doc_a", "示例文档.pdf", "示例描述", "2026-01-01")]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr("app.db.documents.psycopg.connect", lambda dsn: FakeConnection())

    metas = load_doc_meta("kb-demo", ["doc_a"], "user-demo")

    assert metas["doc_a"]["doc_name"] == "示例文档.pdf"
    assert "b.kb_id = %s" in captured["sql"]
    assert "b.user_id = %s" not in captured["sql"]
    assert captured["params"] == (["doc_a"], "kb-demo")
