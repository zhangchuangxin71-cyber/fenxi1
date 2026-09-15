import pytest

from app.agent.nodes.intent import intent_node

from .conftest import base_state


@pytest.mark.asyncio
async def test_intent_uses_outline_modify_rule_without_llm_context(mock_llm_provider):
    state = base_state(
        current_outline={"title": "旧大纲", "sections": []},
        outline_confirmed=False,
        query="删掉第三章",
    )

    result = await intent_node(state, {"configurable": {"emitter": None}})

    assert result["intent"] == "outline_modify"


@pytest.mark.asyncio
async def test_intent_routes_confirmed_outline_to_report_write(mock_llm_provider):
    state = base_state(
        current_outline={"title": "旧大纲", "sections": []},
        outline_confirmed=True,
        query="开始写",
    )

    result = await intent_node(state, {"configurable": {"emitter": None}})

    assert result["intent"] == "report_write"
