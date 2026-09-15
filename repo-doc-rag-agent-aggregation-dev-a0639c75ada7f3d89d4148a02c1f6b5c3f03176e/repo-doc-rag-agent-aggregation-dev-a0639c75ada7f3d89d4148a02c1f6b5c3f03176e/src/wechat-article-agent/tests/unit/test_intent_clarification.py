from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from langgraph.types import Command
from pydantic import ValidationError

from app.adapter.service import AdapterService
from app.adapter.state import PendingInteraction
from app.api.schemas import ResponseRequest
from app.artifacts.models import ArticleArtifact
from app.core.errors import AppError
from app.graph import builder
from app.graph.builder import (
    _parse_intent_topic_resume,
    intent_clarification_interrupt,
    intent_router,
    route_intent,
)
from app.llm.schemas import IntentOutput

OPTIONS = [
    {"id": "A", "topic": "人工智能如何改变日常工作"},
    {"id": "B", "topic": "普通人使用人工智能的实用方法"},
    {"id": "C", "topic": "人工智能应用中的机会与边界"},
]


def test_intent_output_requires_three_topics_only_when_clarification_is_needed() -> None:
    confirmed = IntentOutput.model_validate(
        {"is_wechat_article_intent": True, "user_facing_message": "", "options": []}
    )
    assert confirmed.is_wechat_article_intent is True

    clarification = IntentOutput.model_validate(
        {
            "is_wechat_article_intent": False,
            "user_facing_message": "可以将当前内容整理为公众号文章，请选择一个方向。",
            "options": OPTIONS,
        }
    )
    assert [item.id for item in clarification.options] == ["A", "B", "C"]

    with pytest.raises(ValidationError):
        IntentOutput.model_validate(
            {
                "is_wechat_article_intent": False,
                "user_facing_message": "请选择方向。",
                "options": OPTIONS[:2],
            }
        )
    with pytest.raises(ValidationError):
        IntentOutput.model_validate(
            {
                "is_wechat_article_intent": True,
                "user_facing_message": "请选择方向。",
                "options": OPTIONS,
            }
        )


@pytest.mark.asyncio
async def test_non_article_intent_creates_one_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = IntentOutput.model_validate(
        {
            "is_wechat_article_intent": False,
            "user_facing_message": "可以转换为公众号文章，请选择一个主题。",
            "options": OPTIONS,
        }
    )
    artifacts = SimpleNamespace(update_stage=AsyncMock())
    services = SimpleNamespace(
        llm=SimpleNamespace(structured=AsyncMock(return_value=output)), artifacts=artifacts
    )
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))
    monkeypatch.setattr(builder, "prefixed_id", lambda _: "int_intent")
    visible: list[str] = []
    monkeypatch.setattr(builder, "public_text", visible.append)

    result = await intent_router(
        {
            "run_id": "run-1",
            "response_id": "resp-1",
            "artifact_id": "art-1",
            "revision": 1,
            "current_user_input": "介绍一下这份文档",
            "has_prior_article_revision": False,
            "intent_clarification_used": False,
        },
        cast(Any, SimpleNamespace(context={})),
    )

    assert result["intent"] == "needs_clarification"
    assert result["intent_clarification_used"] is True
    assert result["pending_interrupt_id"] == "int_intent"
    assert result["intent_topic_options"] == OPTIONS
    assert route_intent(result) == "clarify"
    assert visible == ["可以转换为公众号文章，请选择一个主题。"]
    artifacts.update_stage.assert_awaited_once_with(
        "art-1",
        response_id="resp-1",
        current_stage="intent_clarification",
        status="waiting_for_input",
    )


@pytest.mark.asyncio
async def test_prior_revision_pure_continue_bypasses_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = SimpleNamespace(structured=AsyncMock())
    monkeypatch.setattr(
        builder,
        "get_graph_services",
        AsyncMock(return_value=SimpleNamespace(llm=llm)),
    )

    result = await intent_router(
        {
            "current_user_input": "继续",
            "has_prior_article_revision": True,
        },
        cast(Any, SimpleNamespace(context={})),
    )

    assert result == {"intent": "wechat_article", "intent_topic_options": []}
    llm.structured.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_input_cannot_trigger_a_second_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = IntentOutput.model_validate(
        {
            "is_wechat_article_intent": False,
            "user_facing_message": "请选择一个公众号文章方向。",
            "options": OPTIONS,
        }
    )
    artifacts = SimpleNamespace(update_stage=AsyncMock())
    services = SimpleNamespace(
        llm=SimpleNamespace(structured=AsyncMock(return_value=output)), artifacts=artifacts
    )
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))
    monkeypatch.setattr(builder, "public_text", lambda _: None)

    result = await intent_router(
        {
            "run_id": "run-1",
            "response_id": "resp-2",
            "artifact_id": "art-1",
            "revision": 1,
            "current_user_input": "用户在公众号文章意图确认卡中补充：请改成 PPT",
            "has_prior_article_revision": False,
            "intent_clarification_used": True,
        },
        cast(Any, SimpleNamespace(context={})),
    )

    assert result == {"intent": "other", "intent_topic_options": []}
    assert route_intent(result) == "other"
    artifacts.update_stage.assert_not_awaited()


def test_recommended_intent_topic_goes_directly_to_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        builder,
        "interrupt",
        lambda _: {
            "response_id": "resp-2",
            "decision": "revise",
            "selection": {"option_id": "B"},
        },
    )

    command = intent_clarification_interrupt(
        {
            "response_id": "resp-1",
            "artifact_id": "art-1",
            "revision": 1,
            "pending_interrupt_id": "int-1",
            "current_user_input": "介绍人工智能",
            "intent_topic_options": OPTIONS,
        }
    )

    assert isinstance(command, Command)
    assert command.goto == "start_planning"
    assert command.update is not None
    assert command.update["intent"] == "wechat_article"
    assert "普通人使用人工智能的实用方法" in command.update["current_user_input"]


def test_custom_intent_topic_is_reclassified_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        builder,
        "interrupt",
        lambda _: {
            "response_id": "resp-2",
            "decision": "revise",
            "selection": {"option_id": "custom", "topic": "乡村教育数字化"},
        },
    )

    command = intent_clarification_interrupt(
        {
            "response_id": "resp-1",
            "artifact_id": "art-1",
            "revision": 1,
            "pending_interrupt_id": "int-1",
            "current_user_input": "介绍这批资料",
            "intent_topic_options": OPTIONS,
            "intent_clarification_used": True,
        }
    )

    assert command.goto == "intent_router"
    assert command.update is not None
    assert command.update["intent"] == ""
    assert "意图确认卡中补充：乡村教育数字化" in command.update["current_user_input"]


def test_intent_topic_resume_rejects_unknown_option() -> None:
    assert _parse_intent_topic_resume({"selection": {"option_id": "A"}}, OPTIONS) == {
        "option_id": "A",
        "topic": OPTIONS[0]["topic"],
    }
    with pytest.raises(ValueError):
        _parse_intent_topic_resume({"selection": {"option_id": "D"}}, OPTIONS)


def _pending_intent_fixture() -> tuple[ArticleArtifact, PendingInteraction]:
    now = datetime.now(UTC)
    artifact = ArticleArtifact(
        artifact_id="art-1",
        session_id="session-1",
        run_id="run-1",
        current_response_id="resp-1",
        revision=1,
        status="waiting_for_input",
        current_stage="intent_clarification",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=3),
    )
    pending = PendingInteraction(
        runtime_interrupt_id="runtime-int-1",
        interrupt_id="int-1",
        response_id="resp-1",
        artifact_id="art-1",
        revision=1,
        stage="intent_clarification",
        form={"form_type": "agent_clarification", "clarification_type": "intent_confirmation"},
        raw={},
    )
    return artifact, pending


@pytest.mark.asyncio
async def test_adapter_accepts_topic_selection_for_intent_clarification() -> None:
    artifact, pending = _pending_intent_fixture()
    service = AdapterService(
        settings=cast(Any, SimpleNamespace()),
        artifacts=cast(Any, SimpleNamespace()),
        runtime=cast(Any, SimpleNamespace()),
        llm=cast(Any, SimpleNamespace()),
        admission=cast(Any, SimpleNamespace()),
    )
    expected = object()
    submit = AsyncMock(return_value=expected)
    service._submit_resume = submit  # type: ignore[method-assign]
    request = ResponseRequest.model_validate(
        {
            "input": [{"role": "user", "content": "乡村教育数字化"}],
            "previous_response_id": "resp-1",
            "context": {
                "session_id": "session-1",
                "hitl": {
                    "interrupt_id": "int-1",
                    "decision": "revise",
                    "selection": {"option_id": "custom", "topic": "乡村教育数字化"},
                },
            },
        }
    )

    result = await service._resume_explicit(request, "thread-1", artifact, pending)

    assert result is expected
    assert submit.await_args is not None
    assert submit.await_args.kwargs["selection"] == {
        "option_id": "custom",
        "topic": "乡村教育数字化",
    }


@pytest.mark.asyncio
async def test_adapter_rejects_research_direction_for_intent_clarification() -> None:
    artifact, pending = _pending_intent_fixture()
    service = AdapterService(
        settings=cast(Any, SimpleNamespace()),
        artifacts=cast(Any, SimpleNamespace()),
        runtime=cast(Any, SimpleNamespace()),
        llm=cast(Any, SimpleNamespace()),
        admission=cast(Any, SimpleNamespace()),
    )
    request = ResponseRequest.model_validate(
        {
            "input": [{"role": "user", "content": "确认"}],
            "previous_response_id": "resp-1",
            "context": {
                "session_id": "session-1",
                "hitl": {
                    "interrupt_id": "int-1",
                    "decision": "revise",
                    "selection": {
                        "option_id": "custom",
                        "about": "教育",
                        "target": "数字化转型",
                    },
                },
            },
        }
    )

    with pytest.raises(AppError) as caught:
        await service._resume_explicit(request, "thread-1", artifact, pending)

    assert caught.value.code == "INVALID_INTENT_SELECTION"
