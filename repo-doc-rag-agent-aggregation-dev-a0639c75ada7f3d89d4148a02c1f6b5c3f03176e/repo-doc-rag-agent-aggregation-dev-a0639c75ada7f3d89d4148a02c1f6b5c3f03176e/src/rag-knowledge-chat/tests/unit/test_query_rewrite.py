from app.chat.models import RouteDecision, RouteQuery
from app.chat.query_rewrite import (
    downgrade_unresolved_route_queries,
    resolve_incremental_document_references,
)


def test_incremental_document_listing_queries_become_explicit_metadata_queries() -> None:
    names = ["A公司年报.pdf", "临时制度.docx"]

    for query in (
        "我本次新增了什么文档",
        "你能看到我刚传的文档吗",
        "我刚刚传了什么文档",
    ):
        rewritten = resolve_incremental_document_references([query], names)

        assert rewritten == ["查看以下文档的名称与元信息：《A公司年报.pdf》、《临时制度.docx》"]


def test_incremental_document_content_reference_is_replaced_without_changing_task() -> None:
    rewritten = resolve_incremental_document_references(
        ["刚上传的文档主要讲了什么", "A公司年报的营业额是多少"],
        ["A公司年报.pdf", "临时制度.docx"],
    )

    assert rewritten == [
        "《A公司年报.pdf》、《临时制度.docx》主要讲了什么",
        "A公司年报的营业额是多少",
    ]


def test_unique_incremental_document_resolves_current_singular_reference() -> None:
    decision = RouteDecision(
        needs_retrieval=True,
        queries=[
            RouteQuery(
                status="ambiguous",
                reason="未能确定这份报告。",
                rewrite_query="这份报告的营业额是多少？",
            )
        ],
        reason_code="knowledge_base",
    )

    downgraded = downgrade_unresolved_route_queries(
        decision,
        unique_incremental_document_name="酒鬼酒2023年半年度报告.pdf",
    )

    assert downgraded == []
    assert decision.queries[0].status == "resolved"
    assert decision.queries[0].rewrite_query == "《酒鬼酒2023年半年度报告.pdf》的营业额是多少？"
    assert decision.query_items == ["《酒鬼酒2023年半年度报告.pdf》的营业额是多少？"]


def test_unique_incremental_document_does_not_hide_other_unresolved_references() -> None:
    decision = RouteDecision(
        needs_retrieval=True,
        queries=[
            RouteQuery(
                status="ambiguous",
                reason="两个对象都没有消解。",
                rewrite_query="对比这篇文档与上一篇文档的营业额。",
            )
        ],
        reason_code="knowledge_base",
    )

    downgrade_unresolved_route_queries(
        decision,
        unique_incremental_document_name="酒鬼酒2023年半年度报告.pdf",
    )

    assert decision.queries[0].status == "ambiguous"
    assert decision.queries[0].rewrite_query == "对比《酒鬼酒2023年半年度报告.pdf》与上一篇文档的营业额。"
