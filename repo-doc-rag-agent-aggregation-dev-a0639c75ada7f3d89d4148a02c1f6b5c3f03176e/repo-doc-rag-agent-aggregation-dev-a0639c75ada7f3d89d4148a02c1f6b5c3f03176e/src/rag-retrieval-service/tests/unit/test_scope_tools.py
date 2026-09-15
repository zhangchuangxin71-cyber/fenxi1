from __future__ import annotations

import json

from app.db.repositories import DocumentProfile
from app.tools.metadata import (
    SCOPE_ENUMERATION_LIMIT,
    build_scope_descriptions,
    build_scope_metainfo,
)


def _profiles(count: int) -> list[DocumentProfile]:
    return [
        DocumentProfile(
            doc_id=f"doc-{index}",
            doc_name=f"文档{index}",
            doc_description=f"摘要{index}",
            page_count=index + 1,
            node_count=2,
        )
        for index in range(count)
    ]


def test_scope_metainfo_lists_at_most_ten_and_records_listed_documents() -> None:
    result = build_scope_metainfo(
        profiles=_profiles(12),
        group_ref="g0001",
        questions=["你能看到哪些文档"],
        fields=["doc_name", "page_count"],
    )

    assert SCOPE_ENUMERATION_LIMIT == 10
    assert result.chunk.document_id == "doc-0"
    assert result.chunk.document_ids == [f"doc-{index}" for index in range(10)]
    assert result.chunk.document_name == "system"
    assert result.chunk.chunk_id == "scope:g0001:metainfo"
    entries = json.loads(result.chunk.content.splitlines()[1])
    assert entries[0] == {"doc_name": "文档0", "page_count": 1}
    assert entries[-1] == {"doc_name": "文档9", "page_count": 10}
    assert "所有文档均可访问" in result.chunk.content
    assert "具体访问哪一篇" in result.chunk.hint
    assert [warning.code for warning in result.warnings] == ["SCOPE_ENUMERATION_TRUNCATED"]


def test_scope_descriptions_returns_only_ten_chunks_with_document_identity() -> None:
    result = build_scope_descriptions(
        profiles=_profiles(12),
        group_ref="g0002",
        questions=["总结上述所有文档"],
    )

    assert len(result.chunks) == 10
    assert all(chunk.source_type == "document_overview" for chunk in result.chunks)
    serialized = "".join(chunk.model_dump_json() for chunk in result.chunks)
    assert [chunk.document_id for chunk in result.chunks] == [
        f"doc-{index}" for index in range(10)
    ]
    assert [chunk.document_ids for chunk in result.chunks] == [
        [f"doc-{index}"] for index in range(10)
    ]
    assert all(profile.doc_id not in serialized for profile in _profiles(12)[10:])
    assert [chunk.chunk_id for chunk in result.chunks] == [
        f"scope:g0002:description:{index}" for index in range(1, 11)
    ]
    assert result.coverage_complete is False
