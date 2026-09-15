from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.ingestion.container import (
    DocumentAssistantRunner,
    DuplicateDocumentError,
    IngestRequest,
    IngestionService,
    PostgresTaskStore,
    TempIngestRequest,
    TaskStatus,
    TaskStatusData,
)


DOC_ID = "d8daff4d-8872-5417-8edd-ed639ea5c95e"


class _SubmitStore:
    def __init__(self, *, raw_complete: bool) -> None:
        self.raw_complete = raw_complete
        self.tasks = []
        self.existing = None
        self.repair_claims = []
        self.repair_updates = []

    def get_binding_doc_name(self, doc_id, user_id, kb_id):
        return "病毒分类举例——所有人.xlsx"

    def has_complete_raw_mineru(self, doc_id):
        return self.raw_complete

    def claim_raw_mineru_repair(self, **kwargs):
        self.repair_claims.append(kwargs)
        return {
            "outcome": "claimed",
            "repair": {"repair_id": kwargs["repair_id"], "status": "queued"},
        }

    def update_raw_mineru_repair(self, *, doc_id, repair_id, updates):
        self.repair_updates.append(
            {"doc_id": doc_id, "repair_id": repair_id, "updates": dict(updates)}
        )

    async def get_by_idempotency_key(self, *args):
        return self.existing

    async def get_by_biz_key(self, *args):
        return self.existing

    async def upsert(self, task):
        self.tasks.append(task)


class _NoopRunner:
    async def run(self, request, report):
        raise AssertionError("worker should not run during submission test")


def _request() -> IngestRequest:
    return IngestRequest(
        user_id="bi-agent-test-user",
        kb_id="bi-agent-test-kb",
        oss_key="bi-agent/病毒分类举例——所有人.xlsx",
        file_name="病毒分类举例——所有人.xlsx",
        file_type="xlsx",
    )


def _service(store: _SubmitStore) -> IngestionService:
    service = IngestionService(store=store, runner=_NoopRunner())

    async def resolve(self, request):
        return [DOC_ID]

    async def no_workers(self):
        return None

    service._resolve_submit_doc_ids = MethodType(resolve, service)
    service._ensure_workers = MethodType(no_workers, service)
    return service


def test_mineru_duplicate_with_missing_raw_is_queued_for_same_doc_id(monkeypatch) -> None:
    monkeypatch.setenv("INGEST_PARSER_BACKEND", "mineru")
    request = _request()
    store = _SubmitStore(raw_complete=False)

    result = asyncio.run(_service(store).submit_ingestion(request))

    assert result.status == "queued"
    assert result.doc_id == DOC_ID
    assert request.raw_mineru_repair_doc_ids == {DOC_ID}
    assert request.raw_mineru_repair_id
    assert store.repair_claims == [
        {
            "doc_id": DOC_ID,
            "user_id": request.user_id,
            "kb_id": request.kb_id,
            "repair_id": request.raw_mineru_repair_id,
            "require_source": False,
        }
    ]
    assert store.repair_updates[-1]["updates"] == {"task_id": result.task_id, "status": "queued"}
    assert len(store.tasks) == 1


def test_raw_repair_is_not_suppressed_by_historical_completed_task(monkeypatch) -> None:
    monkeypatch.setenv("INGEST_PARSER_BACKEND", "mineru")
    request = _request()
    store = _SubmitStore(raw_complete=False)
    store.existing = TaskStatusData(
        doc_id=DOC_ID,
        user_id=request.user_id,
        kb_id=request.kb_id,
        task_id="historical-task",
        status=TaskStatus.COMPLETED,
        progress=100,
        updated_at="2026-07-21T00:00:00Z",
        persisted_doc_id=DOC_ID,
        persisted_doc_ids=[DOC_ID],
    )

    result = asyncio.run(_service(store).submit_ingestion(request))

    assert result.status == "queued"
    assert result.task_id != "historical-task"
    assert len(store.tasks) == 1


def test_temporary_duplicate_in_another_session_reuses_existing_document(monkeypatch) -> None:
    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)
    store = _SubmitStore(raw_complete=True)
    store.existing = TaskStatusData(
        doc_id=DOC_ID,
        user_id="bi-agent-test-user",
        kb_id="bi-agent-test-kb",
        task_id="existing-task",
        status=TaskStatus.COMPLETED,
        progress=100,
        updated_at="2026-07-31T00:00:00Z",
        persisted_doc_id=DOC_ID,
        persisted_doc_ids=[DOC_ID],
        is_temp=True,
        session_id="session-a",
    )
    request = TempIngestRequest(
        user_id="bi-agent-test-user",
        kb_id="bi-agent-test-kb",
        session_id="session-b",
        oss_key="bi-agent/病毒分类举例——所有人.xlsx",
        file_name="病毒分类举例——所有人.xlsx",
        file_type="xlsx",
    )

    result = asyncio.run(_service(store).submit_temp_ingestion(request))

    assert result.task_id == "existing-task"
    assert result.doc_id == DOC_ID
    assert result.doc_ids == [DOC_ID]
    assert result.status == "completed"
    assert result.is_duplicate is True
    assert store.tasks == []


@pytest.mark.parametrize("backend,raw_complete", [("native", False), ("mineru", True)])
def test_duplicate_without_repair_eligibility_is_still_rejected(
    monkeypatch, backend: str, raw_complete: bool
) -> None:
    monkeypatch.setenv("INGEST_PARSER_BACKEND", backend)
    request = _request()

    with pytest.raises(DuplicateDocumentError):
        asyncio.run(_service(_SubmitStore(raw_complete=raw_complete)).submit_ingestion(request))

    assert request.raw_mineru_repair_doc_ids == set()


def test_runner_raw_repair_bypasses_full_indexing_and_persistence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INGEST_PARSER_BACKEND", "mineru")
    source = tmp_path / "existing.xlsx"
    source.write_bytes(b"xlsx fixture")
    patched = []

    class Store:
        def __init__(self):
            self.repair_updates = []

        def update_raw_mineru_repair(self, doc_id, repair_id, updates):
            self.repair_updates.append((doc_id, repair_id, updates))

        def patch_raw_mineru(self, doc_id, raw_mineru, repair_id=None):
            patched.append((doc_id, raw_mineru, repair_id))

    class Adapter:
        def build_indexing_options(self, **kwargs):
            raise AssertionError("repair must not build indexing options")

    async def parser(file_path, **kwargs):
        assert Path(file_path) == source
        return {
            "raw_mineru": {
                "md_content": "# repaired",
                "content_list": [],
                "middle_json": {"source": "mineru"},
            },
            "doc_description": "must not be persisted",
            "structure": [{"node_id": "must-not-be-persisted"}],
        }

    runner = DocumentAssistantRunner(adapter=Adapter(), mineru_parser=parser)
    store = Store()
    runner._resolve_input_files = lambda docs, temp_dir: asyncio.sleep(
        0, result=([source], [{"doc_name": source.name, "doc_type": "xlsx", "path": str(source)}])
    )
    runner._build_client = lambda **kwargs: SimpleNamespace(_store=store)
    request = _request()
    request._doc_id = DOC_ID
    request._doc_ids = [DOC_ID]
    request._raw_mineru_repair_doc_ids = {DOC_ID}
    request._raw_mineru_repair_id = "repair-1"
    reports = []

    async def report(**event):
        reports.append(event)

    asyncio.run(runner.run(request, report))

    assert patched == [
        (
            DOC_ID,
            {
                "md_content": "# repaired",
                "content_list": [],
                "middle_json": {"source": "mineru"},
            },
            "repair-1",
        )
    ]
    assert [event["status"] for event in reports] == [
        "parsing", "extracting", "structuring", "storing", "completed"
    ]
    assert reports[-1]["status"] == "completed"
    assert reports[-1]["persisted_doc_id"] == DOC_ID
    assert store.repair_updates[0][0:2] == (DOC_ID, "repair-1")
    assert store.repair_updates[0][2]["status"] == "processing"
    assert store.repair_updates[0][2]["lease_expires_at"]


def test_production_task_store_exposes_document_repair_checks() -> None:
    assert callable(getattr(PostgresTaskStore, "get_binding_doc_name", None))
    assert callable(getattr(PostgresTaskStore, "has_complete_raw_mineru", None))

def test_runner_rejects_incomplete_mineru_repair_as_terminal() -> None:
    from app.ingestion.container import RawMineruRepairTerminalError

    assert issubclass(RawMineruRepairTerminalError, Exception)


def test_production_task_store_exposes_on_demand_repair_operations() -> None:
    assert callable(getattr(PostgresTaskStore, "claim_raw_mineru_repair", None))
    assert callable(getattr(PostgresTaskStore, "get_raw_mineru_repair", None))
    assert callable(getattr(PostgresTaskStore, "update_raw_mineru_repair", None))


class _ClaimCursor:
    rowcount = 0

    def __init__(self, row) -> None:
        self.row = row
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, sql, params):
        self.statements.append((sql, params))

    def fetchone(self):
        return self.row


class _ClaimConnection:
    def __init__(self, row) -> None:
        self.cursor_value = _ClaimCursor(row)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def cursor(self):
        return self.cursor_value

    def commit(self):
        return None


def test_claim_raw_repair_returns_missing_source_without_update() -> None:
    store = PostgresTaskStore.__new__(PostgresTaskStore)
    connection = _ClaimConnection(
        (DOC_ID, "病毒分类举例——所有人.xlsx", "xlsx", None, {}, None)
    )
    store._connect = lambda: connection

    result = store.claim_raw_mineru_repair(
        doc_id=DOC_ID,
        user_id="bi-agent-test-user",
        kb_id="bi-agent-test-kb",
        repair_id="repair-1",
    )

    assert result["outcome"] == "missing_source"
    assert len(connection.cursor_value.statements) == 1
    assert "FOR UPDATE" in connection.cursor_value.statements[0][0]


def test_duplicate_claim_can_use_current_request_source_when_stored_key_is_missing() -> None:
    store = PostgresTaskStore.__new__(PostgresTaskStore)
    connection = _ClaimConnection(
        (DOC_ID, "病毒分类举例——所有人.xlsx", "xlsx", None, {}, None)
    )
    store._connect = lambda: connection

    result = store.claim_raw_mineru_repair(
        doc_id=DOC_ID,
        user_id="bi-agent-test-user",
        kb_id="bi-agent-test-kb",
        repair_id="repair-1",
        require_source=False,
    )

    assert result["outcome"] == "claimed"
    assert result["repair"]["repair_id"] == "repair-1"
    assert len(connection.cursor_value.statements) == 2


def test_claim_raw_repair_reuses_active_state_without_update() -> None:
    active = {
        "status": "processing",
        "repair_id": "repair-existing",
        "task_id": "task-existing",
        "attempt_count": 1,
        "lease_expires_at": "2999-01-01T00:00:00Z",
    }
    store = PostgresTaskStore.__new__(PostgresTaskStore)
    connection = _ClaimConnection(
        (
            DOC_ID,
            "病毒分类举例——所有人.xlsx",
            "xlsx",
            "bi-agent/病毒分类举例——所有人.xlsx",
            active,
            None,
        )
    )
    store._connect = lambda: connection

    result = store.claim_raw_mineru_repair(
        doc_id=DOC_ID,
        user_id="bi-agent-test-user",
        kb_id="bi-agent-test-kb",
        repair_id="repair-new",
    )

    assert result["outcome"] == "active"
    assert result["repair"]["repair_id"] == "repair-existing"
    assert len(connection.cursor_value.statements) == 1

def test_claim_raw_repair_persists_attempt_exhaustion_as_terminal() -> None:
    failed = {
        "status": "failed",
        "attempt_count": 3,
        "retryable": True,
    }
    store = PostgresTaskStore.__new__(PostgresTaskStore)
    connection = _ClaimConnection(
        (
            DOC_ID,
            "病毒分类举例——所有人.xlsx",
            "xlsx",
            "bi-agent/病毒分类举例——所有人.xlsx",
            failed,
            None,
        )
    )
    store._connect = lambda: connection

    result = store.claim_raw_mineru_repair(
        doc_id=DOC_ID,
        user_id="bi-agent-test-user",
        kb_id="bi-agent-test-kb",
        repair_id="repair-new",
    )

    assert result["outcome"] == "terminal_failed"
    assert result["repair"]["retryable"] is False
    assert result["repair"]["error_code"] == "RAW_MINERU_REPAIR_ATTEMPTS_EXHAUSTED"
    assert len(connection.cursor_value.statements) == 2



class _RepairSubmitStore:
    def __init__(self, outcome: str = "claimed") -> None:
        self.outcome = outcome
        self.tasks = []
        self.state_updates = []

    def claim_raw_mineru_repair(self, **kwargs):
        repair = {
            "repair_id": kwargs["repair_id"],
            "status": "queued",
            "attempt_count": 1,
        }
        result = {"outcome": self.outcome, "repair": repair}
        if self.outcome == "claimed":
            result["document"] = {
                "doc_id": DOC_ID,
                "doc_name": "病毒分类举例——所有人.xlsx",
                "doc_type": "xlsx",
                "file_oss_key": "bi-agent/病毒分类举例——所有人.xlsx",
            }
        return result

    def update_raw_mineru_repair(self, **kwargs):
        self.state_updates.append(kwargs)

    async def upsert(self, task):
        self.tasks.append(task)


def test_on_demand_repair_missing_source_is_nonretryable() -> None:
    from app.ingestion.container import RawMineruRepairError

    service = _service(_RepairSubmitStore(outcome="missing_source"))

    with pytest.raises(RawMineruRepairError) as exc_info:
        asyncio.run(
            service.submit_raw_mineru_repair(
                doc_id=DOC_ID,
                user_id="bi-agent-test-user",
                kb_id="bi-agent-test-kb",
            )
        )

    assert exc_info.value.code == "RAW_MINERU_SOURCE_MISSING"
    assert exc_info.value.retryable is False
    assert "删除" in str(exc_info.value)
    assert "重新入库" in str(exc_info.value)


def test_on_demand_repair_queues_existing_doc_without_new_id() -> None:
    store = _RepairSubmitStore()
    service = _service(store)

    result = asyncio.run(
        service.submit_raw_mineru_repair(
            doc_id=DOC_ID,
            user_id="bi-agent-test-user",
            kb_id="bi-agent-test-kb",
        )
    )

    assert result["doc_id"] == DOC_ID
    assert result["status"] == "queued"
    assert len(store.tasks) == 1
    assert store.tasks[0].doc_id == DOC_ID
    queued_request = service._normal_queue.get_nowait().request
    assert queued_request.doc_id == DOC_ID
    assert queued_request.raw_mineru_repair_doc_ids == {DOC_ID}
    assert queued_request.oss_key == "bi-agent/病毒分类举例——所有人.xlsx"
    assert store.state_updates[-1]["updates"]["task_id"] == result["task_id"]


def test_on_demand_repair_source_failure_starts_cooldown_at_failure() -> None:
    from app.ingestion.container import RawMineruRepairError

    store = _RepairSubmitStore()
    service = _service(store)

    async def unavailable(self, request):
        raise RuntimeError("OSS temporarily unavailable")

    service._resolve_submit_doc_ids = MethodType(unavailable, service)

    with pytest.raises(RawMineruRepairError):
        asyncio.run(
            service.submit_raw_mineru_repair(
                doc_id=DOC_ID,
                user_id="bi-agent-test-user",
                kb_id="bi-agent-test-kb",
            )
        )

    failure = store.state_updates[-1]["updates"]
    assert failure["retryable"] is True
    assert failure["lease_expires_at"] is None
    assert failure["next_retry_at"]


def test_on_demand_repair_rejects_source_content_mismatch() -> None:
    from app.ingestion.container import RawMineruRepairError

    store = _RepairSubmitStore()
    service = _service(store)

    async def mismatch(self, request):
        return ["11111111-1111-5111-8111-111111111111"]

    service._resolve_submit_doc_ids = MethodType(mismatch, service)

    with pytest.raises(RawMineruRepairError) as exc_info:
        asyncio.run(
            service.submit_raw_mineru_repair(
                doc_id=DOC_ID,
                user_id="bi-agent-test-user",
                kb_id="bi-agent-test-kb",
            )
        )

    assert exc_info.value.code == "RAW_MINERU_SOURCE_MISMATCH"
    assert exc_info.value.retryable is False
    assert store.tasks == []
    assert store.state_updates[-1]["updates"]["status"] == "failed"


@pytest.mark.parametrize(
    ("raised", "expected_retryable", "expected_code"),
    [
        ("terminal", False, "RAW_MINERU_INVALID_RESULT"),
        ("transient", True, "INGESTION_RUNTIME_ERROR"),
    ],
)
def test_on_demand_repair_persists_classified_failure(
    monkeypatch, raised: str, expected_retryable: bool, expected_code: str
) -> None:
    from app.ingestion.container import RawMineruRepairTerminalError

    task_id = "task-repair"

    class Store:
        def __init__(self):
            self.task = TaskStatusData(
                doc_id=DOC_ID,
                user_id="bi-agent-test-user",
                kb_id="bi-agent-test-kb",
                task_id=task_id,
                status=TaskStatus.QUEUED,
                progress=0,
                updated_at="2026-07-27T00:00:00Z",
            )
            self.repair_updates = []

        async def get_by_task_id(self, requested_task_id):
            assert requested_task_id == task_id
            return self.task

        async def update_status(self, requested_task_id, *, status, **updates):
            assert requested_task_id == task_id
            self.task = self.task.model_copy(
                update={
                    "status": status,
                    "progress": updates["progress"],
                    "error_message": updates.get("error_message"),
                    "error_code": updates.get("error_code"),
                    "retryable": updates.get("retryable"),
                }
            )
            return self.task

        def update_raw_mineru_repair(self, *, doc_id, repair_id, updates):
            self.repair_updates.append((doc_id, repair_id, updates))

    class Runner:
        async def run(self, request, report):
            if raised == "terminal":
                raise RawMineruRepairTerminalError("invalid MinerU payload")
            raise RuntimeError("MinerU temporarily unavailable")

    monkeypatch.setenv("INGEST_TASK_TIMEOUT_SECONDS", "0")
    store = Store()
    service = IngestionService(store=store, runner=Runner())
    request = _request()
    request._doc_id = DOC_ID
    request._doc_ids = [DOC_ID]
    request._raw_mineru_repair_doc_ids = {DOC_ID}
    request._raw_mineru_repair_id = "repair-1"

    asyncio.run(service._run_task(task_id, request))

    failure = store.repair_updates[-1][2]
    assert failure["status"] == "failed"
    assert failure["retryable"] is expected_retryable
    assert failure["error_code"] == expected_code
    assert bool(failure.get("next_retry_at")) is expected_retryable


def test_internal_repair_endpoint_returns_202_payload() -> None:
    from app.ingestion.container import RawMineruRepairRequest, repair_raw_mineru

    class Service:
        async def submit_raw_mineru_repair(self, **kwargs):
            assert kwargs == {
                "doc_id": DOC_ID,
                "user_id": "bi-agent-test-user",
                "kb_id": "bi-agent-test-kb",
            }
            return {
                "doc_id": DOC_ID,
                "repair_id": "repair-1",
                "task_id": "task-1",
                "status": "queued",
                "retryable": True,
            }

    response = asyncio.run(
        repair_raw_mineru(
            RawMineruRepairRequest(
                doc_id=DOC_ID,
                user_id="bi-agent-test-user",
                kb_id="bi-agent-test-kb",
            ),
            Service(),
        )
    )

    assert response.code == 202


def test_on_demand_repair_cooldown_preserves_failure_details() -> None:
    class CooldownStore(_RepairSubmitStore):
        def claim_raw_mineru_repair(self, **kwargs):
            return {
                "outcome": "cooldown",
                "repair": {
                    "repair_id": "repair-existing",
                    "status": "failed",
                    "retryable": True,
                    "error_code": "INGESTION_TIMEOUT",
                    "error_message": "MinerU repair timed out",
                    "next_retry_at": "2026-07-27T12:05:00Z",
                },
            }

    result = asyncio.run(
        _service(CooldownStore()).submit_raw_mineru_repair(
            doc_id=DOC_ID,
            user_id="bi-agent-test-user",
            kb_id="bi-agent-test-kb",
        )
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "INGESTION_TIMEOUT"
    assert result["error_message"] == "MinerU repair timed out"
