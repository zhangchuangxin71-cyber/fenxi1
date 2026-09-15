from __future__ import annotations

from pydantic import BaseModel

from app.llm.inputs import (
    ArticleGenerationInput,
    CleanInput,
    IntentInput,
    clean_conversation,
    clean_documents,
    clean_material_evidence,
    clean_material_summaries,
    render_input,
    require_task_spec,
)
from app.llm.schemas import (
    ImagePlanOutput,
    IntentOutput,
    MarkdownOutput,
    OrchestratorOutput,
    PreflightOutput,
    QueryPlanOutput,
    ResearchDirectionPlanOutput,
    ResearchDirectionValidationOutput,
    SessionMemoryOutput,
    TaskSpecOutput,
)


def _assert_property_descriptions(model: type[BaseModel]) -> None:
    schema = model.model_json_schema()
    objects = [schema, *(schema.get("$defs") or {}).values()]
    for object_schema in objects:
        for name, field_schema in (object_schema.get("properties") or {}).items():
            assert field_schema.get("description"), f"{model.__name__}.{name} has no description"


def test_every_llm_output_field_has_a_description() -> None:
    for model in (
        IntentOutput,
        SessionMemoryOutput,
        OrchestratorOutput,
        ResearchDirectionPlanOutput,
        ResearchDirectionValidationOutput,
        QueryPlanOutput,
        TaskSpecOutput,
        MarkdownOutput,
        ImagePlanOutput,
        PreflightOutput,
    ):
        _assert_property_descriptions(model)


def test_every_clean_llm_input_field_has_a_description() -> None:
    pending = list(CleanInput.__subclasses__())
    models: list[type[CleanInput]] = []
    while pending:
        model = pending.pop()
        models.append(model)
        pending.extend(model.__subclasses__())

    assert models
    for model in models:
        for name, field in model.model_fields.items():
            assert field.description, f"{model.__name__}.{name} has no description"


def test_clean_inputs_remove_transport_and_retrieval_metadata() -> None:
    documents = clean_documents(
        [
            {
                "doc_id": "doc-1",
                "doc_name": "政策.pdf",
                "doc_type": "pdf",
                "doc_description": "政策说明",
                "page_count": 12,
                "warnings": ["irrelevant"],
                "user_id": "private-user",
                "created_at": "2026-01-01",
            }
        ]
    )
    materials = clean_material_evidence(
        [
            {
                "material_id": "mat-1",
                "source_doc_id": "doc-1",
                "summary": "政策重点",
                "active": True,
                "metadata": {"warnings": ["noise"], "queries": ["internal query"]},
                "orig_chunks": [
                    {
                        "chunk_id": "chunk-1",
                        "page_number": 3,
                        "path": "第一章",
                        "content": "可引用事实",
                        "score": 0.98,
                        "debug": {"rank": 1},
                    }
                ],
            }
        ]
    )

    assert documents[0].model_dump() == {
        "doc_id": "doc-1",
        "name": "政策.pdf",
        "document_type": "pdf",
        "description": "政策说明",
        "page_count": 12,
    }
    assert materials[0].model_dump() == {
        "material_id": "mat-1",
        "material_kind": "user_document",
        "source": "doc-1",
        "summary": "政策重点",
        "evidence_chunks": [
            {
                "chunk_id": "chunk-1",
                "page_number": 3,
                "path": "第一章",
                "ref": "doc-1",
                "title": "doc-1",
                "content": "可引用事实",
            }
        ],
    }


def test_downstream_keeps_creative_and_popular_summaries_but_excludes_resources() -> None:
    materials = [
        {
            "material_id": "creative",
            "material_kind": "creative_reference",
            "source": "web_search",
            "summary": "可借鉴先故事后分析的结构",
            "orig_chunks": [{"chunk_id": "c1", "content": "可借鉴先故事后分析的结构"}],
        },
        {
            "material_id": "popular",
            "material_kind": "popular_culture",
            "source": "web_search",
            "summary": "说明牛来梗的含义和使用语境",
            "orig_chunks": [{"chunk_id": "c2", "content": "说明牛来梗的含义和使用语境"}],
        },
        {
            "material_id": "resource",
            "material_kind": "resource",
            "source": "web_search",
            "summary": "候选图片资源",
            "orig_chunks": [{"chunk_id": "c3", "content": "图片描述"}],
        },
    ]

    summaries = clean_material_summaries(materials)
    evidence = clean_material_evidence(materials)

    assert [item.material_kind for item in summaries] == ["creative_reference", "popular_culture"]
    assert [item.material_kind for item in evidence] == ["creative_reference", "popular_culture"]


def test_rendered_input_explains_fields_and_contains_only_clean_data() -> None:
    value = IntentInput(
        conversation=clean_conversation(
            [
                {
                    "role": "user",
                    "content": "生成文章",
                    "message_id": "message-private",
                    "tool_calls": [{"name": "hidden"}],
                }
            ]
        ),
        latest_user_input="生成文章",
        has_prior_article_revision=False,
        previous_revision_status="",
    )

    rendered = render_input(value)

    assert rendered.startswith("# 任务输入")
    assert "# 字段说明" in rendered
    assert "`latest_user_input`：本次需要分类的最新用户请求。" in rendered
    assert "<input_json>" in rendered
    assert '"content":"生成文章"' in rendered
    assert "message-private" not in rendered
    assert "tool_calls" not in rendered


def test_article_input_does_not_serialize_material_warnings() -> None:
    value = ArticleGenerationInput(
        task_spec=require_task_spec(
            {
                "topic": "主题",
                "audience": "读者",
                "goal": "目标",
                "tone": "专业",
                "length": "2000 字",
                "other_requirements": [],
            }
        ),
        outline_markdown="# 大纲",
        generation_basis="reference_materials",
        materials=clean_material_evidence(
            [
                {
                    "material_id": "mat-1",
                    "source_doc_id": "doc-1",
                    "summary": "摘要",
                    "metadata": {"warnings": ["不得进入模型"]},
                    "orig_chunks": [{"chunk_id": "c1", "content": "正文证据"}],
                }
            ]
        ),
        memory="",
        feedback="",
        current_candidate="",
    )

    rendered = render_input(value)

    assert "正文证据" in rendered
    assert "不得进入模型" not in rendered
    assert '"metadata"' not in rendered
