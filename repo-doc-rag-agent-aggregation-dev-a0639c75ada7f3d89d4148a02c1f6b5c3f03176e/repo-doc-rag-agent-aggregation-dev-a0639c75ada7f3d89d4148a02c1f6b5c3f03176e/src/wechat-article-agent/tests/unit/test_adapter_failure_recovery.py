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


def _failed(error: dict[str, object]) -> ArticleArtifact:
    now = datetime.now(UTC)
    return ArticleArtifact(
        artifact_id="art_failed",
        session_id="session-1",
        run_id="run_failed",
        current_response_id="resp_failed",
        revision=1,
        status="failed",
        current_stage="outline",
        last_error=error,
        doc_ids=["doc-1"],
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )


def _request() -> ResponseRequest:
    return ResponseRequest.model_validate(
        {
            "input": [{"role": "user", "content": "继续"}],
            "context": {
                "session_id": "session-1",
                "user_id": "user-1",
                "kb_id": "kb-1",
                "doc_ids": ["doc-1"],
            },
        }
    )


def _service(artifact: ArticleArtifact) -> AdapterService:
    return AdapterService(
        settings=cast(
            Any,
            SimpleNamespace(wechat_agent_thread_namespace=UUID("ec9cc62d-d864-45df-90f5-694ba7cccb85")),
        ),
        artifacts=cast(Any, SimpleNamespace(latest=AsyncMock(return_value=artifact))),
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


@pytest.mark.asyncio
async def test_continue_after_transient_failure_retries_checkpoint() -> None:
    failed = _failed({"code": "ARK_STREAM_TIMEOUT", "retryable": False})
    service = _service(failed)
    expected = StreamPlan(kind="runtime", artifact=failed, thread_id="thread")
    retry = AsyncMock(return_value=expected)
    service._retry_failed = retry  # type: ignore[method-assign]

    assert await service.admit(_request()) is expected
    retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_continue_after_attempt_exhaustion_starts_new_revision() -> None:
    failed = _failed({"code": "ARK_STRUCTURED_OUTPUT_INVALID", "retryable": False})
    service = _service(failed)
    expected = StreamPlan(kind="runtime", artifact=failed, thread_id="thread")
    start = AsyncMock(return_value=expected)
    service._start_revision = start  # type: ignore[method-assign]

    assert await service.admit(_request()) is expected
    call = start.await_args
    assert call is not None
    assert "新的 revision" in call.kwargs["prelude_text"]


@pytest.mark.asyncio
async def test_continue_after_input_failure_only_replays_error() -> None:
    failed = _failed(
        {"code": "NO_RELEVANT_DOCUMENTS", "message": "没有匹配研读范围的文档", "retryable": False}
    )
    service = _service(failed)

    result = await service.admit(_request())

    assert result.kind == "replay"
    assert "没有匹配研读范围的文档" in result.replay_reason
