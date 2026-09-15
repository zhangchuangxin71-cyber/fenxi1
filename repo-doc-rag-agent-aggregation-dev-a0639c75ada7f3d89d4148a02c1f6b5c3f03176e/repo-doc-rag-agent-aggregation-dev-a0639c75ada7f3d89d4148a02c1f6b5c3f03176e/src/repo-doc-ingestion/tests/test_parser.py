from __future__ import annotations

import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env", override=True)


FIXTURES = [
    ("pdf", "中国数字经济发展研究报告.pdf"),
    ("docx", "光明区光明街道旅游手册.docx"),
    ("md", "光明区光明街道旅游手册.md"),
    ("txt", "故事.txt"),
    ("xlsx", "日销售收入变动趋势分析.xlsx"),
    ("pptx", "机器学习：从技术原理到商业应用的全景解析.pptx"),
]


@pytest.mark.parametrize(("expected_type", "file_name"), FIXTURES)
def test_parse_fixture_to_structure(expected_type: str, file_name: str) -> None:
    """每种支持格式都应能解析成统一的 PageIndex JSON 结构。"""
    from app.parser.multiformat_parser import parse_document_to_structure

    file_path = FIXTURES_DIR / file_name
    if not file_path.is_file():
        pytest.skip(f"legacy fixture is not available: {file_path}")

    payload = parse_document_to_structure(
        file_path,
        generate_summary=False,
        generate_doc_description=False,
        runtime_overrides={
            # 单元测试关注解析结构，不调用 LLM，也尽量避免慢速表格/视觉增强。
            "pdf_parser": "pymupdf",
            "pdf_table_mode": "off",
            "summary_retry_times": 1,
            "summary_timeout_seconds": 5,
        },
    )

    assert payload["id"]
    assert payload["type"] == expected_type
    assert payload["doc_name"]
    assert isinstance(payload.get("pages"), list)
    assert payload["pages"], "pages should not be empty"

    structure = payload.get("structure") or payload.get("nodes")
    assert isinstance(structure, list)
    assert structure, "structure/nodes should not be empty"

    first_page = payload["pages"][0]
    assert "page" in first_page
    assert "content" in first_page
