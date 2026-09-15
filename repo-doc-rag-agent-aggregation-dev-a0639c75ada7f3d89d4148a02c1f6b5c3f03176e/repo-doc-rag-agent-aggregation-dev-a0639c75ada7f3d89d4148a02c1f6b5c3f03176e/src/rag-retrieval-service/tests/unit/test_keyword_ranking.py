from __future__ import annotations

from app.workflows.keyword_ranking import rank_texts, score_text, tokenize_query


def test_keyword_scoring_prefers_exact_query_then_term_frequency() -> None:
    query = "去年营业额"
    terms = tokenize_query(query)

    exact = score_text(query, terms, content="公司去年营业额为一亿元")
    partial = score_text(query, terms, content="营业额增长，去年经营稳定")
    unrelated = score_text(query, terms, content="公司员工数量")

    assert exact > partial > unrelated


def test_rank_texts_scores_every_item_and_is_stable() -> None:
    ranked = rank_texts(
        "风险控制",
        [
            ("p3", "无关内容", ""),
            ("p2", "风险控制措施", ""),
            ("p1", "风险控制措施", ""),
        ],
    )

    assert [item.item_id for item in ranked] == ["p2", "p1", "p3"]
    assert len(ranked) == 3
