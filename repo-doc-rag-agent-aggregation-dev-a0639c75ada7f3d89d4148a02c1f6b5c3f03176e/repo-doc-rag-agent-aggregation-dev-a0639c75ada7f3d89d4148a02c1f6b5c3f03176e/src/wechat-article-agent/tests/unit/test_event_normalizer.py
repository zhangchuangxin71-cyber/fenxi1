from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from app.artifacts.models import ArticleArtifact
from app.events.normalizer import AgentEventNormalizer, terminal_error


async def _parts() -> AsyncIterator[dict[str, Any]]:
    yield {
        "event": "custom",
        "data": {
            "kind": "usage",
            "data": {
                "phase": "task_spec",
                "repair_attempt": 0,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "input_tokens_details": {"cached_tokens": 2},
            },
        },
    }
    yield {
        "event": "custom",
        "data": {
            "kind": "usage",
            "data": {
                "phase": "outline",
                "input_tokens": 20,
                "output_tokens": 8,
                "total_tokens": 28,
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        },
    }


async def test_terminal_response_aggregates_standard_model_usage() -> None:
    now = datetime.now(UTC)
    artifact = ArticleArtifact(
        artifact_id="art_1",
        session_id="session",
        run_id="run_1",
        current_response_id="resp_1",
        revision=1,
        status="completed",
        current_stage="completed",
        approved_through_stage="article",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )

    async def final_artifact() -> ArticleArtifact:
        return artifact

    async def final_state() -> dict[str, Any]:
        return {}

    async def runtime_status() -> dict[str, str]:
        return {"status": "success"}

    events = [
        item
        async for item in AgentEventNormalizer().stream(
            parts=_parts(),
            artifact=artifact,
            final_artifact=final_artifact,
            final_state=final_state,
            runtime_status=runtime_status,
        )
    ]
    terminal = next(item for item in events if item.startswith("event: response.completed\n"))
    payload = json.loads(terminal.split("data: ", 1)[1])

    assert payload["response"]["usage"] == {
        "input_tokens": 30,
        "output_tokens": 13,
        "total_tokens": 43,
        "input_tokens_details": {"cached_tokens": 2},
        "output_tokens_details": {"reasoning_tokens": 3},
    }
    assert "repair_attempt" not in payload["response"]["usage"]


async def test_duplicate_activity_state_is_forwarded_once() -> None:
    now = datetime.now(UTC)
    artifact = ArticleArtifact(
        artifact_id="art_1",
        session_id="session",
        run_id="run_1",
        current_response_id="resp_1",
        revision=1,
        status="completed",
        current_stage="completed",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )
    activity = {
        "id": "node_intent_router",
        "kind": "node",
        "name": "intent_router",
        "label": "识别请求类型",
        "status": "completed",
        "node": "intent_router",
    }

    async def parts() -> AsyncIterator[dict[str, Any]]:
        for _ in range(2):
            yield {"event": "custom", "data": {"kind": "activity", "data": activity}}

    async def final_artifact() -> ArticleArtifact:
        return artifact

    async def final_state() -> dict[str, Any]:
        return {}

    async def runtime_status() -> dict[str, str]:
        return {"status": "success"}

    events = [
        item
        async for item in AgentEventNormalizer().stream(
            parts=parts(),
            artifact=artifact,
            final_artifact=final_artifact,
            final_state=final_state,
            runtime_status=runtime_status,
        )
    ]

    matching = [
        item for item in events if item.startswith("event: agent.activity\n") and "node_intent_router" in item
    ]
    assert len(matching) == 1


async def test_debug_stream_splits_text_and_includes_material_library() -> None:
    now = datetime.now(UTC)
    materials = [{"material_id": "material_001", "content": "完整素材正文"}]
    artifact = ArticleArtifact(
        artifact_id="art_1",
        session_id="session",
        run_id="run_1",
        current_response_id="resp_1",
        revision=1,
        status="completed",
        current_stage="completed",
        approved_through_stage="docs_research",
        material_library=materials,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )

    async def parts() -> AsyncIterator[dict[str, Any]]:
        yield {
            "event": "custom",
            "data": {"kind": "public_text", "data": {"text": "这是一段需要拆分的解释性文本。"}},
        }
        yield {
            "event": "custom",
            "data": {
                "kind": "artifact",
                "data": {
                    "stage": "material_library",
                    "status": "completed",
                    "content": None,
                    "summary": {"material_count": 1},
                },
            },
        }
        yield {
            "event": "custom",
            "data": {
                "kind": "debug_snapshot",
                "data": {
                    "trace": {
                        "tool_calls": [
                            {
                                "id": "tool_1",
                                "tool": "retrieval_http",
                                "arguments": {"doc_ids": ["doc-1"]},
                                "result": {"chunks": 3},
                            }
                        ]
                    }
                },
            },
        }

    async def final_artifact() -> ArticleArtifact:
        return artifact

    async def final_state() -> dict[str, Any]:
        return {}

    async def runtime_status() -> dict[str, str]:
        return {"status": "success"}

    events = [
        item
        async for item in AgentEventNormalizer().stream(
            parts=parts(),
            artifact=artifact,
            final_artifact=final_artifact,
            final_state=final_state,
            runtime_status=runtime_status,
            include_debug_artifacts=True,
            include_debug_trace=True,
        )
    ]
    deltas = [
        json.loads(item.split("data: ", 1)[1])["delta"]
        for item in events
        if item.startswith("event: response.output_text.delta\n")
    ]
    material_event = next(item for item in events if item.startswith("event: agent.artifact\n"))
    material_payload = json.loads(material_event.split("data: ", 1)[1])
    terminal = next(item for item in events if item.startswith("event: response.completed\n"))
    terminal_payload = json.loads(terminal.split("data: ", 1)[1])

    assert len(deltas) > 1
    assert "".join(deltas) == "这是一段需要拆分的解释性文本。"
    assert material_payload["artifact"]["content"] == materials
    assert terminal_payload["debug"]["trace"]["tool_calls"][0]["arguments"] == {"doc_ids": ["doc-1"]}


async def test_non_debug_stream_hides_debug_artifacts_and_trace() -> None:
    now = datetime.now(UTC)
    artifact = ArticleArtifact(
        artifact_id="art_1",
        session_id="session",
        run_id="run_1",
        current_response_id="resp_1",
        revision=1,
        status="completed",
        current_stage="completed",
        approved_through_stage="article",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )

    async def parts() -> AsyncIterator[dict[str, Any]]:
        for stage in (
            "session_memory",
            "material_library",
            "material_sources",
            "task_spec",
            "final_html",
        ):
            yield {
                "event": "custom",
                "data": {
                    "kind": "artifact",
                    "data": {"stage": stage, "status": "completed", "content": {"stage": stage}},
                },
            }
        yield {
            "event": "custom",
            "data": {
                "kind": "debug_snapshot",
                "data": {
                    "trace": {
                        "llm_calls": [{"phase": "secret"}],
                        "tool_calls": [{"tool": "secret", "arguments": {"token": "hidden"}}],
                    }
                },
            },
        }

    async def final_artifact() -> ArticleArtifact:
        return artifact

    async def final_state() -> dict[str, Any]:
        return {}

    async def runtime_status() -> dict[str, str]:
        return {"status": "success"}

    events = [
        item
        async for item in AgentEventNormalizer().stream(
            parts=parts(),
            artifact=artifact,
            final_artifact=final_artifact,
            final_state=final_state,
            runtime_status=runtime_status,
        )
    ]
    artifact_stages = [
        json.loads(item.split("data: ", 1)[1])["artifact"]["stage"]
        for item in events
        if item.startswith("event: agent.artifact\n")
    ]
    terminal = next(item for item in events if item.startswith("event: response.completed\n"))
    terminal_payload = json.loads(terminal.split("data: ", 1)[1])

    assert artifact_stages == ["material_sources", "task_spec", "final_html"]
    assert "debug" not in terminal_payload


async def test_failed_stream_keeps_structured_artifact_error() -> None:
    now = datetime.now(UTC)
    expected_error = {
        "code": "INSUFFICIENT_REQUIREMENTS",
        "message": "Requirements remain ambiguous.",
        "retryable": False,
        "stage": "docs_research",
        "details": {"rounds": 3},
    }
    artifact = ArticleArtifact(
        artifact_id="art_1",
        session_id="session",
        run_id="run_1",
        current_response_id="resp_1",
        revision=1,
        status="failed",
        current_stage="assess_research_sufficiency",
        last_error=expected_error,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )

    async def parts() -> AsyncIterator[dict[str, Any]]:
        if False:
            yield {}

    async def final_artifact() -> ArticleArtifact:
        return artifact

    async def final_state() -> dict[str, Any]:
        return {"current_stage": "assess_research_sufficiency"}

    async def runtime_status() -> dict[str, str]:
        return {"status": "error"}

    events = [
        item
        async for item in AgentEventNormalizer().stream(
            parts=parts(),
            artifact=artifact,
            final_artifact=final_artifact,
            final_state=final_state,
            runtime_status=runtime_status,
        )
    ]
    terminal = next(item for item in events if item.startswith("event: response.failed\n"))
    payload = json.loads(terminal.split("data: ", 1)[1])

    assert payload["response"]["error"] == {
        **expected_error,
        "recovery_action": "new_revision",
    }


def test_terminal_error_recovers_stable_code_from_wrapped_runtime_error() -> None:
    now = datetime.now(UTC)
    artifact = ArticleArtifact(
        artifact_id="art_runtime",
        session_id="session",
        run_id="run_runtime",
        current_response_id="resp_runtime",
        revision=1,
        status="running",
        current_stage="outline",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )

    error = terminal_error(
        artifact,
        {"current_stage": "outline"},
        {
            "status": "error",
            "error": "RuntimeError('DATABASE_UNAVAILABLE: database temporarily unavailable')",
        },
    )

    assert error["code"] == "DATABASE_UNAVAILABLE"
    assert error["recovery_action"] == "resume_checkpoint"
