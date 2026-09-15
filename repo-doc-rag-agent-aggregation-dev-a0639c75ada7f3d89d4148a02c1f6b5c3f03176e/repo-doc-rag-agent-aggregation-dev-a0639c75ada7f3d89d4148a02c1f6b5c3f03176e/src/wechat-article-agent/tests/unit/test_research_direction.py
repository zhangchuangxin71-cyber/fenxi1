from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.api.schemas import ClarificationSelection, ResponseRequest
from app.core.errors import AppError
from app.graph import builder
from app.graph.builder import (
    _parse_research_direction_resume,
    _research_direction_text,
    conflict_handler,
    document_material_worker,
    generate_task_spec,
    load_document_meta,
    material_sufficiency_judge,
    route_documents,
    validate_custom_research_direction,
    web_material_worker,
)
from app.graph.state import WechatArticleState
from app.llm.schemas import (
    ResearchDirectionPlanOutput,
    ResearchDirectionValidationOutput,
    TaskSpecOutput,
)

OPTIONS = [
    {"id": "A", "about": "政策要求", "target": "对学校日常管理的影响"},
    {"id": "B", "about": "家庭教育", "target": "家长能够采取的行动"},
    {"id": "C", "about": "学生成长", "target": "规则与学习体验的关系"},
]


def test_direction_plan_requires_exactly_a_b_c() -> None:
    output = ResearchDirectionPlanOutput.model_validate(
        {
            "coverage_mode": "best_effort",
            "user_facing_message": "请选择本次文档研读方向。",
            "options": OPTIONS,
        }
    )
    assert [item.id for item in output.options] == ["A", "B", "C"]

    with pytest.raises(ValidationError):
        ResearchDirectionPlanOutput.model_validate(
            {
                "coverage_mode": "best_effort",
                "user_facing_message": "请选择本次文档研读方向。",
                "options": [OPTIONS[0], OPTIONS[0], OPTIONS[2]],
            }
        )


def test_recommended_direction_is_resolved_from_server_options() -> None:
    direction = _parse_research_direction_resume(
        {"response_id": "resp_2", "selection": {"option_id": "B"}}, OPTIONS
    )

    assert direction == {
        "option_id": "B",
        "about": "家庭教育",
        "target": "家长能够采取的行动",
    }
    assert _research_direction_text(direction) == ("素材搜集主题：家长能够采取的行动\n素材关注方面：家庭教育")


def test_custom_direction_requires_both_fields() -> None:
    direction = _parse_research_direction_resume(
        {
            "selection": {
                "option_id": "custom",
                "about": "乡村学校资源配置",
                "target": "可落地的改善措施",
            }
        },
        OPTIONS,
    )
    assert direction["option_id"] == "custom"

    with pytest.raises(ValueError):
        _parse_research_direction_resume(
            {"selection": {"option_id": "custom", "about": "乡村学校资源配置"}},
            OPTIONS,
        )


def test_hitl_request_accepts_recommended_or_custom_selection() -> None:
    base = {
        "input": [{"role": "user", "content": "已确认文档研读方向。"}],
        "previous_response_id": "resp_1",
        "context": {
            "session_id": "session-1",
            "hitl": {
                "interrupt_id": "int_1",
                "decision": "revise",
                "selection": {"option_id": "A"},
            },
        },
    }
    assert ResponseRequest.model_validate(base).context.hitl is not None
    base["context"]["hitl"]["decision"] = "approve"  # type: ignore[index]
    with pytest.raises(ValidationError):
        ResponseRequest.model_validate(base)
    base["context"]["hitl"]["decision"] = "revise"  # type: ignore[index]
    base["context"]["hitl"]["selection"] = {  # type: ignore[index]
        "option_id": "custom",
        "about": "乡村学校资源配置",
        "target": "可落地的改善措施",
    }
    assert ResponseRequest.model_validate(base).context.hitl is not None


def test_hitl_request_accepts_custom_intent_topic_selection() -> None:
    request = ResponseRequest.model_validate(
        {
            "input": [{"role": "user", "content": "围绕乡村教育数字化生成公众号文章"}],
            "previous_response_id": "resp_1",
            "context": {
                "session_id": "session-1",
                "hitl": {
                    "interrupt_id": "int_1",
                    "decision": "revise",
                    "selection": {
                        "option_id": "custom",
                        "topic": "乡村教育数字化",
                    },
                },
            },
        }
    )

    assert request.context.hitl is not None
    assert request.context.hitl.selection is not None
    assert isinstance(request.context.hitl.selection, ClarificationSelection)
    assert request.context.hitl.selection.topic == "乡村教育数字化"


@pytest.mark.asyncio
async def test_custom_direction_at_round_limit_skips_model_and_forces_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = SimpleNamespace(structured=AsyncMock())
    services = SimpleNamespace(
        settings=SimpleNamespace(clarification_max_rounds=2),
        llm=llm,
    )
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))
    visible: list[str] = []
    monkeypatch.setattr(builder, "public_text", visible.append)

    result = await validate_custom_research_direction(
        {
            "run_id": "run_1",
            "current_user_input": "生成文章",
            "research_direction": {
                "option_id": "custom",
                "about": "教育",
                "target": "重点",
            },
            "clarification_round": 2,
        }
    )

    assert result == {"status": "research_ready"}
    llm.structured.assert_not_awaited()
    assert "轮次上限" in visible[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("sufficient", [True, False])
async def test_custom_direction_validation_routes_or_reprompts(
    monkeypatch: pytest.MonkeyPatch,
    sufficient: bool,
) -> None:
    output = ResearchDirectionValidationOutput.model_validate(
        {
            "sufficient": sufficient,
            "user_facing_message": "方向已明确。" if sufficient else "请进一步明确研读方向。",
            "normalized_about": "义务教育政策",
            "normalized_target": "学校落地与家庭影响",
            "options": [] if sufficient else OPTIONS,
        }
    )
    artifacts = SimpleNamespace(update_stage=AsyncMock())
    structured = AsyncMock(return_value=output)
    services = SimpleNamespace(
        settings=SimpleNamespace(clarification_max_rounds=3),
        llm=SimpleNamespace(structured=structured),
        artifacts=artifacts,
    )
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))
    monkeypatch.setattr(builder, "public_text", lambda _: None)
    monkeypatch.setattr(builder, "prefixed_id", lambda _: "int_next")
    state: WechatArticleState = {
        "run_id": "run_1",
        "response_id": "resp_1",
        "artifact_id": "art_1",
        "current_user_input": "生成文章",
        "research_direction": {
            "option_id": "custom",
            "about": "教育",
            "target": "影响",
        },
        "document_meta": [],
        "session_memory": {"docs_research": "优先关注网友真实讨论"},
        "web_info_overview": "牛来是近期受到关注的电影及网络文化现象。",
        "clarification_round": 1,
    }

    result = await validate_custom_research_direction(state)
    input_text = structured.await_args.kwargs["input_text"]
    assert '"memory":"优先关注网友真实讨论"' in input_text
    assert '"web_info_overview":"牛来是近期受到关注的电影及网络文化现象。"' in input_text

    if sufficient:
        assert result["research_direction"] == {
            "option_id": "custom",
            "about": "义务教育政策",
            "target": "学校落地与家庭影响",
        }
        assert result["status"] == "research_ready"
        artifacts.update_stage.assert_not_awaited()
    else:
        assert result["status"] == "needs_clarification"
        assert result["pending_interrupt_id"] == "int_next"
        assert len(result["research_direction_options"]) == 3
        artifacts.update_stage.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_spec_receives_persisted_research_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def structured(**kwargs: object) -> TaskSpecOutput:
        captured.update(kwargs)
        return TaskSpecOutput.model_validate(
            {
                "user_facing_message": "任务书已生成，请审核。",
                "artifact": {
                    "topic": "教育政策",
                    "audience": "家长",
                    "goal": "解释政策影响",
                    "tone": "专业",
                    "length": "2000字",
                    "other_requirements": [],
                },
            }
        )

    artifact_record = SimpleNamespace(
        research_direction={
            "option_id": "A",
            "about": "义务教育政策",
            "target": "学校落地与家庭影响",
        },
        document_coverage_mode="best_effort",
        material_library=[],
        task_spec=None,
    )
    artifacts = SimpleNamespace(
        get=AsyncMock(return_value=artifact_record),
        update_stage=AsyncMock(),
    )
    services = SimpleNamespace(
        llm=SimpleNamespace(structured=structured),
        artifacts=artifacts,
    )
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))
    visible: list[str] = []
    monkeypatch.setattr(builder, "public_text", visible.append)
    monkeypatch.setattr(builder, "artifact", lambda **_: None)

    await generate_task_spec(
        {
            "run_id": "run_1",
            "response_id": "resp_1",
            "artifact_id": "art_1",
            "revision": 1,
            "current_user_input": "生成文章",
            "session_memory": {},
            "review_feedback": "",
        }
    )

    input_text = str(captured["input_text"])
    assert "# 字段说明" in input_text
    assert '"research_direction":{"about":"义务教育政策","target":"学校落地与家庭影响"}' in input_text
    assert '"generation_basis":"general_knowledge"' in input_text
    assert "option_id" not in input_text
    assert "基于通用知识生成文章" in visible[0]


@pytest.mark.asyncio
async def test_conflict_feedback_handler_keeps_original_research_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    material_library = [
        {
            "material_id": "mat-1",
            "material_kind": "factual",
            "active": True,
            "orig_chunks": [
                {
                    "chunk_id": "chunk-1",
                    "content": "营业额为 12 亿元",
                    "path": "年度报告",
                    "ref": "https://example.com/a",
                    "title": "来源 A",
                },
                {
                    "chunk_id": "chunk-2",
                    "content": "营业额为 15 亿元",
                    "path": "新闻报道",
                    "ref": "https://example.com/b",
                    "title": "来源 B",
                },
            ],
        }
    ]
    structured = AsyncMock(
        return_value=SimpleNamespace(model_dump=lambda: {"chunk-1": "", "chunk-2": "以年报口径为准"})
    )
    artifacts = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(material_library=material_library)),
        update_stage=AsyncMock(),
    )
    services = SimpleNamespace(llm=SimpleNamespace(structured=structured), artifacts=artifacts)
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))

    await conflict_handler(
        {
            "run_id": "run-1",
            "response_id": "resp-1",
            "artifact_id": "art-1",
            "research_direction": {"about": "票房数据与传播现象", "target": "电影牛来"},
            "conflict_resolution": "document_priority",
            "conflict_feedback": "",
            "material_conflicts": [
                {
                    "conflict_chunk_ids": ["chunk-1", "chunk-2"],
                    "user_facing_message": "两个来源的营业额口径不一致",
                }
            ],
        }
    )

    input_text = structured.await_args.kwargs["input_text"]
    assert '"research_direction":{"about":"票房数据与传播现象","target":"电影牛来"}' in input_text


@pytest.mark.asyncio
async def test_no_documents_skip_retrieval_and_continue_with_general_knowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieval = SimpleNamespace(meta=AsyncMock(), route=AsyncMock())
    services = SimpleNamespace(retrieval=retrieval)
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))

    state: WechatArticleState = {
        "user_id": "user-1",
        "kb_id": "kb-1",
        "session_id": "session-1",
        "doc_ids": [],
        "temp_doc_ids": [],
        "research_direction": {"about": "人工智能", "target": "日常应用"},
        "document_coverage_mode": "all_required",
    }

    meta_result = await load_document_meta(state)
    route_result = await route_documents(state)

    assert meta_result == {"current_stage": "material_sufficiency", "document_meta": []}
    assert route_result["relevant_doc_ids"] == []
    assert route_result["document_coverage_mode"] == "best_effort"
    assert route_result["retrieval_warnings"][0]["code"] == "NO_REFERENCE_DOCUMENTS"
    retrieval.meta.assert_not_awaited()
    retrieval.route.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_matching_documents_degrades_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieval = SimpleNamespace(route=AsyncMock(return_value=([], [])))
    services = SimpleNamespace(retrieval=retrieval)
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))
    events: list[dict[str, object]] = []
    monkeypatch.setattr(builder, "activity", lambda **event: events.append(event))

    result = await route_documents(
        {
            "user_id": "user-1",
            "kb_id": "kb-1",
            "session_id": "session-1",
            "doc_ids": ["doc-1"],
            "temp_doc_ids": [],
            "research_direction": {"about": "人工智能", "target": "日常应用"},
            "document_coverage_mode": "all_required",
        }
    )

    assert result["relevant_doc_ids"] == []
    assert result["document_coverage_mode"] == "best_effort"
    assert result["retrieval_warnings"][0]["code"] == "NO_RELEVANT_DOCUMENTS"
    assert len(events) == 2
    assert {event["kind"] for event in events} == {"tool"}
    assert {event["name"] for event in events} == {"retrieval.route_documents"}
    assert [event["label"] for event in events] == ["正在检索相关文档", "相关文档检索完成"]
    assert len({event["activity_id"] for event in events}) == 1
    assert events[0]["label"] != builder._NODE_LABELS.get("route_documents", "")
    assert events[-1]["status"] == "degraded"


@pytest.mark.asyncio
async def test_empty_relevant_documents_complete_document_worker_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(material_library=[])),
        replace_materials=AsyncMock(),
    )
    services = SimpleNamespace(artifacts=artifacts)
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))

    result = await document_material_worker(
        {
            "artifact_id": "art-1",
            "response_id": "resp-1",
            "revision": 1,
            "relevant_doc_ids": [],
            "document_meta": [],
            "document_coverage_mode": "best_effort",
        }
    )

    assert result == {"document_material_status": "completed"}
    artifacts.replace_materials.assert_awaited_once_with(
        "art-1", response_id="resp-1", sources=set(), materials=[]
    )


@pytest.mark.asyncio
async def test_all_relevant_documents_failing_technically_degrades_for_web_or_general_knowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieval = SimpleNamespace(
        raw=AsyncMock(side_effect=AppError(503, "RETRIEVAL_DOWN", "down", True)),
        retrieve=AsyncMock(side_effect=AppError(503, "RETRIEVAL_DOWN", "down", True)),
    )
    services = SimpleNamespace(
        retrieval=retrieval,
        llm=SimpleNamespace(structured=AsyncMock(side_effect=AppError(503, "MODEL_DOWN", "down", True))),
        artifacts=SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(material_library=[])),
            replace_materials=AsyncMock(),
        ),
        settings=SimpleNamespace(
            short_document_max_chars=10_000,
            material_query_groups_per_document=1,
        ),
    )
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))
    monkeypatch.setattr(builder, "activity", lambda **_: None)
    monkeypatch.setattr(builder, "emit", lambda *_args, **_kwargs: None)

    result = await document_material_worker(
        {
            "run_id": "run-1",
            "user_id": "user-1",
            "kb_id": "kb-1",
            "session_id": "session-1",
            "artifact_id": "art-1",
            "response_id": "resp-1",
            "revision": 1,
            "current_user_input": "生成文章",
            "doc_ids": ["doc-1"],
            "temp_doc_ids": [],
            "relevant_doc_ids": ["doc-1"],
            "research_direction": {"about": "人工智能", "target": "日常应用"},
            "document_meta": [{"doc_id": "doc-1", "doc_name": "参考文档"}],
            "document_coverage_mode": "best_effort",
        }
    )

    assert result == {"document_material_status": "degraded"}
    services.artifacts.replace_materials.assert_awaited_once()


@pytest.mark.asyncio
async def test_all_required_document_failure_does_not_silently_degrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieval = SimpleNamespace(
        raw=AsyncMock(side_effect=AppError(503, "RETRIEVAL_DOWN", "down", True)),
        retrieve=AsyncMock(side_effect=AppError(503, "RETRIEVAL_DOWN", "down", True)),
    )
    services = SimpleNamespace(
        retrieval=retrieval,
        llm=SimpleNamespace(structured=AsyncMock(side_effect=AppError(503, "MODEL_DOWN", "down", True))),
        artifacts=SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(material_library=[])),
            replace_materials=AsyncMock(),
        ),
        settings=SimpleNamespace(
            short_document_max_chars=10_000,
            material_query_groups_per_document=1,
        ),
    )
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))
    monkeypatch.setattr(builder, "activity", lambda **_: None)
    monkeypatch.setattr(builder, "emit", lambda *_args, **_kwargs: None)

    with pytest.raises(AppError) as caught:
        await document_material_worker(
            {
                "run_id": "run-1",
                "user_id": "user-1",
                "kb_id": "kb-1",
                "session_id": "session-1",
                "artifact_id": "art-1",
                "response_id": "resp-1",
                "revision": 1,
                "current_user_input": "比较全部指定文件",
                "doc_ids": ["doc-1"],
                "temp_doc_ids": [],
                "relevant_doc_ids": ["doc-1"],
                "research_direction": {"about": "政策差异", "target": "逐份比较"},
                "document_meta": [{"doc_id": "doc-1", "doc_name": "参考文档"}],
                "document_coverage_mode": "all_required",
            }
        )

    assert caught.value.code == "DOCUMENT_COVERAGE_REQUIRED"
    assert caught.value.details == {"failed_doc_ids": ["doc-1"]}
    services.artifacts.replace_materials.assert_not_awaited()


@pytest.mark.asyncio
async def test_material_sufficiency_failure_conservatively_disables_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = SimpleNamespace(
        settings=SimpleNamespace(web_search_enabled=True),
        llm=SimpleNamespace(
            structured=AsyncMock(side_effect=AppError(502, "ARK_STRUCTURED_OUTPUT_INVALID", "invalid"))
        ),
    )
    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))
    monkeypatch.setattr(builder, "emit", lambda kind, value: warnings.append((kind, value)))

    result = await material_sufficiency_judge(
        {
            "run_id": "run-1",
            "current_user_input": "生成文章",
            "document_meta": [{"doc_id": "doc-1", "doc_name": "参考文档"}],
        }
    )

    assert result == {"need_web_search": False, "web_info_overview": ""}
    assert warnings[0][1]["code"] == "MATERIAL_SUFFICIENCY_DEGRADED"


@pytest.mark.asyncio
async def test_web_material_search_failure_degrades_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = SimpleNamespace(replace_materials=AsyncMock())
    services = SimpleNamespace(
        llm=SimpleNamespace(
            web_search=AsyncMock(side_effect=AppError(503, "ARK_UPSTREAM_ERROR", "down", True))
        ),
        artifacts=artifacts,
    )
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))
    monkeypatch.setattr(builder, "activity", lambda **_: None)
    monkeypatch.setattr(builder, "public_text", lambda _: None)

    result = await web_material_worker(
        {
            "need_web_search": True,
            "run_id": "run-1",
            "response_id": "resp-1",
            "artifact_id": "art-1",
            "current_user_input": "生成文章",
            "research_direction": {"about": "传播现象", "target": "牛来"},
        }
    )

    assert result == {"web_material_status": "degraded"}
    artifacts.replace_materials.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_material_persistence_failure_is_not_silently_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_result = SimpleNamespace(
        output=SimpleNamespace(
            model_dump=lambda: {
                "factual": {
                    "summary": "可核实事实",
                    "urls": ["https://example.com/fact"],
                }
            }
        ),
        annotations=[
            {
                "url": "https://example.com/fact",
                "title": "事实来源",
                "summary": "可核实事实摘要",
            }
        ],
        queries=["牛来 事实"],
    )
    database_error = AppError(503, "DATABASE_UNAVAILABLE", "database down", True)
    artifacts = SimpleNamespace(replace_materials=AsyncMock(side_effect=database_error))
    services = SimpleNamespace(
        llm=SimpleNamespace(web_search=AsyncMock(return_value=search_result)),
        artifacts=artifacts,
    )
    monkeypatch.setattr(builder, "get_graph_services", AsyncMock(return_value=services))
    monkeypatch.setattr(builder, "activity", lambda **_: None)
    visible: list[str] = []
    monkeypatch.setattr(builder, "public_text", visible.append)

    with pytest.raises(AppError) as caught:
        await web_material_worker(
            {
                "need_web_search": True,
                "run_id": "run-1",
                "response_id": "resp-1",
                "artifact_id": "art-1",
                "current_user_input": "生成文章",
                "research_direction": {"about": "传播现象", "target": "牛来"},
            }
        )

    assert caught.value is database_error
    artifacts.replace_materials.assert_awaited_once()
    assert visible == []
