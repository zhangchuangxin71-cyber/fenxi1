from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace


def test_index_documents_async_respects_index_concurrency(monkeypatch, tmp_path: Path) -> None:
    from app.ingestion.agent_adapter import DocumentAssistantAdapter

    adapter = DocumentAssistantAdapter()
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def fake_load_or_index_document(**kwargs):
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        file_path = Path(kwargs["file_path"])
        return SimpleNamespace(payload={"doc_id": file_path.stem, "id": file_path.stem})

    monkeypatch.setattr(adapter, "load_or_index_document", fake_load_or_index_document)

    saved: list[str] = []
    client = SimpleNamespace(model="", retrieve_model="", documents={})

    def save_doc(doc_id: str) -> None:
        saved.append(doc_id)

    client._save_doc = save_doc

    files = []
    for name in ["a.md", "b.md", "c.md", "d.md"]:
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        files.append(path)

    options = SimpleNamespace(index_concurrency=2)

    doc_ids, failed_docs = asyncio.run(
        adapter.index_documents_async(
            client,
            files,
            options=options,
            runtime=None,
        )
    )

    assert doc_ids == ["a", "b", "c", "d"]
    assert failed_docs == []
    assert max_active == 2
    assert saved == ["a", "b", "c", "d"]

def test_load_or_index_document_passes_summary_rate_limit(monkeypatch, tmp_path: Path) -> None:
    from app.ingestion import agent_adapter
    from app.ingestion.agent_adapter import DocumentAssistantAdapter

    adapter = DocumentAssistantAdapter()
    captured: dict[str, object] = {}

    def fake_parse_document_to_structure(file_path, *, runtime_overrides, pageindex_overrides, generate_summary, generate_doc_description):
        captured["runtime_overrides"] = dict(runtime_overrides)
        return {"doc_id": "doc-rate", "id": "doc-rate", "structure": []}

    monkeypatch.setattr(agent_adapter, "parse_document_to_structure", fake_parse_document_to_structure)

    source = tmp_path / "a.md"
    source.write_text("# A", encoding="utf-8")
    options = agent_adapter.StandaloneIndexingOptions(
        simple_index_page_threshold=80,
        long_pdf_hard_threshold=180,
        simple_index_chunk_pages=10,
        hybrid_index_chunk_pages=6,
        chunk_overlap_ratio=0.15,
        long_pdf_mode="auto",
        complexity_sample_pages=12,
        index_concurrency=1,
        summary_enabled=True,
        table_parse_mode="auto",
        node_max_tokens=512,
        max_tree_depth=3,
        enable_ocr=False,
        summary_concurrency=2,
        summary_rate_limit_per_sec=1.5,
    )

    indexed = asyncio.run(
        adapter.load_or_index_document(
            model="m",
            retrieve_model="r",
            file_path=source,
            options=options,
            runtime=None,
        )
    )

    assert indexed is not None
    assert captured["runtime_overrides"]["summary_rate_limit_per_sec"] == 1.5
    assert captured["runtime_overrides"]["summary_concurrency"] == 2


def test_doc_source_metadata_is_stored_as_docx() -> None:
    from app.ingestion.agent_adapter import DocumentAssistantAdapter

    payload = {
        "id": "doc-1",
        "doc_id": "doc-1",
        "type": "docx",
        "doc_name": "converted.docx",
        "path": "/tmp/converted.docx",
    }

    normalized = DocumentAssistantAdapter.apply_source_metadata(
        payload,
        {
            "doc_name": "legacy.doc",
            "doc_type": "doc",
            "path": "/tmp/legacy.doc",
            "file_oss_key": "uploads/legacy.doc",
        },
    )

    assert normalized["doc_name"] == "legacy.doc"
    assert normalized["type"] == "docx"
    assert normalized["path"] == "/tmp/legacy.doc"
    assert normalized["file_oss_key"] == "uploads/legacy.doc"
