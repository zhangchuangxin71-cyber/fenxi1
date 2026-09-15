from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.llm.schemas import dynamic_theme_selection_output
from app.prompts.system import build_layout_theme_selection_system_prompt
from app.rendering.wechat_layout.catalog import describe_supported_themes
from app.rendering.wechat_layout.llm_selector import select_theme_with_llm
from app.rendering.wechat_layout.themes.registry import default_registry


def test_theme_catalog_is_generated_from_every_registered_theme() -> None:
    registry = default_registry()
    catalog = describe_supported_themes(registry)

    assert catalog.count("- theme_id=") == len(registry.ids())
    for theme in registry.all():
        assert f"theme_id={theme.id}" in catalog
        assert theme.label in catalog
        assert "、".join(theme.suitable_for) in catalog


def test_dynamic_theme_schema_only_accepts_registered_ids() -> None:
    ids = list(default_registry().ids())
    output_model = dynamic_theme_selection_output("TestThemeSelection", ids)
    schema = output_model.model_json_schema()

    assert schema["properties"]["theme_id"]["enum"] == ids
    accepted = output_model.model_validate(
        {"theme_id": ids[0], "user_facing_message": "已选择适合当前任务的排版主题。"}
    )
    assert accepted.theme_id == ids[0]
    with pytest.raises(ValidationError):
        output_model.model_validate(
            {"theme_id": "invented-theme", "user_facing_message": "选择了一个不存在的主题。"}
        )


def test_theme_selection_prompt_contains_catalog_and_scoped_contract() -> None:
    catalog = describe_supported_themes()
    prompt = build_layout_theme_selection_system_prompt(catalog)

    assert catalog in prompt
    assert "不包含文章正文" in prompt
    assert "不得创造" in prompt
    assert "user_facing_message" in prompt


@pytest.mark.asyncio
async def test_llm_selector_uses_task_spec_only_and_forwards_user_message() -> None:
    registry = default_registry()
    selected_id = registry.ids()[1]

    class FakeLLM:
        call: dict[str, Any]

        async def structured(self, **kwargs: Any) -> Any:
            self.call = kwargs
            return kwargs["output_model"].model_validate(
                {
                    "theme_id": selected_id,
                    "user_facing_message": "已根据文章定位选择简洁、稳定的排版主题。",
                }
            )

    llm = FakeLLM()
    result, catalog = await select_theme_with_llm(
        llm=llm,
        run_id="run_test",
        task_spec={
            "topic": "政策解读",
            "audience": "基层工作人员",
            "goal": "帮助读者理解执行要点",
            "tone": "专业",
            "length": "2000 字",
            "other_requirements": [],
            "article_markdown": "这段正文不得进入模型输入。",
        },
        registry=registry,
    )

    assert result.theme_id == selected_id
    assert result.user_facing_message
    assert llm.call["phase"] == "html_layout_theme"
    assert llm.call["thinking"] is False
    assert "article_markdown" not in llm.call["input_text"]
    assert "政策解读" in llm.call["input_text"]
    assert catalog in llm.call["system_prompt"]
