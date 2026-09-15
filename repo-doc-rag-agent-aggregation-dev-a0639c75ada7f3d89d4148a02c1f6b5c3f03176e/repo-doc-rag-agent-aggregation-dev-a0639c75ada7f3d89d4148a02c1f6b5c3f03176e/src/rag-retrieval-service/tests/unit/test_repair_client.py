from __future__ import annotations

import httpx
import pytest

from app.documents.repair_client import RawRepairClient, RawRepairClientError


@pytest.mark.asyncio
async def test_repair_client_submits_to_internal_ingestion_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ingestion/v1/internal/documents/raw/repair"
        assert request.method == "POST"
        return httpx.Response(
            202,
            json={
                "code": 202,
                "message": "accepted",
                "data": {"repair_id": "repair-1", "task_id": "task-1", "status": "queued"},
            },
        )

    http_client = httpx.AsyncClient(
        base_url="http://ingestion", transport=httpx.MockTransport(handler)
    )
    client = RawRepairClient(base_url="http://unused", timeout_seconds=1, client=http_client)

    result = await client.submit(user_id="u", kb_id="kb", doc_id="d1")

    assert result["repair_id"] == "repair-1"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_repair_client_preserves_stable_upstream_error() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "code": 422,
                "message": "missing source",
                "data": {"error_code": "RAW_MINERU_SOURCE_MISSING", "retryable": False},
            },
        )

    http_client = httpx.AsyncClient(
        base_url="http://ingestion", transport=httpx.MockTransport(handler)
    )
    client = RawRepairClient(base_url="http://unused", timeout_seconds=1, client=http_client)

    with pytest.raises(RawRepairClientError) as error:
        await client.submit(user_id="u", kb_id="kb", doc_id="d1")

    assert error.value.status_code == 422
    assert error.value.code == "RAW_MINERU_SOURCE_MISSING"
    assert error.value.retryable is False
    await http_client.aclose()


@pytest.mark.asyncio
async def test_repair_client_accepts_completed_race_without_repair_id() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "code": 202,
                "message": "already completed",
                "data": {"repair_id": None, "task_id": None, "status": "completed"},
            },
        )

    http_client = httpx.AsyncClient(
        base_url="http://ingestion", transport=httpx.MockTransport(handler)
    )
    client = RawRepairClient(base_url="http://unused", timeout_seconds=1, client=http_client)

    result = await client.submit(user_id="u", kb_id="kb", doc_id="d1")

    assert result["status"] == "completed"
    assert result["repair_id"] is None
    await http_client.aclose()
