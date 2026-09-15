from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env", override=True)


def test_mineru_fallback_log_includes_source_and_exception(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    import logging

    from app.parser import multiformat_parser

    source = tmp_path / "fallback.md"
    source.write_text("# 标题\n\n正文内容", encoding="utf-8")

    def fake_parse_with_mineru(file_path: str | Path, **kwargs: object) -> dict:
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(multiformat_parser, "parse_document_with_mineru", fake_parse_with_mineru)
    caplog.set_level(logging.WARNING, logger="app.parser.multiformat_parser")

    payload = multiformat_parser.parse_document_to_structure(
        source,
        runtime_overrides={
            "parser_backend": "mineru",
            "mineru_fallback_to_native": True,
        },
        generate_summary=False,
        generate_doc_description=False,
    )

    assert payload["type"] == "md"
    repair = payload["raw_mineru_repair"]
    assert repair["status"] == "failed"
    assert repair["attempt_count"] == 1
    assert repair["retryable"] is True
    assert repair["error_code"] == "RAW_MINERU_INITIAL_PARSE_FAILED"
    assert repair["error_message"] == "adapter exploded"
    assert repair["next_retry_at"]
    fallback_records = [
        record
        for record in caplog.records
        if "MinerU parser failed, falling back to native parser" in record.message
    ]
    assert fallback_records
    assert "fallback.md" in fallback_records[0].message
    assert ".md" in fallback_records[0].message
    assert "RuntimeError" in fallback_records[0].message
    assert "adapter exploded" in fallback_records[0].message
    assert fallback_records[0].exc_info is not None


def test_mineru_content_list_converts_to_pageindex_payload(tmp_path: Path) -> None:
    from app.parser.mineru_adapter import mineru_result_to_legacy_payload

    source = tmp_path / "demo.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    payload = mineru_result_to_legacy_payload(
        source_path=source,
        doc_type="pdf",
        md_content="# 项目总览\n\n正文",
        content_list=[
            {
                "type": "text",
                "text": "项目总览",
                "text_level": 1,
                "page_idx": 0,
            },
            {
                "type": "text",
                "text": "这里介绍项目背景和目标。",
                "page_idx": 0,
            },
            {
                "type": "table",
                "table_caption": ["关键指标"],
                "table_body": "<html><body><table><tr><td>指标</td><td>值</td></tr></table></body></html>",
                "page_idx": 0,
            },
            {
                "type": "text",
                "text": "实施方案",
                "text_level": 2,
                "page_idx": 1,
            },
            {
                "type": "list",
                "text": "1. 完成解析接入\n2. 验证入库结构",
                "page_idx": 1,
            },
        ],
        middle_json={"_backend": "mineru-test"},
    )

    assert payload["type"] == "pdf"
    assert payload["doc_name"] == "demo.pdf"
    assert payload["page_count"] == 2
    assert len(payload["pages"]) == 2
    assert payload["pages"][0]["page"] == 1
    assert "关键指标" in payload["pages"][0]["content"]
    assert "指标" in payload["pages"][0]["content"]
    assert payload["pages"][1]["page"] == 2
    assert "实施方案" in payload["pages"][1]["content"]

    structure = payload["structure"]
    assert structure[0]["title"] == "项目总览"
    assert "这里介绍项目背景和目标" in structure[0]["text"]
    assert structure[0]["nodes"][0]["title"] == "实施方案"
    assert "完成解析接入" in structure[0]["nodes"][0]["text"]
    assert payload["raw_mineru"]["md_content"] == "# 项目总览\n\n正文"
    assert payload["raw_mineru"]["content_list"][0]["text"] == "项目总览"
    assert payload["raw_mineru"]["middle_json"]["_backend"] == "mineru-test"


def test_mineru_markdown_content_fallback_builds_heading_tree(tmp_path: Path) -> None:
    from app.parser.mineru_adapter import mineru_result_to_legacy_payload

    source = tmp_path / "demo.md"
    source.write_text("# Root\n\nRoot intro.\n\n## Child\n\nChild body.", encoding="utf-8")

    payload = mineru_result_to_legacy_payload(
        source_path=source,
        doc_type="md",
        md_content="# Root\n\nRoot intro.\n\n## Child\n\nChild body.",
        content_list=[
            {
                "type": "text",
                "text": "# Root\n\nRoot intro.\n\n## Child\n\nChild body.",
                "page_idx": 0,
            }
        ],
        middle_json={},
    )

    root = payload["structure"][0]
    assert root["title"] == "Root"
    assert "Root intro" in root["text"]
    assert root["nodes"][0]["title"] == "Child"
    assert "Child body" in root["nodes"][0]["text"]


def test_mineru_empty_heading_block_is_skipped(tmp_path: Path) -> None:
    from app.parser.mineru_adapter import mineru_result_to_legacy_payload

    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    payload = mineru_result_to_legacy_payload(
        source_path=source,
        doc_type="pdf",
        md_content="",
        content_list=[
            {
                "type": "text",
                "text": "",
                "text_level": 2,
                "page_idx": 0,
                "bbox": [46, 30, 110, 84],
            },
            {
                "type": "text",
                "text": "申请条件 APPLICATION REQUIREMENTS",
                "text_level": 2,
                "page_idx": 0,
            },
            {
                "type": "text",
                "text": "有意愿来浦东工作的人才",
                "page_idx": 0,
            },
        ],
        middle_json={},
    )

    assert [node["title"] for node in payload["structure"]] == ["申请条件 APPLICATION REQUIREMENTS"]
    assert payload["structure"][0]["text"] == "有意愿来浦东工作的人才"


def test_mineru_weak_heading_postprocess_splits_overlong_txt_leaf(tmp_path: Path) -> None:
    from app.parser.mineru_adapter import mineru_result_to_legacy_payload

    source = tmp_path / "poems.txt"
    text = "\n".join(
        [
            "唐诗选读说明。",
            "一、赋得古原草送别",
            "离离原上草，一岁一枯荣。" * 20,
            "二、钱塘湖春行",
            "孤山寺北贾亭西，水面初平云脚低。" * 20,
        ]
    )
    source.write_text(text, encoding="utf-8")

    payload = mineru_result_to_legacy_payload(
        source_path=source,
        doc_type="txt",
        md_content=text,
        content_list=[{"type": "text", "text": text, "page_idx": 0}],
        middle_json={},
        weak_heading_split_enabled=True,
        weak_heading_split_min_chars=40,
        weak_heading_split_max_chars=500,
    )

    titles = [node["title"] for node in payload["structure"]]
    assert titles == ["一、赋得古原草送别", "二、钱塘湖春行"]
    assert payload["structure"][0]["node_id"] == "0000"
    assert payload["structure"][1]["node_id"] == "0001"
    assert "唐诗选读说明" in payload["structure"][0]["text"]


def test_parse_document_to_structure_uses_mineru_backend(monkeypatch, tmp_path: Path) -> None:
    from app.parser import multiformat_parser

    source = tmp_path / "demo.md"
    source.write_text("# 标题\n\n正文内容", encoding="utf-8")

    def fake_parse_with_mineru(file_path: str | Path, **kwargs: object) -> dict:
        assert Path(file_path) == source
        assert kwargs["client_concurrency"] == 1
        assert kwargs["weak_heading_split_enabled"] is True
        assert kwargs["weak_heading_split_min_chars"] == 33
        assert kwargs["weak_heading_split_max_chars"] == 333
        return {
            "id": "placeholder",
            "type": "md",
            "path": str(source),
            "doc_name": "demo.md",
            "doc_description": "",
            "page_count": 1,
            "structure": [
                {
                    "title": "标题",
                    "node_id": "0000",
                    "start_index": 1,
                    "end_index": 1,
                    "text": "正文内容",
                    "summary": "",
                }
            ],
            "raw_mineru": {
                "md_content": "# 标题\n\n正文内容",
                "content_list": [{"type": "text", "text": "正文内容", "page_idx": 0}],
                "middle_json": {"_backend": "pipeline", "_version_name": "test"},
            },
            "pages": [{"page": 1, "content": "标题\n正文内容"}],
        }

    monkeypatch.setattr(multiformat_parser, "parse_document_with_mineru", fake_parse_with_mineru)

    payload = multiformat_parser.parse_document_to_structure(
        source,
        runtime_overrides={
            "parser_backend": "mineru",
            "mineru_api_url": "http://127.0.0.1:8000",
            "mineru_client_concurrency": 1,
            "weak_heading_split_enabled": True,
            "weak_heading_split_min_chars": 33,
            "weak_heading_split_max_chars": 333,
        },
        generate_summary=False,
        generate_doc_description=False,
    )

    assert payload["type"] == "md"
    assert payload["structure"][0]["text"] == "正文内容"
    assert payload["pages"][0]["content"] == "标题\n正文内容"
    assert payload["raw_mineru"]["md_content"] == "# 标题\n\n正文内容"
    assert payload["raw_mineru"]["middle_json"]["_backend"] == "pipeline"
    assert "raw_mineru_repair" not in payload


def test_mineru_client_retries_transient_http_response() -> None:
    import asyncio

    import httpx

    from app.parser.mineru_adapter import MinerUClient, MinerUClientConfig

    client = MinerUClient(
        MinerUClientConfig(
            api_url="http://mineru.local",
            retry_times=2,
            retry_backoff_base=0,
        )
    )
    request = httpx.Request("POST", "http://mineru.local/tasks")
    calls = {"n": 0}

    async def flaky() -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(202, request=request, json={"task_id": "task-ok"})

    response = asyncio.run(client._with_retries(flaky))

    assert response.status_code == 202
    assert calls["n"] == 2
