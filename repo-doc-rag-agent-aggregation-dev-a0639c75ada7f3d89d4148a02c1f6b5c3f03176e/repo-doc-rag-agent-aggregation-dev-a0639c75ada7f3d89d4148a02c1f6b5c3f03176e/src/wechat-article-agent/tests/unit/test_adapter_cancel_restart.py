from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.adapter.service import AdapterService, StreamPlan
from app.api.schemas import ResponseRequest
from app.artifacts.models import ArticleArtifact
from app.core.errors import AppError


@pytest.mark.asyncio
async def test_input_after_explicit_cancel_starts_new_routed_revision_even_with_stale_interrupt() -> None:
    now = datetime.now(UTC)
    cancelled = ArticleArtifact(
        artifact_id="art_cancelled",
        session_id="session-1",
        run_id="run_cancelled",
        current_response_id="resp_cancelled",
        revision=1,
        status="cancelled",
        current_stage="cancelled",
        doc_ids=["doc-1"],
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )
    runtime = SimpleNamespace(
        ensure_thread=AsyncMock(return_value={}),
        state=AsyncMock(
            return_value={
                "interrupts": [
                    {
                        "id": "int_old",
                        "value": {
                            "interrupt_id": "int_old",
                            "response_id": "resp_cancelled",
                            "artifact_id": "art_cancelled",
                            "revision": 1,
                            "stage": "task_spec_review",
                        },
                    }
                ]
            }
        ),
    )
    artifacts = SimpleNamespace(latest=AsyncMock(return_value=cancelled))
    service = AdapterService(
        settings=cast(
            Any,
            SimpleNamespace(wechat_agent_thread_namespace=UUID("ec9cc62d-d864-45df-90f5-694ba7cccb85")),
        ),
        artifacts=cast(Any, artifacts),
        runtime=cast(Any, runtime),
        llm=cast(Any, SimpleNamespace()),
        admission=cast(Any, SimpleNamespace()),
    )
    expected = StreamPlan(kind="runtime", artifact=cancelled, thread_id="thread")
    start_revision = AsyncMock(return_value=expected)
    service._start_revision = start_revision  # type: ignore[method-assign]
    request = ResponseRequest.model_validate(
        {
            "model": "wechat-article-agent",
            "stream": True,
            "input": [{"role": "user", "content": "继续"}],
            "context": {
                "session_id": "session-1",
                "user_id": "user-1",
                "kb_id": "kb-1",
                "doc_ids": ["doc-1"],
            },
        }
    )

    result = await service.admit(request)

    assert result is expected
    start_revision.assert_awaited_once()
    call = start_revision.await_args
    assert call is not None
    assert "不会恢复" in call.kwargs["prelude_text"]
    assert "force_full_restart" not in call.kwargs


@pytest.mark.asyncio
async def test_structured_decision_after_cancel_is_stale() -> None:
    now = datetime.now(UTC)
    cancelled = ArticleArtifact(
        artifact_id="art_cancelled",
        session_id="session-1",
        run_id="run_cancelled",
        current_response_id="resp_cancelled",
        revision=1,
        status="cancelled",
        current_stage="cancelled",
        doc_ids=["doc-1"],
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )
    service = AdapterService(
        settings=cast(
            Any,
            SimpleNamespace(wechat_agent_thread_namespace=UUID("ec9cc62d-d864-45df-90f5-694ba7cccb85")),
        ),
        artifacts=cast(Any, SimpleNamespace(latest=AsyncMock(return_value=cancelled))),
        runtime=cast(
            Any,
            SimpleNamespace(
                ensure_thread=AsyncMock(return_value={}),
                state=AsyncMock(return_value={}),
            ),
        ),
        llm=cast(Any, SimpleNamespace()),
        admission=cast(Any, SimpleNamespace()),
    )
    request = ResponseRequest.model_validate(
        {
            "input": [{"role": "user", "content": "已确认文章任务书。"}],
            "previous_response_id": "resp_cancelled",
            "context": {
                "session_id": "session-1",
                "hitl": {"interrupt_id": "int_old", "decision": "approve"},
            },
        }
    )

    with pytest.raises(AppError) as caught:
        await service.admit(request)

    assert caught.value.code == "STALE_INTERRUPT"


def test_new_revision_state_keeps_latest_input_for_normal_graph_routing() -> None:
    service = AdapterService(
        settings=cast(Any, SimpleNamespace()),
        artifacts=cast(Any, SimpleNamespace()),
        runtime=cast(Any, SimpleNamespace()),
        llm=cast(Any, SimpleNamespace()),
        admission=cast(Any, SimpleNamespace()),
    )
    now = datetime.now(UTC)
    artifact = ArticleArtifact(
        artifact_id="art_2",
        session_id="session-1",
        run_id="run_2",
        current_response_id="resp_2",
        revision=2,
        status="running",
        current_stage="docs_research",
        doc_ids=["doc-1"],
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )
    request = ResponseRequest.model_validate(
        {
            "input": [{"role": "user", "content": "之前的大纲不合适，请增加一个案例章节"}],
            "context": {"session_id": "session-1", "user_id": "u", "kb_id": "k"},
        }
    )

    graph_input = service._initial_state(
        request,
        artifact,
        previous=artifact.model_copy(update={"status": "cancelled"}),
        user_id="u",
        kb_id="k",
        doc_ids=["doc-1"],
        temp_doc_ids=[],
    )

    assert "force_full_restart" not in graph_input
    assert graph_input["current_user_input"] == "之前的大纲不合适，请增加一个案例章节"
    assert graph_input["previous_revision_status"] == "cancelled"
    assert graph_input["entry_stage"] == "docs_research"


@pytest.mark.asyncio
async def test_new_revision_accepts_an_empty_document_scope() -> None:
    queue_error = AppError(503, "RUN_QUEUE_FULL", "full", True)
    service = AdapterService(
        settings=cast(Any, SimpleNamespace()),
        artifacts=cast(Any, SimpleNamespace()),
        runtime=cast(Any, SimpleNamespace()),
        llm=cast(Any, SimpleNamespace()),
        admission=cast(Any, SimpleNamespace(acquire=AsyncMock(side_effect=queue_error))),
    )
    request = ResponseRequest.model_validate(
        {
            "input": [{"role": "user", "content": "写一篇关于时间管理的公众号文章"}],
            "context": {
                "session_id": "session-1",
                "user_id": "user-1",
                "kb_id": "kb-1",
                "doc_ids": [],
                "temp_doc_ids": [],
            },
        }
    )

    with pytest.raises(AppError) as caught:
        await service._start_revision(request, "thread-1", None, {})

    assert caught.value.code == "RUN_QUEUE_FULL"
