from __future__ import annotations

import pytest

from app.workflows.classification.guards import guard_category


@pytest.mark.parametrize(
    "query",
    [
        "你能看到什么？",
        "你能查到哪些信息？",
        "你能查到几篇文档？",
        "总结你看到的文档",
        "这些文档主要在讲什么？",
        "总结当前的4篇文档",
        "当前用户请求的4篇文档主要内容是什么？",
        "概述当前请求的12份文件",
        "概括当前这四篇材料",
        "这 12 份文件主要讲了什么",
    ],
)
def test_high_confidence_collection_queries_are_guarded_as_scope(query: str) -> None:
    assert guard_category(query) == "scope_direct"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("湖南海利2023年半年度报告有多少页", "routed_direct"),
        ("湖南海利报告的章节结构是什么", "routed_direct"),
        ("查看《中华人民共和国能源法》第三章", "routed_direct"),
        ("详细总结湖南海利2023年半年度报告", "routed_broad"),
        ("《中华人民共和国能源法》整体讲了什么", "routed_broad"),
        ("A文档与B文档整体有什么区别", "routed_broad"),
    ],
)
def test_high_confidence_direct_and_broad_queries_are_guarded(query: str, expected: str) -> None:
    assert guard_category(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "湖南海利2023年营业收入是多少",
        "A公司与B公司的营业额谁更高",
        "这些文档中哪些公司的营业收入超过一亿元",
        "帮我看看这份材料",
    ],
)
def test_uncertain_or_focused_queries_are_not_forced_by_guard(query: str) -> None:
    assert guard_category(query) is None
