from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from types import MethodType
from types import SimpleNamespace

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


def _cleanup(doc_id: str, persisted_doc_id: str | None = None) -> None:
    conn = _connect_or_skip()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                delete from ingestion_task_events
                where task_id in (
                    select task_id from ingestion_tasks where doc_id = %s
                )
                """,
                (doc_id,),
            )
            cur.execute("delete from ingestion_tasks where doc_id = %s", (doc_id,))
            if persisted_doc_id:
                cur.execute("delete from documents where doc_id = %s", (persisted_doc_id,))
        conn.commit()
    finally:
        conn.close()


def test_ingest_request_accepts_doc_file_type(monkeypatch) -> None:
    monkeypatch.setenv("POSTGRES_DSN", os.getenv("POSTGRES_DSN") or "postgresql://unused")

    from app.ingestion.container import IngestRequest

    request = IngestRequest(
        user_id="user-1",
        kb_id="kb-1",
        oss_key="uploads/legacy.doc",
        file_name="legacy.doc",
        file_type="doc",
    )

    assert request.file_type == "doc"
    assert request.iter_documents()[0].file_type == "doc"


def test_index_documents_preserves_original_file_metadata(monkeypatch, tmp_path: Path) -> None:
    """Temporary parser inputs must not overwrite the stored original file type."""
    import asyncio

    from app.ingestion import agent_adapter

    original = tmp_path / "02.html"
    original.write_text("<html><body><h1>红楼梦</h1></body></html>", encoding="utf-8")
    prepared = tmp_path / "ingest_text_demo.md"
    prepared.write_text("# 红楼梦\n\n正文", encoding="utf-8")

    def fake_parse_document_to_structure(file_path: str | Path, **_: object) -> dict:
        assert Path(file_path) == prepared
        return {
            "id": "placeholder",
            "doc_id": "placeholder",
            "type": "md",
            "path": str(prepared),
            "doc_name": prepared.name,
            "doc_description": "converted markdown",
            "page_count": 1,
            "structure": [],
            "pages": [{"page": 1, "content": "正文"}],
        }

    class FakeClient:
        model = ""
        retrieve_model = ""

        def __init__(self) -> None:
            self.documents: dict[str, dict] = {}
            self.saved: list[str] = []

        def _save_doc(self, doc_id: str) -> None:
            self.saved.append(doc_id)

    monkeypatch.setattr(agent_adapter, "parse_document_to_structure", fake_parse_document_to_structure)

    adapter = agent_adapter.DocumentAssistantAdapter()
    client = FakeClient()
    doc_ids, failed_docs = asyncio.run(
        adapter.index_documents_async(
            client,
            [prepared],
            target_doc_ids=["doc-html"],
            source_metadata=[
                {
                    "doc_name": original.name,
                    "doc_type": "html",
                    "path": str(original),
                    "file_oss_key": "fixtures/02.html",
                }
            ],
            options=SimpleNamespace(summary_enabled=False, summary_concurrency=1),
            runtime=SimpleNamespace(stats={}),
        )
    )

    assert failed_docs == []
    assert doc_ids == ["doc-html"]
    payload = client.documents["doc-html"]
    assert payload["doc_name"] == "02.html"
    assert payload["type"] == "html"
    assert payload["path"] == str(original)
    assert payload["file_oss_key"] == "fixtures/02.html"
    assert client.saved == ["doc-html"]


@pytest.mark.integration
def test_ingest_txt_document_via_fastapi_service(tmp_path: Path) -> None:
    """The API ingestion service should accept a request and create a task.

    This validates the FastAPI request model and the service submission path.
    We deliberately prevent background workers from starting here, because the
    full parse-and-persist path is covered by test_ingestion_db.py and in-process
    TestClient workers can be cancelled when the test event loop shuts down.
    """
    import asyncio

    from app.ingestion import container

    source = tmp_path / "pytest-story.txt"
    source.write_text(
        "第一章 测试文档\n\n这是一份用于入库接口测试的短文本。\n\n第二章 检索说明\n\n它应该能够创建入库任务。",
        encoding="utf-8",
    )

    service = container.get_ingestion_service()
    original_ensure_workers = service._ensure_workers
    original_resolve_source = service._stable_document_id_and_local_path

    async def _no_worker_start(self):
        return None

    def _resolve_fixture_oss_key(oss_key, file_name, submit_temp_dir=None):
        import hashlib

        sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sha256:{sha256}")), source

    service._ensure_workers = MethodType(_no_worker_start, service)
    service._stable_document_id_and_local_path = _resolve_fixture_oss_key
    user_id = f"pytest-user-{uuid.uuid4().hex}"
    doc_id = ""

    try:
        request = container.IngestRequest(
            user_id=user_id,
            kb_id="pytest-kb",
            oss_key=f"pytest-fixtures/{source.name}",
            file_name=source.name,
            file_type="txt",
            config=container.IngestConfig(
                summary_enabled=False,
                enable_ocr=False,
                extract_tables=False,
            ),
        )
        doc_id = asyncio.run(service._resolve_submit_doc_ids(request))[0]
        _cleanup(doc_id)
        result = asyncio.run(service.submit_ingestion(request))
        doc_id = result.doc_id
        uuid.UUID(doc_id)
        assert result.doc_id == doc_id
        assert result.doc_ids == [doc_id]
        assert result.user_id == user_id
        assert result.status == "queued"
        assert result.task_id
        assert result.estimated_seconds >= 0

        status = asyncio.run(service.get_status_by_task_id(result.task_id))
        assert status.doc_id == doc_id
        assert status.kb_id == "pytest-kb"
        assert status.status == container.TaskStatus.QUEUED

        doc_status = asyncio.run(service.get_status_by_doc_id(doc_id, user_id=user_id, kb_id="pytest-kb"))
        assert doc_status.task_id == result.task_id
        with pytest.raises(container.NotFoundError):
            asyncio.run(service.get_status_by_doc_id(doc_id, user_id="other-user", kb_id="pytest-kb"))

        conn = _connect_or_skip()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select doc_id, user_id, kb_id, status, current_step
                    from ingestion_tasks
                    where doc_id = %s
                    """,
                    (doc_id,),
                )
                row = cur.fetchone()
                assert row is not None
                assert row[0] == doc_id
                assert row[1] == user_id
                assert row[2] == "pytest-kb"
                assert row[3] == "queued"
        finally:
            conn.close()
    finally:
        service._ensure_workers = original_ensure_workers
        service._stable_document_id_and_local_path = original_resolve_source
        _cleanup(doc_id)
