import pytest


@pytest.mark.asyncio
async def test_stub_uses_remote_backend_when_configured(monkeypatch):
    from app.config import settings
    from app.rag import stub
    from app.rag.types import ProvidedChunkInput

    monkeypatch.setattr(settings, "rag_retrieval_backend", "remote")
    monkeypatch.setattr(settings, "rag_retrieval_service_url", "http://127.0.0.1:8120")
    monkeypatch.setattr(settings, "rag_retrieval_timeout_seconds", 7)

    captured = {}

    async def fake_remote_retrieve(**kwargs):
        captured.update(kwargs)
        return [
            ProvidedChunkInput(
                chunk_id="remote:1",
                document_id="doc-remote",
                document_name="remote.md",
                path="page:1",
                content="remote content",
            )
        ]

    monkeypatch.setattr("app.rag.stub.remote_retrieve", fake_remote_retrieve)

    chunks = await stub.retrieve(
        kb_id="kb1",
        user_id="u1",
        query="hello",
        session_id="s1",
        top_k=2,
        ensure_document_coverage=True,
    )

    assert chunks[0].chunk_id == "remote:1"
    assert captured["base_url"] == "http://127.0.0.1:8120"
    assert "api_key" not in captured
    assert captured["timeout_seconds"] == 7
    assert captured["kb_id"] == "kb1"
    assert captured["user_id"] == "u1"
    assert captured["ensure_document_coverage"] is True


@pytest.mark.asyncio
async def test_stub_keeps_explicit_postgres_backend_available(monkeypatch):
    from app.config import settings
    from app.rag import stub
    from app.rag.types import ProvidedChunkInput

    monkeypatch.setattr(settings, "rag_retrieval_backend", "postgres")

    async def fake_postgres_retrieve(**kwargs):
        return [
            ProvidedChunkInput(
                chunk_id="local:1",
                document_id="doc-local",
                document_name="local.md",
                path="page:1",
                content="local content",
            )
        ]

    monkeypatch.setattr("app.rag.stub.postgres_retrieve", fake_postgres_retrieve)

    chunks = await stub.retrieve(kb_id="kb1", user_id="u1", query="hello", session_id="s1")

    assert chunks[0].chunk_id == "local:1"


def test_settings_default_to_independent_remote_retrieval():
    from app.config import Settings

    config = Settings(_env_file=None)

    assert config.rag_retrieval_backend == "remote"
