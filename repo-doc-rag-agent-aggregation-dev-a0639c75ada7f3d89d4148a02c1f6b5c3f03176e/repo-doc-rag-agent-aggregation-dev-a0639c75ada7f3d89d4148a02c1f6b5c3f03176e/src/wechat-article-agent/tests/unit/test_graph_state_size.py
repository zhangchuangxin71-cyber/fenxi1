from app.graph.state import WechatArticleState


def test_checkpoint_state_holds_references_not_large_artifacts() -> None:
    fields = set(WechatArticleState.__annotations__)
    assert "artifact_id" in fields
    assert "material_library" not in fields
    assert "article_markdown" not in fields
    assert "final_html" not in fields
    assert "conversation" not in fields
