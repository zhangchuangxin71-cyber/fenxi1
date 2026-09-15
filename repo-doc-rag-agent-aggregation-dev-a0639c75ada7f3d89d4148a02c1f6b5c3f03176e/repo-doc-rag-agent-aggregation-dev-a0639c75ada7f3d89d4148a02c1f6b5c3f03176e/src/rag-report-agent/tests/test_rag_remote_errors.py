import httpx
import pytest

from app.rag.remote_retriever import retrieve
from app.utils.errors import FrontendServiceError
from app.utils.safety import safe_run_graph


@pytest.mark.asyncio
async def test_remote_retrieve_maps_rate_limit_to_frontend_429(monkeypatch):
    class FakeResponse:
        status_code = 429

        def json(self):
            return {"error": {"code": "RATE_LIMITED", "message": "rate limit exceeded", "request_id": "req-upstream"}}

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

    with pytest.raises(FrontendServiceError) as exc_info:
        await retrieve(
            base_url="http://rag-service",
            kb_id="kb1",
            user_id="u1",
            query="hello",
            session_id="s1",
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.error_type == "rag_retrieval_rate_limited"
    assert "req-upstream" in exc_info.value.internal_message


@pytest.mark.asyncio
async def test_remote_retrieve_maps_auth_failure_to_frontend_503(monkeypatch):
    class FakeResponse:
        status_code = 401

        def json(self):
            return {"error": {"code": "UNAUTHORIZED", "message": "invalid key", "request_id": "req-auth"}}

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

    with pytest.raises(FrontendServiceError) as exc_info:
        await retrieve(
            base_url="http://rag-service",
            kb_id="kb1",
            user_id="u1",
            query="hello",
            session_id="s1",
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.error_type == "rag_retrieval_unavailable"
    assert "UNAUTHORIZED" in exc_info.value.internal_message


@pytest.mark.asyncio
async def test_remote_retrieve_maps_connection_error_to_frontend_503(monkeypatch):
    class FakeAsyncClient:
        def __init__(self, *, timeout, trust_env):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, json, headers):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(FrontendServiceError) as exc_info:
        await retrieve(
            base_url="http://127.0.0.1:9999",
            kb_id="kb1",
            user_id="u1",
            query="hello",
            session_id="s1",
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.error_type == "rag_retrieval_unavailable"
    assert "connection refused" in exc_info.value.internal_message


class FakeEmitter:
    def __init__(self):
        self.errors = []

    async def emit_error(self, code, err_type, message, fatal=True):
        self.errors.append({"code": code, "type": err_type, "message": message, "fatal": fatal})


class FailingGraph:
    async def ainvoke(self, state, config):
        raise FrontendServiceError(
            status_code=503,
            error_type="rag_retrieval_unavailable",
            public_message="知识库检索服务暂时不可用，请稍后再试。",
            internal_message="remote retrieval auth failed: req-auth",
        )


@pytest.mark.asyncio
async def test_safe_run_graph_emits_frontend_service_error() -> None:
    emitter = FakeEmitter()

    result = await safe_run_graph(FailingGraph(), {}, emitter)

    assert result == {}
    assert emitter.errors == [
        {
            "code": 503,
            "type": "rag_retrieval_unavailable",
            "message": "知识库检索服务暂时不可用，请稍后再试。",
            "fatal": True,
        }
    ]
