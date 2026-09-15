from __future__ import annotations

import pytest

from app.api.schemas import (
    DocumentMetaRequest,
    DocumentRawRepairAccepted,
    DocumentRawRequest,
    DocumentRawResponse,
    DocumentRawStatusRequest,
)
from app.core.errors import ApiError
from app.documents.service import DocumentService


class Repository:
    def __init__(self) -> None:
        self.meta_calls: list[dict[str, object]] = []

    def fetch_document_meta(self, **_: object):
        self.meta_calls.append(_)
        return (
            [
                {
                    "doc_id": "d1",
                    "doc_name": "文档",
                    "doc_type": "pdf",
                    "doc_description": "摘要",
                    "status": "ready",
                    "page_count": 1,
                    "node_count": 1,
                    "created_at": None,
                    "updated_at": None,
                    "kb_id": "kb",
                    "user_id": "u",
                    "is_temporary": False,
                    "bound_at": None,
                }
            ],
            ["missing"],
        )

    def fetch_document_raw(self, **_: object):
        return {
            "doc_id": "d1",
            "doc_name": "文档",
            "doc_type": "pdf",
            "doc_description": "摘要",
            "status": "ready",
            "page_count": 1,
            "line_count": 3,
            "node_count": 1,
            "is_temporary": False,
            "pages": [{"page": 1, "content": "原文"}],
            "structure": [],
            "raw_mineru": {"md_content": "原文", "content_list": [], "middle_json": {}},
            "raw_mineru_repair": None,
            "file_oss_key": "docs/d1.pdf",
            "_binding_user_id": "document-owner",
            "_binding_kb_id": "document-kb",
        }

    def fetch_document_raw_state(self, **_: object):
        return {
            "doc_id": "d1",
            "raw_mineru": None,
            "raw_mineru_repair": {"status": "processing", "repair_id": "repair-1"},
            "file_oss_key": "docs/d1.pdf",
        }


class RepairClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def submit(self, **kwargs: str):
        self.calls.append(kwargs)
        return {"repair_id": "repair-1", "task_id": "task-1", "status": "queued"}


@pytest.mark.asyncio
async def test_document_meta_preserves_legacy_contract_and_missing_switch() -> None:
    repository = Repository()
    service = DocumentService(repository=repository)

    response = await service.meta(
        DocumentMetaRequest(
            user_id="u",
            kb_id="kb",
            session_id="session-1",
            doc_ids=["d1", "missing"],
            temp_doc_ids=["temporary"],
            include_missing=False,
        )
    )

    assert response.documents[0].doc_id == "d1"
    assert response.missing_doc_ids == []
    assert repository.meta_calls == [
        {
            "user_id": "u",
            "kb_id": "kb",
            "doc_ids": ["d1", "missing"],
            "temp_doc_ids": ["temporary"],
            "session_id": "session-1",
        }
    ]


@pytest.mark.asyncio
async def test_document_raw_preserves_pages_and_mineru_payload() -> None:
    service = DocumentService(repository=Repository())

    response = await service.raw(DocumentRawRequest(user_id="u", kb_id="kb", doc_id="d1"))

    assert response.schema_version == "1.0"
    assert response.pages[0].content == "原文"
    assert response.raw_mineru.md_content == "原文"


@pytest.mark.asyncio
async def test_document_raw_returns_404_when_document_is_outside_scope() -> None:
    repository = Repository()
    repository.fetch_document_raw = lambda **_: None
    service = DocumentService(repository=repository)

    with pytest.raises(ApiError) as error:
        await service.raw(DocumentRawRequest(user_id="u", kb_id="kb", doc_id="missing"))

    assert error.value.status_code == 404
    assert error.value.code == "DOCUMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_document_raw_missing_result_submits_async_repair() -> None:
    repository = Repository()
    document = repository.fetch_document_raw()
    document["raw_mineru"] = None
    repository.fetch_document_raw = lambda **_: document
    client = RepairClient()
    service = DocumentService(repository=repository, repair_client=client)

    response = await service.raw(
        DocumentRawRequest(user_id="unrelated-caller", kb_id="unrelated-kb", doc_id="d1")
    )

    assert isinstance(response, DocumentRawRepairAccepted)
    assert response.status == "queued"
    assert response.status_url.endswith(
        "/rag/v1/documents/raw/status?user_id=unrelated-caller&kb_id=unrelated-kb&doc_id=d1"
    )
    assert client.calls == [{"user_id": "document-owner", "kb_id": "document-kb", "doc_id": "d1"}]


@pytest.mark.asyncio
async def test_document_raw_incomplete_mapping_submits_repair() -> None:
    repository = Repository()
    document = repository.fetch_document_raw()
    document["raw_mineru"] = {}
    repository.fetch_document_raw = lambda **_: document
    client = RepairClient()
    service = DocumentService(repository=repository, repair_client=client)

    response = await service.raw(DocumentRawRequest(user_id="u", kb_id="kb", doc_id="d1"))

    assert isinstance(response, DocumentRawRepairAccepted)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_document_raw_status_url_uses_configured_api_prefix() -> None:
    repository = Repository()
    document = repository.fetch_document_raw()
    document["raw_mineru"] = None
    repository.fetch_document_raw = lambda **_: document
    service = DocumentService(
        repository=repository,
        repair_client=RepairClient(),
        api_prefix="/custom/rag",
    )

    response = await service.raw(DocumentRawRequest(user_id="u", kb_id="kb", doc_id="d1"))

    assert response.status_url.startswith("/custom/rag/documents/raw/status?")


@pytest.mark.asyncio
async def test_document_raw_missing_oss_key_returns_actionable_422() -> None:
    repository = Repository()
    document = repository.fetch_document_raw()
    document["raw_mineru"] = None
    document["file_oss_key"] = None
    repository.fetch_document_raw = lambda **_: document
    service = DocumentService(repository=repository, repair_client=RepairClient())

    with pytest.raises(ApiError) as error:
        await service.raw(DocumentRawRequest(user_id="u", kb_id="kb", doc_id="d1"))

    assert error.value.status_code == 422
    assert error.value.code == "DOCUMENT_OSS_KEY_MISSING"
    assert error.value.retryable is False
    assert "delete" in error.value.message.casefold()
    assert "re-ingest" in error.value.message.casefold()


@pytest.mark.asyncio
async def test_document_raw_completed_race_refetches_full_result() -> None:
    repository = Repository()
    incomplete = repository.fetch_document_raw()
    incomplete["raw_mineru"] = None
    complete = repository.fetch_document_raw()
    documents = iter([incomplete, complete])
    repository.fetch_document_raw = lambda **_: next(documents)

    class CompletedClient(RepairClient):
        async def submit(self, **kwargs: str):
            self.calls.append(kwargs)
            return {"status": "completed"}

    service = DocumentService(repository=repository, repair_client=CompletedClient())

    response = await service.raw(DocumentRawRequest(user_id="u", kb_id="kb", doc_id="d1"))

    assert isinstance(response, DocumentRawResponse)
    assert response.raw_mineru.md_content == "原文"


@pytest.mark.asyncio
async def test_document_raw_status_does_not_treat_empty_mapping_as_complete() -> None:
    repository = Repository()
    state = repository.fetch_document_raw_state()
    state["raw_mineru"] = {}
    repository.fetch_document_raw_state = lambda **_: state
    service = DocumentService(repository=repository, repair_client=RepairClient())

    response = await service.raw_status(
        DocumentRawStatusRequest(user_id="u", kb_id="kb", doc_id="d1")
    )

    assert response.status == "processing"


@pytest.mark.asyncio
async def test_document_raw_status_is_read_only() -> None:
    repository = Repository()
    client = RepairClient()
    service = DocumentService(repository=repository, repair_client=client)

    response = await service.raw_status(
        DocumentRawStatusRequest(user_id="u", kb_id="kb", doc_id="d1")
    )

    assert response.status == "processing"
    assert response.repair_id == "repair-1"
    assert client.calls == []


@pytest.mark.asyncio
async def test_document_raw_status_maps_persisted_error_message() -> None:
    repository = Repository()
    repository.fetch_document_raw_state = lambda **_: {
        "doc_id": "d1",
        "raw_mineru": None,
        "file_oss_key": "docs/d1.pdf",
        "raw_mineru_repair": {
            "status": "failed",
            "retryable": False,
            "error_code": "RAW_MINERU_INVALID_RESULT",
            "error_message": "MinerU returned no usable result",
        },
    }
    service = DocumentService(repository=repository, repair_client=RepairClient())

    response = await service.raw_status(
        DocumentRawStatusRequest(user_id="u", kb_id="kb", doc_id="d1")
    )

    assert response.message == "MinerU returned no usable result"


@pytest.mark.asyncio
async def test_document_raw_retryable_cooldown_returns_503() -> None:
    repository = Repository()
    document = repository.fetch_document_raw()
    document["raw_mineru"] = None
    repository.fetch_document_raw = lambda **_: document

    class CooldownClient(RepairClient):
        async def submit(self, **kwargs: str):
            self.calls.append(kwargs)
            return {
                "repair_id": "repair-1",
                "status": "failed",
                "retryable": True,
                "error_code": "RAW_MINERU_REPAIR_COOLDOWN",
                "error_message": "repair is cooling down",
                "next_retry_at": "2026-07-27T12:00:00Z",
            }

    with pytest.raises(ApiError) as error:
        await DocumentService(repository=repository, repair_client=CooldownClient()).raw(
            DocumentRawRequest(user_id="u", kb_id="kb", doc_id="d1")
        )

    assert error.value.status_code == 503
    assert error.value.code == "RAW_MINERU_REPAIR_COOLDOWN"
    assert error.value.retryable is True


@pytest.mark.asyncio
async def test_document_raw_terminal_failure_is_nonretryable() -> None:
    repository = Repository()
    repository.fetch_document_raw_state = lambda **_: {
        "doc_id": "d1",
        "raw_mineru": None,
        "file_oss_key": "docs/d1.pdf",
        "raw_mineru_repair": {
            "status": "failed",
            "retryable": False,
            "error_code": "RAW_MINERU_INVALID_RESULT",
            "message": "MinerU returned no usable result",
        },
    }
    service = DocumentService(repository=repository, repair_client=RepairClient())

    response = await service.raw_status(
        DocumentRawStatusRequest(user_id="u", kb_id="kb", doc_id="d1")
    )

    assert response.status == "failed"
    assert response.retryable is False
    assert response.error_code == "RAW_MINERU_INVALID_RESULT"


@pytest.mark.asyncio
async def test_document_raw_does_not_retry_terminal_failure() -> None:
    repository = Repository()
    document = repository.fetch_document_raw()
    document["raw_mineru"] = None
    document["raw_mineru_repair"] = {
        "status": "failed",
        "retryable": False,
        "error_code": "RAW_MINERU_INVALID_RESULT",
        "message": "MinerU returned no usable result",
    }
    repository.fetch_document_raw = lambda **_: document
    client = RepairClient()
    service = DocumentService(repository=repository, repair_client=client)

    with pytest.raises(ApiError) as error:
        await service.raw(DocumentRawRequest(user_id="u", kb_id="kb", doc_id="d1"))

    assert error.value.status_code == 422
    assert error.value.code == "RAW_MINERU_INVALID_RESULT"
    assert client.calls == []
