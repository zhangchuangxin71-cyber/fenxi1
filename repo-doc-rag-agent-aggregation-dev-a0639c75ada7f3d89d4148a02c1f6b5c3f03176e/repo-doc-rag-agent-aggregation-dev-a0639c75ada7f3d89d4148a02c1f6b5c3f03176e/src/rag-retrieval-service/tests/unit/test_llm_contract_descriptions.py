from __future__ import annotations

from typing import Any

import pytest

from app.workflows.classification.strategies import classification_schema
from app.workflows.direct_access.planner import DIRECT_TOOLS
from app.workflows.document_routing.service import ROUTING_SYSTEM_PROMPT, route_schema
from app.workflows.focused_search.page_inspector import (
    PAGE_INSPECTION_PROMPT,
    page_decision_schema,
)
from app.workflows.focused_search.service import TREE_NAVIGATION_PROMPT, TREE_NAVIGATION_TOOLS
from app.workflows.scope_access.planner import SCOPE_TOOLS


def _assert_property_descriptions(value: Any, path: str = "schema") -> None:
    if isinstance(value, dict):
        properties = value.get("properties", {})
        for name, child in properties.items():
            assert child.get("description", "").strip(), f"missing description: {path}.{name}"
        for name, child in value.items():
            _assert_property_descriptions(child, f"{path}.{name}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_property_descriptions(child, f"{path}[{index}]")


@pytest.mark.parametrize(
    "contract",
    [classification_schema(), route_schema(), page_decision_schema()],
)
def test_structured_output_contracts_are_strict_and_describe_every_field(
    contract: dict[str, Any],
) -> None:
    assert contract["type"] == "json_schema"
    assert contract["json_schema"]["strict"] is True
    _assert_property_descriptions(contract["json_schema"]["schema"])


@pytest.mark.parametrize(
    "tool",
    [*DIRECT_TOOLS, *SCOPE_TOOLS, *TREE_NAVIGATION_TOOLS],
)
def test_function_tools_are_strict_and_describe_every_field(tool: dict[str, Any]) -> None:
    function = tool["function"]
    assert function["strict"] is True
    assert function["description"].strip()
    assert function["parameters"]["additionalProperties"] is False
    _assert_property_descriptions(function["parameters"])


@pytest.mark.parametrize(
    "prompt",
    [ROUTING_SYSTEM_PROMPT, PAGE_INSPECTION_PROMPT, TREE_NAVIGATION_PROMPT],
)
def test_decision_prompts_have_clear_sections(prompt: str) -> None:
    assert "# 角色与任务" in prompt
    assert "# 输入说明" in prompt
    assert "# 禁止事项" in prompt
    assert "# 输出约束" in prompt
