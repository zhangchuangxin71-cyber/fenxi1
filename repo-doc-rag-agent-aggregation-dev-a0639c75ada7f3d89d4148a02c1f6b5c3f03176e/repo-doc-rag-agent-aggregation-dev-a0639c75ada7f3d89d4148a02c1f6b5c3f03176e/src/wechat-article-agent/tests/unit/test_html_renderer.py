from app.rendering.html import HtmlRenderer


def test_renderer_inserts_image_after_requested_paragraph() -> None:
    markdown = """# 标题

## 一、章节

第一段。

第二段。

## 二、结尾

结束。
"""
    html = HtmlRenderer().render(
        markdown,
        [
            {
                "url": "https://example.com/image.png",
                "caption": "示意图",
                "insertion_position": {
                    "heading_path": ["标题", "一、章节"],
                    "paragraph_ordinal": 1,
                },
            }
        ],
    )
    first = html.index("第一段")
    image = html.index("<figure>")
    second = html.index("第二段")
    assert first < image < second


def test_renderer_ignores_unsafe_image_url() -> None:
    html = HtmlRenderer().render(
        "# 标题\n\n正文。",
        [
            {
                "url": "javascript:alert(1)",
                "caption": "bad",
                "insertion_position": {"heading_path": ["标题"], "paragraph_ordinal": 1},
            }
        ],
    )
    assert "javascript:" not in html
    assert "<figure>" not in html
