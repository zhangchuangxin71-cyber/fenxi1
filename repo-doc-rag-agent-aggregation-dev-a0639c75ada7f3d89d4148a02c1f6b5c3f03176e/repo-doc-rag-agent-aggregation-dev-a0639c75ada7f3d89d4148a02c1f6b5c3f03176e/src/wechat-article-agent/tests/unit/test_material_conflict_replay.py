from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from app.adapter.service import AdapterService
from app.adapter.state import PendingInteraction
from app.artifacts.models import ArticleArtifact


def test_conflict_replay_uses_checkpoint_form_without_exposing_material_library() -> None:
    now = datetime.now(UTC)
    artifact = ArticleArtifact(
        artifact_id="art_1",
        session_id="session-1",
        run_id="run_1",
        current_response_id="resp_1",
        revision=1,
        status="waiting_for_input",
        current_stage="material_conflict_review",
        material_library=[{"secret": "完整素材正文不得重放"}],
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )
    fields = [
        {
            "conflict_id": "conflict_1",
            "message": "两个来源的数据口径不一致。",
            "sources": [
                {"path": "政策文件.pdf：第 2 页", "ref": "doc-1", "excerpt": "口径 A"},
                {"path": "example.com", "ref": "https://example.com", "excerpt": "口径 B"},
            ],
        }
    ]
    pending = PendingInteraction(
        runtime_interrupt_id="runtime_int_1",
        interrupt_id="int_1",
        response_id="resp_1",
        artifact_id="art_1",
        revision=1,
        stage="material_conflict_review",
        form={"fields": fields},
        raw={},
    )
    service = AdapterService(
        settings=cast(Any, SimpleNamespace()),
        artifacts=cast(Any, SimpleNamespace()),
        runtime=cast(Any, SimpleNamespace()),
        llm=cast(Any, SimpleNamespace()),
        admission=cast(Any, SimpleNamespace()),
    )

    plan = service._replay_pending(artifact, "thread-1", pending, "重新展示")

    assert plan.replay_stage == "material_conflicts"
    assert plan.replay_content == {"conflicts": fields}
    assert "完整素材正文" not in str(plan.replay_content)


async def test_replay_refreshes_artifact_and_thread_sliding_ttl() -> None:
    now = datetime.now(UTC)
    artifact = ArticleArtifact(
        artifact_id="art_1",
        session_id="session-1",
        run_id="run_1",
        current_response_id="resp_1",
        revision=1,
        status="completed",
        current_stage="completed",
        final_html="<p>done</p>",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )
    artifacts = SimpleNamespace(refresh_ttl=AsyncMock())
    runtime = SimpleNamespace(refresh_thread_ttl=AsyncMock())
    service = AdapterService(
        settings=cast(Any, SimpleNamespace()),
        artifacts=cast(Any, artifacts),
        runtime=cast(Any, runtime),
        llm=cast(Any, SimpleNamespace()),
        admission=cast(Any, SimpleNamespace()),
    )
    plan = service._replay_final(artifact, "thread-1")

    events = [event async for event in service._stream_replay(plan)]

    artifacts.refresh_ttl.assert_awaited_once_with("art_1")
    runtime.refresh_thread_ttl.assert_awaited_once_with("thread-1")
    assert events[-1] == "data: [DONE]\n\n"
