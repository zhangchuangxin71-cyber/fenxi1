from app.graph.builder import _grounded_image_prompt, _heading_positions


def test_heading_positions_include_exact_article_context() -> None:
    positions = _heading_positions("# 标题\n\n## 一、经营情况\n营业额为 1200 万元，同比增长 8.5%。\n")

    assert positions == [
        {
            "position_id": "pos_001",
            "heading_path": ["标题", "一、经营情况"],
            "paragraph_ordinal": 1,
            "context_text": "营业额为 1200 万元，同比增长 8.5%。",
        }
    ]


def test_grounded_image_prompt_preserves_facts_and_blocks_invention() -> None:
    prompt = _grounded_image_prompt(
        "纪实摄影风格的企业经营场景。",
        {
            "heading_path": ["经营复盘", "一、核心数据"],
            "context_text": "营业额为 1200 万元，同比增长 8.5%。",
        },
    )

    assert "营业额为 1200 万元，同比增长 8.5%。" in prompt
    assert "不得新增、替换或改写" in prompt
    assert "不含文字、数字、图表、流程节点或界面标签" in prompt
