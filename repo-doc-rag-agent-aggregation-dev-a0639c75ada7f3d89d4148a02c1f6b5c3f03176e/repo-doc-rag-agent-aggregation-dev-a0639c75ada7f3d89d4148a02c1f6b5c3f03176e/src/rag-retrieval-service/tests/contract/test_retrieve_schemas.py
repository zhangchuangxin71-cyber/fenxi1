from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    DocumentMetaRequest,
    DocumentRouteRequest,
    RetrievalWarning,
    RetrievedChunk,
    RetrieveRequest,
)


def test_document_meta_request_accepts_mixed_scope_and_prefers_permanent_ids() -> None:
    request = DocumentMetaRequest(
        user_id="user-1",
        kb_id="kb-1",
        session_id="session-1",
        doc_ids=["permanent", "shared", "shared"],
        temp_doc_ids=["temporary", "shared", "temporary"],
    )

    assert request.doc_ids == ["permanent", "shared"]
    assert request.temp_doc_ids == ["temporary"]


def test_document_meta_request_accepts_temporary_documents_without_a_session() -> None:
    request = DocumentMetaRequest(
        user_id="user-1",
        kb_id="kb-1",
        temp_doc_ids=["temporary"],
    )

    assert request.session_id is None
    assert request.temp_doc_ids == ["temporary"]


def test_document_meta_request_requires_at_least_one_document() -> None:
    with pytest.raises(ValidationError, match="document"):
        DocumentMetaRequest(user_id="user-1", kb_id="kb-1")


def test_request_keeps_legacy_fields_and_adds_return_token_budget() -> None:
    request = RetrieveRequest(user_id="user-1", kb_id="kb-1", query="营业额是多少")

    assert request.search_mode == "hybrid"
    assert request.top_k == 5
    assert request.max_return_tokens is None
    assert request.doc_ids == []
    assert request.temp_doc_ids == []
    assert request.options.include_debug is False


def test_request_return_token_budget_defers_upper_bound_to_runtime_settings() -> None:
    request = RetrieveRequest(
        user_id="user-1",
        kb_id="kb-1",
        query="详细总结文档",
        max_return_tokens=300_000,
    )

    assert request.max_return_tokens == 300_000
    with pytest.raises(ValidationError):
        RetrieveRequest(
            user_id="user-1",
            kb_id="kb-1",
            query="详细总结文档",
            max_return_tokens=127,
        )


def test_request_deduplicates_document_ids_across_permanent_and_temporary_scope() -> None:
    request = RetrieveRequest(
        user_id="u",
        kb_id="kb",
        query="问题",
        doc_ids=["d1", "d1"],
        temp_doc_ids=["d1", "d2", "d2"],
    )

    assert request.doc_ids == ["d1"]
    assert request.temp_doc_ids == ["d2"]


def test_request_rejects_unknown_fields_and_blank_query() -> None:
    with pytest.raises(ValidationError):
        RetrieveRequest(user_id="user-1", kb_id="kb-1", query="   ")
    with pytest.raises(ValidationError):
        RetrieveRequest(user_id="user-1", kb_id="kb-1", query="x", unknown=True)


def test_request_accepts_preprocessed_query_list_and_exposes_one_text_view() -> None:
    request = RetrieveRequest(
        user_id="user-1",
        kb_id="kb-1",
        query=[" 你能看到哪些文档？ ", "A 公司营业额是多少？", "A 公司营业额是多少？"],
    )

    assert request.query == ["你能看到哪些文档？", "A 公司营业额是多少？"]
    assert request.query_text == "你能看到哪些文档？；A 公司营业额是多少？"


@pytest.mark.parametrize(
    "query",
    [
        [],
        ["问题", "   "],
        ["问题", 1],
        ["x" * 8001, "y" * 8000],
    ],
)
def test_request_rejects_invalid_preprocessed_query_list(query: object) -> None:
    with pytest.raises(ValidationError):
        RetrieveRequest(user_id="user-1", kb_id="kb-1", query=query)


def test_scope_metadata_chunk_can_reference_multiple_documents() -> None:
    chunk = RetrievedChunk(
        chunk_id="scope:g0001",
        document_id=None,
        document_ids=["doc-1", "doc-2"],
        document_name=None,
        path="request:scope",
        content="共两篇文档",
        hint="所有文档均可访问",
        score=None,
        source_type="scope_metadata",
        document_meta=None,
        chunk_meta={},
    )

    assert chunk.document_id is None
    assert chunk.document_ids == ["doc-1", "doc-2"]


def test_retrieved_page_chunk_exposes_page_number_at_top_level() -> None:
    chunk = RetrievedChunk(
        chunk_id="doc-1:page:12",
        document_id="doc-1",
        document_ids=["doc-1"],
        document_name="制度.pdf",
        page_number=12,
        path="document:doc-1:page:12",
        content="原文",
        hint="来源：《制度.pdf》第 12 页",
        score=1.0,
        source_type="page",
        document_meta=None,
        chunk_meta={},
    )

    assert chunk.page_number == 12


def test_warning_is_available_without_debug() -> None:
    warning = RetrievalWarning(
        code="SCOPE_ENUMERATION_TRUNCATED",
        message="仅列举四篇文档",
        affected_group_refs=["g0001"],
    )

    assert warning.retryable is False
    assert warning.affected_group_refs == ["g0001"]


def test_document_route_request_defaults_to_llm_only_and_preserves_criteria_order() -> None:
    request = DocumentRouteRequest.model_validate(
        {
            "user_id": " u1 ",
            "kb_id": " kb1 ",
            "doc_ids": ["d1", "d1"],
            "temp_doc_ids": ["d1", "d2"],
            "criteria": [
                {
                    "queries": [" 新能源汽车 ", "新能源汽车"],
                    "target_docs_description": " 产业趋势材料 ",
                    "target_docs_keywords": ["政策", "政策"],
                },
                {
                    "queries": ["市场规模"],
                    "target_docs_description": "市场数据",
                    "target_docs_keywords": [],
                },
            ],
        }
    )

    assert request.keyword_prefilter is False
    assert request.user_id == "u1"
    assert request.doc_ids == ["d1"]
    assert request.temp_doc_ids == ["d2"]
    assert request.criteria[0].queries == ["新能源汽车"]
    assert [criterion.target_docs_description for criterion in request.criteria] == [
        "产业趋势材料",
        "市场数据",
    ]
