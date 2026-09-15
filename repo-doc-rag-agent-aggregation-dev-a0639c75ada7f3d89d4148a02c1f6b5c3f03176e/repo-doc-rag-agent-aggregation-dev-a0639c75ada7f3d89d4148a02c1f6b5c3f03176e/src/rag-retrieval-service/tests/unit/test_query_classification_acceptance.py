from __future__ import annotations

from app.workflows.classification.models import QueryGroup
from scripts.run_query_classification_acceptance import CASES, evaluate_groups


def _group(
    *,
    category: str,
    question: str,
    description: str | None = None,
    keywords: list[str] | None = None,
) -> QueryGroup:
    return QueryGroup(
        group_ref="g0001",
        category=category,
        queries=[question],
        target_docs_description=description,
        target_docs_keywords=keywords or [],
        ordinal=1,
    )


def test_evaluator_matches_questions_without_array_position_alignment() -> None:
    case = next(item for item in CASES if item.name == "mixed_three_categories")
    groups = [
        _group(category="scope_direct", question="你能看到哪些文档？"),
        _group(
            category="routed_direct",
            question="中华人民共和国能源法的章节结构是什么？",
            description="中华人民共和国能源法",
            keywords=["中华人民共和国能源法", "能源法"],
        ),
        _group(
            category="routed_focused",
            question="中华人民共和国能源法规定的能源规划类型有哪些？",
            description="中华人民共和国能源法",
            keywords=["中华人民共和国能源法", "能源法"],
        ),
    ]

    result = evaluate_groups(case, groups)

    assert result.retained == result.expected == 3
    assert result.category_correct == 3
    assert result.route_usable == result.route_expected == 2
    assert result.cluster_correct == result.cluster_pairs == 1


def test_evaluator_reports_cross_document_cluster_collapse() -> None:
    case = next(item for item in CASES if item.name == "cross_document_fact")
    groups = [
        _group(
            category="routed_focused",
            question="龙江交通2023年营业收入是多少？",
            description="两家公司年报",
            keywords=["龙江交通", "新乡化纤"],
        ),
        _group(
            category="routed_focused",
            question="新乡化纤2023年营业收入是多少？",
            description="两家公司年报",
            keywords=["龙江交通", "新乡化纤"],
        ),
    ]

    result = evaluate_groups(case, groups)

    assert result.retained == 2
    assert result.category_correct == 2
    assert result.cluster_pairs == 1
    assert result.cluster_correct == 0
