from __future__ import annotations

from app.parser.weak_heading_splitter import split_long_leaf_nodes


def test_splits_long_txt_leaf_by_chinese_weak_headings() -> None:
    text = "\n".join(
        [
            "白居易是唐代现实主义诗人，与元稹并称元白。",
            "一、赋得古原草送别",
            "离离原上草，一岁一枯荣。",
            "野火烧不尽，春风吹又生。",
            "这首诗借古原野草表达送别情绪。" * 20,
            "二、钱塘湖春行",
            "孤山寺北贾亭西，水面初平云脚低。",
            "这首诗描绘西湖早春景象。" * 20,
        ]
    )
    nodes = [
        {
            "title": "白居易诗歌",
            "level": 1,
            "start_index": 1,
            "end_index": 1,
            "text": text,
            "summary": "",
        }
    ]

    result = split_long_leaf_nodes(nodes, doc_type="txt", enabled=True, min_chars=40, max_chars=500)

    titles = [node["title"] for node in result]
    assert titles == ["一、赋得古原草送别", "二、钱塘湖春行"]
    assert "白居易是唐代现实主义诗人" in result[0]["text"]
    assert "钱塘湖春行" in result[1]["text"]
    assert all(len(node["text"]) >= 40 for node in result)


def test_does_not_split_short_leaf() -> None:
    nodes = [{"title": "短文", "level": 1, "text": "一、标题\n正文", "summary": ""}]

    result = split_long_leaf_nodes(nodes, doc_type="txt", enabled=True, min_chars=20, max_chars=100)

    assert result == nodes


def test_ignores_sentence_numbers_and_percentages() -> None:
    text = "\n".join(
        [
            "一去二三里，烟村四五家。",
            "公司今年的营业额比去年增长了 2.1%。",
            "这些内容只是正文，不应被识别成标题。" * 30,
        ]
    )
    nodes = [{"title": "正文", "level": 1, "text": text, "summary": ""}]

    result = split_long_leaf_nodes(nodes, doc_type="txt", enabled=True, min_chars=40, max_chars=100)

    assert len(result) > 1
    assert all("一去二三里" not in node["title"] for node in result)
    assert all("2.1" not in node["title"] for node in result)


def test_ignores_markdown_headings_inside_fenced_code() -> None:
    text = "\n".join(
        [
            "# 正文标题",
            "开头说明。" * 20,
            "```python",
            "# this is a code comment",
            "## not a markdown heading",
            "```",
            "## 真实小节",
            "真实正文。" * 40,
        ]
    )
    nodes = [{"title": "全文", "level": 1, "text": text, "summary": ""}]

    result = split_long_leaf_nodes(nodes, doc_type="md", enabled=True, min_chars=40, max_chars=250)

    titles = [node["title"] for node in result]
    assert "# this is a code comment" not in titles
    assert "not a markdown heading" not in titles
    assert "正文标题" in titles
    assert "真实小节" in titles


def test_only_md_and_txt_are_processed() -> None:
    nodes = [{"title": "一、标题", "level": 1, "text": "一、标题\n正文" * 100, "summary": ""}]

    result = split_long_leaf_nodes(nodes, doc_type="pdf", enabled=True, min_chars=20, max_chars=40)

    assert result == nodes

def test_html_markdown_preprocessed_docs_are_processed() -> None:
    text = "\n".join(
        [
            "HTML 转 Markdown 后的说明。",
            "一、项目背景",
            "背景正文。" * 80,
            "二、技术路线",
            "路线正文。" * 80,
        ]
    )
    nodes = [{"title": "HTML 页面", "level": 1, "text": text, "summary": ""}]

    result = split_long_leaf_nodes(nodes, doc_type="html", enabled=True, min_chars=40, max_chars=500)

    assert [node["title"] for node in result] == ["一、项目背景", "二、技术路线"]

