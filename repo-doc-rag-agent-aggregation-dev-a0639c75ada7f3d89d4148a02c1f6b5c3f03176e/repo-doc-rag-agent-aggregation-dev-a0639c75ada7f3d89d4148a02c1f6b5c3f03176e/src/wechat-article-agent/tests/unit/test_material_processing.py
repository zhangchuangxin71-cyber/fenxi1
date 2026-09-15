from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.materials.processing import (
    apply_conflict_notes,
    material_source_names,
    normalize_material_library,
    replace_reference_chunks_with_summary,
    trim_materials_to_budget,
    web_search_materials,
)


def _material(kind: str, material_id: str, content: str, *, mode: str = "retrieve") -> dict[str, Any]:
    return {
        "material_id": material_id,
        "material_kind": kind,
        "source": "doc-1" if kind == "user_document" else "web_search",
        "summary": f"{kind} summary",
        "active": True,
        "metadata": {
            "extraction_mode": mode,
            "queries": [],
            "coverage_complete": True,
            "warnings": [],
        },
        "orig_chunks": [
            {
                "chunk_id": f"{material_id}-chunk",
                "content": content,
                "type": "full_text" if mode == "raw" else "page",
                "path": "opaque",
                "page_number": 2,
                "ref": "doc-1",
                "title": "政策文件.pdf",
            }
        ],
    }


def test_normalize_legacy_document_material_builds_readable_path_and_clean_warning() -> None:
    normalized = normalize_material_library(
        [
            {
                "source_doc_id": "doc-1",
                "summary": "摘要",
                "metadata": {
                    "extraction_mode": "retrieve_fallback",
                    "warnings": [{"code": "W", "detail": "可读警告", "internal": "drop"}],
                },
                "orig_chunks": [
                    {
                        "chunk_meta": {
                            "chunk_id": "chunk-1",
                            "page_number": 3,
                            "content": "正文",
                        },
                        "path": ["document:doc-1", "page:3"],
                    }
                ],
            }
        ],
        documents=[{"doc_id": "doc-1", "doc_name": "制度说明.pdf"}],
    )

    assert normalized[0]["material_kind"] == "user_document"
    assert normalized[0]["metadata"] == {
        "extraction_mode": "retrieve",
        "queries": [],
        "coverage_complete": False,
        "warnings": [{"code": "W", "message": "可读警告"}],
    }
    assert normalized[0]["orig_chunks"][0] == {
        "chunk_id": "chunk-1",
        "content": "正文",
        "type": "page",
        "path": "制度说明.pdf：第 3 页",
        "page_number": 3,
        "ref": "doc-1",
        "title": "制度说明.pdf",
    }


def test_web_materials_only_accept_urls_from_ark_annotations_and_actual_queries() -> None:
    output = SimpleNamespace(
        model_dump=lambda: {
            "factual": {
                "summary": "可信事实",
                "urls": ["https://news.example/fact", "https://hallucinated.example/item"],
            },
            "creative_reference": {
                "summary": "创作参考",
                "urls": ["https://news.example/fact", "https://media.example/style"],
            },
            "resource": {
                "summary": "annotation 没有摘要，不能进入素材库",
                "urls": ["https://empty.example/result"],
            },
            "popular_culture": {"summary": "模型声称有梗，但 URL 不存在", "urls": []},
        }
    )
    materials = web_search_materials(
        output,
        annotations=[
            {
                "url": "https://news.example/fact",
                "title": "事实来源",
                "site_name": "权威媒体",
                "summary": "原始搜索摘要",
            },
            {
                "url": "https://media.example/style",
                "title": "创作范例",
                "site_name": "内容平台",
                "summary": "范例摘要",
            },
            {
                "url": "https://empty.example/result",
                "title": "缺少摘要的结果",
                "site_name": "空结果",
                "summary": "",
            },
        ],
        queries=["计划外的 Ark 实际 query", "计划外的 Ark 实际 query"],
    )

    assert [item["material_kind"] for item in materials] == ["factual", "creative_reference"]
    assert materials[0]["metadata"]["queries"] == ["计划外的 Ark 实际 query"]
    assert materials[0]["orig_chunks"][0]["path"] == "权威媒体"
    assert materials[0]["orig_chunks"][0]["ref"] == "https://news.example/fact"
    assert [chunk["ref"] for chunk in materials[1]["orig_chunks"]] == ["https://media.example/style"]


def test_cross_category_duplicate_url_keeps_summary_only_reference_material() -> None:
    factual = _material("factual", "fact", "事实")
    creative = _material("creative_reference", "creative", "写法")
    for item in (factual, creative):
        item["orig_chunks"][0]["ref"] = "https://example.com/shared"

    normalized = normalize_material_library([factual, creative])

    assert [item["material_kind"] for item in normalized] == ["factual", "creative_reference"]
    assert len(normalized[0]["orig_chunks"]) == 1
    assert [chunk["content"] for chunk in normalized[1]["orig_chunks"]] == [
        "creative_reference summary"
    ]
    assert normalized[1]["orig_chunks"][0]["ref"] == ""
    assert normalized[1]["summary"] == "creative_reference summary"


def test_web_category_with_only_cross_category_duplicates_keeps_its_summary() -> None:
    output = SimpleNamespace(
        model_dump=lambda: {
            "factual": {"summary": "事实", "urls": ["https://example.com/shared"]},
            "creative_reference": {
                "summary": "可借鉴先讲故事、再解释现象的写法",
                "urls": ["https://example.com/shared"],
            },
            "resource": {"summary": "", "urls": []},
            "popular_culture": {
                "summary": "该梗用于表达意外走红，适合年轻受众",
                "urls": ["https://example.com/shared"],
            },
        }
    )

    materials = web_search_materials(
        output,
        annotations=[
            {
                "url": "https://example.com/shared",
                "title": "共同来源",
                "site_name": "示例站",
                "summary": "同一网页搜索摘要",
            }
        ],
        queries=["实际 query"],
    )

    assert [item["material_kind"] for item in materials] == [
        "factual",
        "creative_reference",
        "popular_culture",
    ]
    assert len(materials[0]["orig_chunks"]) == 1
    assert [chunk["content"] for chunk in materials[1]["orig_chunks"]] == [
        "可借鉴先讲故事、再解释现象的写法"
    ]
    assert [chunk["content"] for chunk in materials[2]["orig_chunks"]] == [
        "该梗用于表达意外走红，适合年轻受众"
    ]
    assert materials[1]["orig_chunks"][0]["ref"] == ""
    assert materials[2]["orig_chunks"][0]["ref"] == ""
    assert materials[1]["summary"] == "可借鉴先讲故事、再解释现象的写法"
    assert materials[2]["summary"] == "该梗用于表达意外走红，适合年轻受众"


def test_reference_material_uses_summary_once_and_keeps_all_source_refs() -> None:
    material = _material("popular_culture", "culture", "搜索摘要 A")
    material["summary"] = "梗的含义、用法和适用语境"
    material["orig_chunks"].append(
        {
            **material["orig_chunks"][0],
            "chunk_id": "culture-chunk-2",
            "content": "搜索摘要 B",
            "ref": "https://example.com/b",
        }
    )

    replaced = replace_reference_chunks_with_summary(material)

    assert [chunk["content"] for chunk in replaced["orig_chunks"]] == [
        "梗的含义、用法和适用语境",
        "",
    ]
    assert [chunk["ref"] for chunk in replaced["orig_chunks"]] == [
        "doc-1",
        "https://example.com/b",
    ]


def test_public_material_sources_are_stable_deduplicated_titles_and_urls() -> None:
    materials = [
        _material("user_document", "doc-a", "正文"),
        {
            **_material("factual", "web-a", "事实"),
            "orig_chunks": [
                {
                    "chunk_id": "web-1",
                    "content": "事实",
                    "type": "web_search",
                    "path": "example.com",
                    "page_number": None,
                    "ref": "https://example.com/a",
                    "title": "网页 A",
                }
            ],
        },
        {
            **_material("resource", "web-b", "资源"),
            "orig_chunks": [
                {
                    "chunk_id": "web-2",
                    "content": "资源",
                    "type": "web_search",
                    "path": "example.com",
                    "page_number": None,
                    "ref": "https://example.com/a",
                    "title": "重复 URL",
                },
                {
                    "chunk_id": "web-3",
                    "content": "资源",
                    "type": "web_search",
                    "path": "images.example",
                    "page_number": None,
                    "ref": "https://images.example/b",
                    "title": "图片候选",
                },
            ],
        },
    ]

    assert material_source_names(materials) == [
        "政策文件.pdf",
        "https://example.com/a",
        "https://images.example/b",
    ]


def test_conflict_note_is_prepended_without_changing_chunk_identity() -> None:
    material = _material("factual", "web-a", "原始事实")
    updated = apply_conflict_notes([material], {"web-a-chunk": "该来源数据不可靠，正确口径为 42%。"})

    chunk = updated[0]["orig_chunks"][0]
    assert chunk["chunk_id"] == "web-a-chunk"
    assert chunk["content"].startswith("[冲突提示] 该来源数据不可靠，正确口径为 42%。")
    assert chunk["content"].endswith("原始事实")


def test_over_budget_drops_low_priority_materials_without_compacting_raw() -> None:
    payload = "长内容" * 2500
    materials = [
        _material("user_document", "raw-doc", payload, mode="raw"),
        _material("factual", "fact", payload),
        _material("creative_reference", "creative", payload),
        _material("resource", "resource", payload),
        _material("popular_culture", "culture", payload),
    ]

    retained, warnings = trim_materials_to_budget(materials, context_window=46_000)

    retained_ids = [item["material_id"] for item in retained]
    assert retained_ids == ["raw-doc"]
    assert retained[0]["orig_chunks"][0]["content"] == payload
    assert [warning["code"] for warning in warnings] == ["MATERIAL_DROPPED_FOR_CONTEXT_BUDGET"] * 4
