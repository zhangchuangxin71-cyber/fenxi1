from __future__ import annotations

import pytest

from scripts.probe_dynamic_classification_schema import (
    build_dynamic_binary_schema,
    validate_dynamic_binary_decisions,
)


def test_dynamic_binary_schema_uses_query_refs_as_required_properties() -> None:
    query_refs = ["q_001", "q_007", "q_042"]

    response_format = build_dynamic_binary_schema(query_refs, schema_name="scope_decisions")

    assert response_format["type"] == "json_schema"
    contract = response_format["json_schema"]
    assert contract["name"] == "scope_decisions"
    assert contract["strict"] is True
    root = contract["schema"]
    assert root["required"] == ["decisions"]
    assert root["additionalProperties"] is False
    decisions = root["properties"]["decisions"]
    assert decisions["required"] == query_refs
    assert decisions["additionalProperties"] is False
    assert list(decisions["properties"]) == query_refs
    assert all(spec["type"] == "boolean" for spec in decisions["properties"].values())


def test_dynamic_binary_decisions_require_exact_boolean_key_set() -> None:
    query_refs = ["q_001", "q_002"]

    assert validate_dynamic_binary_decisions(
        {"decisions": {"q_001": True, "q_002": False}}, query_refs
    ) == {"q_001": True, "q_002": False}

    invalid_payloads = [
        {"decisions": {"q_001": True}},
        {"decisions": {"q_001": True, "q_002": False, "q_003": True}},
        {"decisions": {"q_001": 1, "q_002": False}},
        {"decisions": [True, False]},
        {"other": {"q_001": True, "q_002": False}},
    ]
    for payload in invalid_payloads:
        with pytest.raises(ValueError):
            validate_dynamic_binary_decisions(payload, query_refs)


@pytest.mark.parametrize(
    "query_refs",
    [[], ["q_001", "q_001"], ["invalid ref"], ["decisions"]],
)
def test_dynamic_binary_schema_rejects_unsafe_query_refs(query_refs: list[str]) -> None:
    with pytest.raises(ValueError):
        build_dynamic_binary_schema(query_refs)
