from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from app.artifacts.models import RevisionCreate
from app.artifacts.repository import ArtifactRepository
from app.config import AppSettings
from app.core.ids import prefixed_id, thread_id_for_session
from app.persistence.migrate import apply_migrations
from app.persistence.pool import create_pool
from app.runtime.client import GraphRuntimeClient
from app.runtime.maintenance import MaintenanceWorkers

DATABASE_URL = os.getenv(
    "WECHAT_TEST_DATABASE_URL",
    "postgresql://wechat_article_agent:wechat_article_agent@127.0.0.1:5433/wechat_article_agent",
)


def _settings() -> AppSettings:
    return AppSettings.model_validate(
        {
            "DATABASE_URL": DATABASE_URL,
            "WECHAT_AGENT_THREAD_NAMESPACE": "ee4b563b-9a8f-4d3c-9e04-7bd912566bf5",
            "AGENT_SERVER_URL": "http://127.0.0.1:8242",
            "ARK_RESPONSES_URL": "https://ark.cn-beijing.volces.com/api/v3/responses",
            "ARK_MODEL_FAST": "test",
            "ARK_MODEL_MAIN": "test",
            "RETRIEVAL_BASE_URL": "http://127.0.0.1:8220",
            "SEEDREAM_RESPONSES_URL": "https://ark.cn-beijing.volces.com/api/v3/responses",
            "SEEDREAM_MODEL": "test",
        }
    )


@pytest.fixture
async def repository() -> AsyncGenerator[ArtifactRepository, None]:
    await apply_migrations(DATABASE_URL)
    pool = create_pool(_settings())
    await pool.open()
    try:
        yield ArtifactRepository(pool)
    finally:
        await pool.close()


@pytest.mark.integration
async def test_revision_inherits_only_approved_upstream(repository: ArtifactRepository) -> None:
    session_id = prefixed_id("session")
    first = await repository.create_revision(
        RevisionCreate(
            artifact_id=prefixed_id("art"),
            session_id=session_id,
            run_id=prefixed_id("run"),
            current_response_id=prefixed_id("resp"),
            entry_stage="docs_research",
            doc_ids=["doc-1"],
            temp_doc_ids=[],
        )
    )
    first = await repository.update_stage(
        first.artifact_id,
        response_id=first.current_response_id,
        current_stage="task_spec",
        values={
            "relevant_doc_ids": ["doc-1"],
            "document_coverage_mode": "all_required",
            "research_direction": {
                "option_id": "A",
                "about": "教育政策",
                "target": "学校落地实践",
            },
            "material_library": [{"material_id": "mat-1"}],
        },
    )
    first = await repository.mark_approved(
        first.artifact_id,
        response_id=first.current_response_id,
        stage="docs_research",
        next_stage="task_spec",
    )
    first = await repository.update_stage(
        first.artifact_id,
        response_id=first.current_response_id,
        current_stage="task_spec_review",
        values={"task_spec": {"topic": "unapproved"}},
        status="waiting_for_input",
    )
    first = await repository.begin_cancel(first.artifact_id, response_id=first.current_response_id)
    await repository.finish_cancel(first.artifact_id, response_id=first.current_response_id)

    second = await repository.create_revision(
        RevisionCreate(
            artifact_id=prefixed_id("art"),
            session_id=session_id,
            run_id=prefixed_id("run"),
            current_response_id=prefixed_id("resp"),
            entry_stage="task_spec",
            doc_ids=["doc-1"],
            temp_doc_ids=[],
        )
    )
    assert second.revision == 2
    assert second.parent_artifact_id == first.artifact_id
    assert second.approved_through_stage == "docs_research"
    assert second.document_coverage_mode == "all_required"
    assert second.research_direction == {
        "option_id": "A",
        "about": "教育政策",
        "target": "学校落地实践",
    }
    assert second.material_library == [{"material_id": "mat-1"}]
    assert second.task_spec is None


@pytest.mark.integration
async def test_cancel_blocks_late_artifact_write(repository: ArtifactRepository) -> None:
    artifact = await repository.create_revision(
        RevisionCreate(
            artifact_id=prefixed_id("art"),
            session_id=prefixed_id("session"),
            run_id=prefixed_id("run"),
            current_response_id=prefixed_id("resp"),
            entry_stage="docs_research",
            doc_ids=["doc-1"],
            temp_doc_ids=[],
        )
    )
    await repository.begin_cancel(artifact.artifact_id, response_id=artifact.current_response_id)
    with pytest.raises(Exception) as raised:
        await repository.update_stage(
            artifact.artifact_id,
            response_id=artifact.current_response_id,
            current_stage="task_spec",
            values={"material_library": [{"late": True}]},
        )
    assert getattr(raised.value, "code", None) == "RUN_CANCELLED"


class _RuntimeStub:
    def __init__(self, *, fail_delete: bool = False) -> None:
        self.fail_delete = fail_delete
        self.deleted: list[str] = []

    async def delete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)
        if self.fail_delete:
            raise RuntimeError("runtime unavailable")


class _AdapterStub:
    def __init__(self) -> None:
        self.cancelled: list[tuple[str, str]] = []

    async def cancel(self, *, session_id: str, response_id: str) -> tuple[Any, int]:
        self.cancelled.append((session_id, response_id))
        return object(), 200


async def _expire(repository: ArtifactRepository, session_id: str) -> None:
    async with repository.pool.connection() as connection:
        await connection.execute(
            "UPDATE article_artifacts SET expires_at=%s WHERE session_id=%s",
            (datetime.now(UTC) - timedelta(minutes=1), session_id),
        )
        await connection.commit()


def _workers(
    repository: ArtifactRepository, runtime: _RuntimeStub, adapter: _AdapterStub
) -> MaintenanceWorkers:
    return MaintenanceWorkers(
        settings=_settings(),
        artifacts=repository,
        runtime=cast(GraphRuntimeClient, runtime),
        adapter=cast(Any, adapter),
    )


@pytest.mark.integration
async def test_cleanup_deletes_thread_before_artifacts(repository: ArtifactRepository) -> None:
    session_id = prefixed_id("cleanup")
    await repository.create_revision(
        RevisionCreate(
            artifact_id=prefixed_id("art"),
            session_id=session_id,
            run_id=prefixed_id("run"),
            current_response_id=prefixed_id("resp"),
            entry_stage="docs_research",
            doc_ids=[],
            temp_doc_ids=[],
        )
    )
    await _expire(repository, session_id)
    runtime = _RuntimeStub()
    await _workers(repository, runtime, _AdapterStub()).cleanup_expired_once()

    assert await repository.latest(session_id) is None
    assert thread_id_for_session(_settings().wechat_agent_thread_namespace, session_id) in runtime.deleted


@pytest.mark.integration
async def test_cleanup_keeps_artifacts_when_thread_delete_fails(
    repository: ArtifactRepository,
) -> None:
    session_id = prefixed_id("cleanup")
    await repository.create_revision(
        RevisionCreate(
            artifact_id=prefixed_id("art"),
            session_id=session_id,
            run_id=prefixed_id("run"),
            current_response_id=prefixed_id("resp"),
            entry_stage="docs_research",
            doc_ids=[],
            temp_doc_ids=[],
        )
    )
    await _expire(repository, session_id)
    await _workers(repository, _RuntimeStub(fail_delete=True), _AdapterStub()).cleanup_expired_once()

    assert await repository.latest(session_id) is not None
    await repository.delete_session(session_id)


@pytest.mark.integration
async def test_cancellation_reconciler_retries_cancelling_rows(
    repository: ArtifactRepository,
) -> None:
    artifact = await repository.create_revision(
        RevisionCreate(
            artifact_id=prefixed_id("art"),
            session_id=prefixed_id("cancel-reconcile"),
            run_id=prefixed_id("run"),
            current_response_id=prefixed_id("resp"),
            entry_stage="docs_research",
            doc_ids=[],
            temp_doc_ids=[],
        )
    )
    await repository.begin_cancel(artifact.artifact_id, response_id=artifact.current_response_id)
    adapter = _AdapterStub()
    await _workers(repository, _RuntimeStub(), adapter).reconcile_cancellations_once()
    assert (artifact.session_id, artifact.current_response_id) in adapter.cancelled


@pytest.mark.integration
async def test_cancelled_empty_revision_does_not_break_approved_inheritance(
    repository: ArtifactRepository,
) -> None:
    session_id = prefixed_id("inherit-chain")
    first = await repository.create_revision(
        RevisionCreate(
            artifact_id=prefixed_id("art"),
            session_id=session_id,
            run_id=prefixed_id("run"),
            current_response_id=prefixed_id("resp"),
            entry_stage="docs_research",
            doc_ids=["doc-1"],
            temp_doc_ids=[],
        )
    )
    first = await repository.update_stage(
        first.artifact_id,
        response_id=first.current_response_id,
        current_stage="task_spec",
        values={"material_library": [{"material_id": "mat-1"}], "relevant_doc_ids": ["doc-1"]},
    )
    first = await repository.mark_approved(
        first.artifact_id,
        response_id=first.current_response_id,
        stage="docs_research",
        next_stage="task_spec",
    )
    empty = await repository.create_revision(
        RevisionCreate(
            artifact_id=prefixed_id("art"),
            session_id=session_id,
            run_id=prefixed_id("run"),
            current_response_id=prefixed_id("resp"),
            entry_stage="docs_research",
            doc_ids=["doc-1"],
            temp_doc_ids=[],
        )
    )
    await repository.begin_cancel(empty.artifact_id, response_id=empty.current_response_id)
    await repository.finish_cancel(empty.artifact_id, response_id=empty.current_response_id)
    third = await repository.create_revision(
        RevisionCreate(
            artifact_id=prefixed_id("art"),
            session_id=session_id,
            run_id=prefixed_id("run"),
            current_response_id=prefixed_id("resp"),
            entry_stage="docs_research",
            doc_ids=["doc-1"],
            temp_doc_ids=[],
        )
    )
    third = await repository.initialize_entry_stage(
        third.artifact_id,
        response_id=third.current_response_id,
        entry_stage="task_spec",
    )

    assert third.parent_artifact_id == empty.artifact_id
    assert third.approved_through_stage == "docs_research"
    assert third.material_library == [{"material_id": "mat-1"}]
    await repository.delete_session(session_id)


@pytest.mark.integration
async def test_parallel_material_workers_replace_only_their_owned_subset(
    repository: ArtifactRepository,
) -> None:
    session_id = prefixed_id("parallel-materials")
    artifact = await repository.create_revision(
        RevisionCreate(
            artifact_id=prefixed_id("art"),
            session_id=session_id,
            run_id=prefixed_id("run"),
            current_response_id=prefixed_id("resp"),
            entry_stage="docs_research",
            doc_ids=["doc-1"],
            temp_doc_ids=[],
        )
    )
    await repository.update_stage(
        artifact.artifact_id,
        response_id=artifact.current_response_id,
        current_stage="material_workers",
        values={
            "material_library": [
                {
                    "material_id": "old-web",
                    "material_kind": "factual",
                    "source": "web_search",
                }
            ]
        },
    )

    await asyncio.gather(
        repository.replace_materials(
            artifact.artifact_id,
            response_id=artifact.current_response_id,
            sources={"doc-1"},
            materials=[
                {
                    "material_id": "doc-material",
                    "material_kind": "user_document",
                    "source": "doc-1",
                }
            ],
        ),
        repository.replace_materials(
            artifact.artifact_id,
            response_id=artifact.current_response_id,
            kinds={"factual", "resource", "creative_reference", "popular_culture"},
            materials=[
                {
                    "material_id": "new-web",
                    "material_kind": "popular_culture",
                    "source": "web_search",
                }
            ],
        ),
    )

    refreshed = await repository.get(artifact.artifact_id)
    assert refreshed is not None
    assert {item["material_id"] for item in refreshed.material_library or []} == {
        "doc-material",
        "new-web",
    }
    await repository.delete_session(session_id)
