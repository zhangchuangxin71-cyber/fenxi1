import httpx
import pytest

from app.rag.remote_retriever import calculate_max_return_tokens, retrieve


def test_calculate_max_return_tokens_matches_chat_service_policy():
    assert calculate_max_return_tokens("普通问题", document_count=2) == 8192
    assert calculate_max_return_tokens("普通问题", document_count=10) == 16384
    assert calculate_max_return_tokens("请详细总结这些文档", document_count=2) == 32768


@pytest.mark.asyncio
async def test_remote_retrieve_posts_expected_payload_and_maps_chunks(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "chunks": [
                    {
                        "chunk_id": "doc-1:page:1",
                        "document_id": "doc-1",
                        "document_name": "demo.md",
                        "path": "page:1",
                        "content": "hello from remote rag",
                        "score": 42.0,
                        "source_type": "page",
                    }
                ],
                "usage": {"candidate_count": 1, "returned_count": 1, "latency_ms": 3},
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout, trust_env):
            captured["timeout"] = timeout
            captured["trust_env"] = trust_env

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    chunks = await retrieve(
        base_url="http://rag-service:8120",
        kb_id="kb1",
        user_id="u1",
        query="hello",
        session_id="s1",
        doc_ids=["doc-1"],
        temp_doc_ids=["tmp-1"],
        top_k=3,
        search_mode="hybrid",
        ensure_document_coverage=True,
        timeout_seconds=9,
    )

    assert captured["url"] == "http://rag-service:8120/rag/v1/retrieve"
    assert captured["headers"] == {}
    assert captured["json"] == {
        "user_id": "u1",
        "kb_id": "kb1",
        "query": "hello",
        "session_id": "s1",
        "doc_ids": ["doc-1"],
        "temp_doc_ids": ["tmp-1"],
        "top_k": 3,
        "max_return_tokens": 8192,
        "search_mode": "hybrid",
        "options": {
            "include_document_meta": False,
            "include_debug": False,
            "ensure_document_coverage": True,
        },
    }
    assert captured["timeout"] == 9
    assert captured["trust_env"] is False
    assert len(chunks) == 1
    assert chunks[0].chunk_id == "doc-1:page:1"
    assert chunks[0].content == "hello from remote rag"


@pytest.mark.asyncio
async def test_remote_retrieve_keeps_new_contract_scope_chunk_without_single_document_id(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "chunks": [
                    {
                        "chunk_id": "scope:metadata:1",
                        "document_id": None,
                        "document_ids": ["doc-1", "doc-2"],
                        "document_name": None,
                        "path": "request_scope",
                        "content": "可访问两篇文档。",
                        "source_type": "scope_metadata",
                    }
                ],
                "usage": {"candidate_count": 1, "returned_count": 1, "latency_ms": 1},
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout, trust_env):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, json, headers):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    chunks = await retrieve(
        base_url="http://rag-service:8120",
        kb_id="kb1",
        user_id="u1",
        query="你能看到哪些文档",
        session_id="s1",
        doc_ids=["doc-1", "doc-2"],
    )

    assert len(chunks) == 1
    assert chunks[0].chunk_id == "scope:metadata:1"
    assert chunks[0].document_id == ""
    assert chunks[0].document_name == "请求范围"
    assert chunks[0].content == "可访问两篇文档。"


@pytest.mark.asyncio
async def test_remote_retrieve_never_sends_obsolete_internal_authorization(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"chunks": [], "usage": {"candidate_count": 0, "returned_count": 0, "latency_ms": 1}}

    class FakeAsyncClient:
        def __init__(self, *, timeout, trust_env):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, json, headers):
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    chunks = await retrieve(
        base_url="http://rag-service:8120/",
        kb_id="kb1",
        user_id="u1",
        query="hello",
        session_id="s1",
        timeout_seconds=5,
    )

    assert chunks == []
    assert captured["headers"] == {}
