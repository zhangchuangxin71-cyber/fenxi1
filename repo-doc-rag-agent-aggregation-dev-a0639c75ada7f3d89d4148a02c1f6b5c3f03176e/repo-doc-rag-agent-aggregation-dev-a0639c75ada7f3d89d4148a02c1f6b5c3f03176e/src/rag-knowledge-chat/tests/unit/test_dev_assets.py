from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_dev_panel_renders_collapsible_reasoning_stream() -> None:
    script = (PROJECT_ROOT / "dev/static/app.js").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "dev/static/styles.css").read_text(encoding="utf-8")

    for token in (
        "appendReasoning",
        "reasoning_content",
        "reasoning-details",
        "reasoning-content",
        "思考过程",
    ):
        assert token in script
    assert ".reasoning-details" in styles
    assert ".reasoning-content" in styles
