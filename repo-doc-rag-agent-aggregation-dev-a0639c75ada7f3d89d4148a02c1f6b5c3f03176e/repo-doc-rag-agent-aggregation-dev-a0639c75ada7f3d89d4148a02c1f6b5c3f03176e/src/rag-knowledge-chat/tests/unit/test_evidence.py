from app.chat.evidence import build_evidence, build_references


def test_evidence_is_deduplicated_and_numbered() -> None:
    chunks = [
        {
            "chunk_id": "c1",
            "document_id": "d1",
            "document_name": "制度.pdf",
            "path": "1",
            "content": "审批需要两级确认。",
            "score": 0.9,
            "source_type": "node",
        },
        {
            "chunk_id": "c1",
            "document_id": "d1",
            "document_name": "制度.pdf",
            "path": "1",
            "content": "重复内容",
            "score": 0.8,
            "source_type": "node",
        },
    ]

    evidence = build_evidence(chunks)

    assert len(evidence.chunks) == 1
    assert evidence.prompt_text.startswith("[1]")
    assert evidence.chunks[0]["citation_index"] == 1


def test_references_only_include_valid_markers() -> None:
    evidence = build_evidence(
        [
            {
                "chunk_id": "c1",
                "document_id": "d1",
                "document_name": "制度.pdf",
                "page_number": 3,
                "path": "1",
                "content": "内容",
                "score": 0.9,
                "source_type": "node",
            }
        ]
    )

    references, invalid = build_references("结论[1]，错误引用[9]。", evidence.chunks)

    assert references == [
        {
            "citation_index": 1,
            "doc_id": "d1",
            "doc_name": "制度.pdf",
            "page_number": 3,
        }
    ]
    assert invalid == [9]


def test_scope_reference_uses_stable_system_location() -> None:
    evidence = build_evidence(
        [
            {
                "chunk_id": "scope:g1:metainfo",
                "document_id": None,
                "document_name": None,
                "page_number": None,
                "path": "request:scope",
                "content": "当前请求范围共包含四篇文档。",
                "source_type": "scope_metadata",
            }
        ]
    )

    references, invalid = build_references("可访问这些文档[1]。", evidence.chunks)

    assert references == [
        {
            "citation_index": 1,
            "doc_id": None,
            "doc_name": "system",
            "page_number": 1,
        }
    ]


def test_scope_reference_keeps_representative_id_and_system_location() -> None:
    evidence = build_evidence(
        [
            {
                "chunk_id": "scope:1:metainfo",
                "document_id": "doc-1",
                "document_ids": ["doc-1", "doc-2"],
                "document_name": "system",
                "page_number": None,
                "path": "request:scope",
                "content": "可访问文档",
                "source_type": "scope_metadata",
            }
        ]
    )

    references, invalid = build_references("可访问文档[1]", evidence.chunks)

    assert invalid == []
    assert references == [
        {
            "citation_index": 1,
            "doc_id": "doc-1",
            "doc_name": "system",
            "page_number": 1,
        }
    ]
    assert invalid == []


def test_scope_evidence_keeps_missing_document_id_empty() -> None:
    evidence = build_evidence(
        [
            {
                "chunk_id": "scope:g1:description:1",
                "document_id": None,
                "document_name": "概览.pdf",
                "path": "request:scope",
                "content": "摘要",
                "source_type": "document_overview",
            }
        ]
    )

    assert evidence.chunks[0]["document_id"] == ""


def test_evidence_preserves_retrieval_hint() -> None:
    evidence = build_evidence(
        [
            {
                "chunk_id": "doc-1:page:3",
                "document_id": "doc-1",
                "document_name": "能源法.pdf",
                "path": "page:3",
                "content": "能源规划包括综合能源规划和分领域能源规划。",
                "hint": "本 chunk 可能用于回答：能源规划类型有哪些。来源：《能源法.pdf》第 3 页",
                "source_type": "page",
            }
        ]
    )

    assert evidence.chunks[0]["hint"] == (
        "本 chunk 可能用于回答：能源规划类型有哪些。来源：《能源法.pdf》第 3 页"
    )
