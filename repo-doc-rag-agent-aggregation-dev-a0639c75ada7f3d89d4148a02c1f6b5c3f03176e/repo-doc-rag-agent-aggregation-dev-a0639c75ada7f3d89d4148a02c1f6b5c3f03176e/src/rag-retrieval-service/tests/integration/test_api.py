from __future__ import annotations

import httpx
import pytest

from app.api.app import create_app
from app.api.schemas import (
    CoverageSummary,
    DocumentMeta,
    DocumentMetaResponse,
    DocumentRawRepairAccepted,
    DocumentRawResponse,
    DocumentRawStatusResponse,
    DocumentRouteGroupResult,
    DocumentRouteResponse,
    DocumentRouteUsage,
    MinerURawResult,
    RetrieveResponse,
    RetrieveUsage,
)
from app.config.settings import Settings
from app.security.admission import RetrievalAdmissionController
from app.security.rate_limit import TokenBucketLimiter


class Engine:
    def __init__(self) -> None:
        self.calls = 0

    async def retrieve(self, request):
        self.calls += 1
        return RetrieveResponse(
            chunks=[],
            warnings=[],
            coverage=CoverageSummary(complete=True),
            usage=RetrieveUsage(tokenizer="test", latency_ms=1, actual_mode="semantic"),
        )


class Documents:
    async def meta(self, request):
        return DocumentMetaResponse(
            documents=[
                DocumentMeta(
                    doc_id=request.doc_ids[0],
                    doc_name="文档",
                    status="ready",
                    kb_id=request.kb_id,
                    user_id=request.user_id,
                )
            ]
        )

    async def raw(self, request):
        return DocumentRawResponse(
            doc_id=request.doc_id,
            doc_name="文档",
            doc_type="pdf",
            status="ready",
            raw_mineru=MinerURawResult(),
        )


class DocumentRoutes:
    def __init__(self) -> None:
        self.calls = 0

    async def route(self, request):
        self.calls += 1
        return DocumentRouteResponse(
            request_id="route_test",
            groups=[
                DocumentRouteGroupResult(
                    index=0,
                    accept_doc_ids=[request.doc_ids[0]],
                    decision_source="llm",
                )
            ],
            usage=DocumentRouteUsage(candidate_document_count=len(request.doc_ids)),
        )


class Container:
    def __init__(self) -> None:
        self.settings = Settings(
            _env_file=None,
            rag_rate_limit_enabled=True,
            rag_rate_limit_per_minute=60,
            rag_rate_limit_burst=1,
        )
        self.limiter = TokenBucketLimiter(enabled=True, per_minute=60, burst=1)
        self.admission = RetrievalAdmissionController(
            max_active=1, max_queued=1, wait_timeout_seconds=1
        )
        self.engine = Engine()
        self.documents = Documents()
        self.document_routes = DocumentRoutes()
        self.started = 0
        self.closed = 0

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1

    async def ready(self) -> None:
        return None


@pytest.mark.asyncio
async def test_api_has_no_auth_and_rate_limits_external_requests_before_graph() -> None:
    container = Container()
    app = create_app(container=container)
    payload = {"user_id": "u1", "kb_id": "kb", "query": "有哪些文档"}

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/rag/v1/retrieve", json=payload)
            second = await client.post("/rag/v1/retrieve", json=payload)

    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert container.engine.calls == 1
    assert container.started == 1
    assert container.closed == 1


@pytest.mark.asyncio
async def test_health_and_readiness_contracts() -> None:
    container = Container()
    app = create_app(container=container)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            health = await client.get("/healthz")
            ready = await client.get("/readyz")

    assert health.json()["ok"] is True
    assert ready.json() == {"ok": True, "service": "rag-retrieval-service", "database": "ok"}


@pytest.mark.asyncio
async def test_readyz_keeps_legacy_database_unavailable_error() -> None:
    container = Container()

    async def fail_ready() -> None:
        raise RuntimeError("database down")

    container.ready = fail_ready
    app = create_app(container=container)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "DATABASE_UNAVAILABLE",
        "message": "database is not ready",
        "retryable": True,
        "details": {},
    }


def test_openapi_contract_has_no_auth_or_sse_surface() -> None:
    app = create_app(container=Container())

    schema = app.openapi()

    assert set(schema["paths"]) == {
        "/healthz",
        "/readyz",
        "/rag/v1/retrieve",
        "/rag/v1/documents/meta",
        "/rag/v1/documents/route",
        "/rag/v1/documents/raw",
        "/rag/v1/documents/raw/status",
    }
    assert "securitySchemes" not in schema.get("components", {})
    serialized = str(schema).casefold()
    assert "text/event-stream" not in serialized
    assert "authorization" not in serialized

    query_schema = schema["components"]["schemas"]["RetrieveRequest"]["properties"]["query"]
    query_variants = query_schema["anyOf"]
    assert {variant["type"] for variant in query_variants} == {"string", "array"}
    array_schema = next(variant for variant in query_variants if variant["type"] == "array")
    assert array_schema["items"]["type"] == "string"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload", "expected_key"),
    [
        (
            "/rag/v1/documents/meta",
            {"user_id": "u1", "kb_id": "kb", "doc_ids": ["d1"]},
            "documents",
        ),
        (
            "/rag/v1/documents/raw",
            {"user_id": "u1", "kb_id": "kb", "doc_id": "d1"},
            "raw_mineru",
        ),
    ],
)
async def test_document_compatibility_endpoints_have_no_auth(
    path: str, payload: dict, expected_key: str
) -> None:
    container = Container()
    app = create_app(container=container)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(path, json=payload)

    assert response.status_code == 200
    assert expected_key in response.json()


@pytest.mark.asyncio
async def test_document_route_endpoint_uses_group_contract() -> None:
    container = Container()
    app = create_app(container=container)
    payload = {
        "user_id": "u1",
        "kb_id": "kb",
        "doc_ids": ["d1", "d2"],
        "criteria": [
            {
                "queries": ["哪些文档适合写新能源汽车产业趋势？"],
                "target_docs_description": "新能源汽车产业趋势材料",
                "target_docs_keywords": ["新能源汽车", "产业趋势"],
            }
        ],
    }

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/rag/v1/documents/route", json=payload)

    assert response.status_code == 200
    assert response.json()["groups"][0]["accept_doc_ids"] == ["d1"]
    assert container.document_routes.calls == 1


@pytest.mark.asyncio
async def test_only_retrieve_uses_admission_controller() -> None:
    container = Container()
    calls: list[str] = []
    underlying = container.admission

    class Admission:
        def admit(self, user_id: str):
            calls.append(user_id)
            return underlying.admit(user_id)

    container.admission = Admission()
    app = create_app(container=container)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            retrieve = await client.post(
                "/rag/v1/retrieve",
                json={"user_id": "retrieve-user", "kb_id": "kb", "query": "问题"},
            )
            meta = await client.post(
                "/rag/v1/documents/meta",
                json={"user_id": "meta-user", "kb_id": "kb", "doc_ids": ["d1"]},
            )
            raw = await client.post(
                "/rag/v1/documents/raw",
                json={"user_id": "raw-user", "kb_id": "kb", "doc_id": "d1"},
            )
            health = await client.get("/healthz")
            ready = await client.get("/readyz")

    assert retrieve.status_code == 200
    assert meta.status_code == 200
    assert raw.status_code == 200
    assert health.status_code == 200
    assert ready.status_code == 200
    assert calls == ["retrieve-user"]


@pytest.mark.asyncio
async def test_raw_missing_result_returns_202_and_status_endpoint_is_read_only() -> None:
    class RepairDocuments(Documents):
        async def raw(self, request):
            return DocumentRawRepairAccepted(
                doc_id=request.doc_id,
                repair_id="repair-1",
                task_id="task-1",
                status="queued",
                status_url=("/rag/v1/documents/raw/status?user_id=u1&kb_id=kb&doc_id=d1"),
            )

        async def raw_status(self, request):
            return DocumentRawStatusResponse(
                doc_id=request.doc_id,
                repair_id="repair-1",
                status="processing",
            )

    container = Container()
    container.documents = RepairDocuments()
    app = create_app(container=container)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            accepted = await client.post(
                "/rag/v1/documents/raw",
                json={"user_id": "u1", "kb_id": "kb", "doc_id": "d1"},
            )
            status = await client.get(
                "/rag/v1/documents/raw/status",
                params={"user_id": "u2", "kb_id": "kb", "doc_id": "d1"},
            )

    assert accepted.status_code == 202
    assert accepted.json()["repair_id"] == "repair-1"
    assert status.status_code == 200
    assert status.json()["status"] == "processing"
