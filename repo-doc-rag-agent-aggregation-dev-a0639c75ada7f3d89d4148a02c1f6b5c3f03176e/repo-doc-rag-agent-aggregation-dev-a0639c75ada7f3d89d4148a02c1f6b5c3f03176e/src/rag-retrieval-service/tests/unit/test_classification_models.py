from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.workflows.classification.models import (
    QueryClassificationOutput,
    materialize_groups,
)


def test_classification_requires_all_four_category_keys() -> None:
    with pytest.raises(ValidationError):
        QueryClassificationOutput.model_validate(
            {
                "routed_focused": [],
                "routed_broad": [],
                "routed_direct": [],
            }
        )


def test_classification_rejects_llm_generated_ids_and_reasons() -> None:
    with pytest.raises(ValidationError):
        QueryClassificationOutput.model_validate(
            {
                "routed_focused": [
                    {
                        "group_id": "made-up",
                        "reason": "not part of the contract",
                        "queries": ["A公司营业额是多少"],
                        "target_docs_description": "A公司年报",
                        "target_docs_keywords": ["A公司", "年报"],
                    }
                ],
                "routed_broad": [],
                "routed_direct": [],
                "scope_direct": [],
            }
        )


def test_materialize_groups_assigns_stable_ids_and_drops_empty_groups() -> None:
    output = QueryClassificationOutput.model_validate(
        {
            "routed_focused": [
                {
                    "queries": ["A公司营业额是多少", "B公司营业额是多少"],
                    "target_docs_description": "A公司与B公司年报",
                    "target_docs_keywords": ["A公司", "B公司", "年报"],
                }
            ],
            "routed_broad": [],
            "routed_direct": [],
            "scope_direct": [{"queries": ["你能看到哪些文档"]}, {"queries": []}],
        }
    )

    groups = materialize_groups(output)

    assert [group.group_ref for group in groups] == ["g0001", "g0002"]
    assert groups[0].category == "routed_focused"
    assert groups[0].queries == ["A公司营业额是多少", "B公司营业额是多少"]
    assert groups[1].category == "scope_direct"
    assert groups[1].target_docs_description is None
