from __future__ import annotations

import pytest

from app.workflows.classification.contracts import (
    binary_decision_schema,
    document_grouping_schema,
    validate_binary_decisions,
    validate_document_groups,
)


def test_binary_schema_requires_every_runtime_query_ref() -> None:
    refs = ["q_001", "q_009"]

    contract = binary_decision_schema(refs, schema_name="scope_decisions")

    decisions = contract["json_schema"]["schema"]["properties"]["decisions"]
    assert contract["json_schema"]["strict"] is True
    assert decisions["required"] == refs
    assert list(decisions["properties"]) == refs
    assert decisions["additionalProperties"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"decisions": {"q_001": True}},
        {"decisions": {"q_001": True, "q_002": False, "q_003": True}},
        {"decisions": {"q_001": 1, "q_002": False}},
        {"other": {"q_001": True, "q_002": False}},
    ],
)
def test_binary_validation_rejects_non_exact_results(payload: dict) -> None:
    with pytest.raises(ValueError):
        validate_binary_decisions(payload, ["q_001", "q_002"])


def test_document_grouping_requires_an_exact_query_partition() -> None:
    refs = ["q_001", "q_002", "q_003"]
    payload = {
        "document_groups": [
            {
                "query_refs": ["q_001", "q_002"],
                "target_docs_description": "A 公司年报",
                "target_docs_keywords": ["A 公司", "年报"],
            },
            {
                "query_refs": ["q_003"],
                "target_docs_description": "能源法",
                "target_docs_keywords": ["能源法"],
            },
        ]
    }

    groups = validate_document_groups(payload, refs)

    assert groups[0].query_refs == ("q_001", "q_002")
    assert groups[1].target_docs_keywords == ("能源法",)

    for invalid_refs in (
        [["q_001"], ["q_003"]],
        [["q_001", "q_002"], ["q_002", "q_003"]],
        [["q_001", "q_002"], ["q_004"]],
    ):
        invalid = {
            "document_groups": [
                {
                    "query_refs": group_refs,
                    "target_docs_description": "目标文档",
                    "target_docs_keywords": ["目标"],
                }
                for group_refs in invalid_refs
            ]
        }
        with pytest.raises(ValueError):
            validate_document_groups(invalid, refs)


def test_document_grouping_schema_limits_refs_to_runtime_enum() -> None:
    contract = document_grouping_schema(["q_003", "q_008"])

    item = contract["json_schema"]["schema"]["properties"]["document_groups"]["items"]
    assert item["properties"]["query_refs"]["items"]["enum"] == ["q_003", "q_008"]
    assert item["additionalProperties"] is False
