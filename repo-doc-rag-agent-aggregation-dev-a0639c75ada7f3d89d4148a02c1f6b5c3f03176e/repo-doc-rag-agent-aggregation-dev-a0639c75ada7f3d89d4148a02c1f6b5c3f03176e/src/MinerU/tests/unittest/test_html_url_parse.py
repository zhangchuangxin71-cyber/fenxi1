# Copyright (c) Opendatalab. All rights reserved.
from mineru.backend.html import html_analyze
from mineru.backend.html.html_middle_json_mkcontent import union_make
from mineru.cli.output_paths import build_parse_dir, resolve_parse_dir
from mineru.utils.enum_class import MakeMode


def test_html_url_analyze_builds_mineru_text_outputs(monkeypatch):
    def fake_call_jina_reader(**kwargs):
        return {
            "url": kwargs["target_url"],
            "reader_url": f"https://r.jina.ai/{kwargs['target_url']}",
            "title": "Sino Life",
            "description": "Example description",
            "markdown": "# Sino Life\n\nHTML content",
            "jina_json": {"data": {"content": "# Sino Life\n\nHTML content"}},
            "usage": None,
            "http_status": 200,
            "elapsed_seconds": 0.01,
        }

    monkeypatch.setattr(html_analyze, "call_jina_reader", fake_call_jina_reader)

    middle_json, model_output = html_analyze.html_url_analyze(
        "https://www.sino-life.com/"
    )

    assert middle_json["_backend"] == "html"
    assert middle_json["_source_type"] == "html_url"
    assert middle_json["_source_url"] == "https://www.sino-life.com/"
    assert model_output["type"] == "html_url"
    assert model_output["title"] == "Sino Life"

    pdf_info = middle_json["pdf_info"]
    assert union_make(pdf_info, MakeMode.MM_MD, "images") == "# Sino Life\n\nHTML content"

    content_list = union_make(pdf_info, MakeMode.CONTENT_LIST, "images")
    assert content_list == [
        {
            "type": "text",
            "text": "# Sino Life\n\nHTML content",
            "page_idx": 0,
        }
    ]


def test_html_parse_dir_fallback(tmp_path):
    html_parse_dir = build_parse_dir(
        tmp_path,
        "www.sino-life.com",
        "pipeline",
        "auto",
        is_html=True,
    )
    html_parse_dir.mkdir(parents=True)

    assert (
        resolve_parse_dir(
            tmp_path,
            "www.sino-life.com",
            "pipeline",
            "auto",
            allow_html_fallback=True,
        )
        == html_parse_dir
    )

