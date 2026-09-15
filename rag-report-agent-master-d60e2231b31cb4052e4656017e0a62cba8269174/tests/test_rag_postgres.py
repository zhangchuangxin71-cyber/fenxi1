import pytest

from app.rag.postgres_retriever import (
    CandidateChunk,
    _fetch_candidate_chunks_sync,
    rank_candidates,
    retrieve,
)


def test_rank_candidates_prefers_direct_keyword_match():
    chunks = [
        CandidateChunk(
            chunk_id="doc_b:node:1",
            document_id="doc_b",
            document_name="旅游手册.docx",
            path="node:1",
            content="光明街道有许多旅游景点和美食。",
            metadata_text="旅游手册 光明景点",
        ),
        CandidateChunk(
            chunk_id="doc_a:node:2",
            document_id="doc_a",
            document_name="马飞报告.pdf",
            path="node:2",
            content="团队负责人为研究员马飞，清华大学博士，曾就职华为。",
            metadata_text="迈向以人为中心的世界模型 马飞",
        ),
    ]

    ranked = rank_candidates("马飞是谁", chunks, top_k=1)

    assert ranked[0].document_id == "doc_a"
    assert "马飞" in ranked[0].content


def test_rank_candidates_boosts_profile_chunks_for_who_question():
    chunks = [
        CandidateChunk(
            chunk_id="doc_a:node:cover",
            document_id="doc_a",
            document_name="马飞报告.pdf",
            path="node:cover",
            content="汇报人：马飞 2026年2月10日",
            metadata_text="马飞报告",
        ),
        CandidateChunk(
            chunk_id="doc_a:node:profile",
            document_id="doc_a",
            document_name="马飞报告.pdf",
            path="node:profile",
            content="团队负责人为研究员马飞，清华大学博士，曾就职华为。",
            metadata_text="马飞报告",
        ),
    ]

    ranked = rank_candidates("马飞是谁", chunks, top_k=1)

    assert ranked[0].chunk_id == "doc_a:node:profile"


def test_rank_candidates_can_cover_multiple_documents_for_compound_query():
    chunks = [
        CandidateChunk(
            chunk_id="doc_hotel:node:1",
            document_id="doc_hotel",
            document_name="旅游手册.docx",
            path="node:1",
            content="光明区酒店 酒店 酒店 商务酒店 旅游住宿。",
            metadata_text="光明区旅游手册 酒店",
        ),
        CandidateChunk(
            chunk_id="doc_hotel:node:2",
            document_id="doc_hotel",
            document_name="旅游手册.docx",
            path="node:2",
            content="光明区酒店推荐，适合会议和旅游。",
            metadata_text="光明区旅游手册 酒店",
        ),
        CandidateChunk(
            chunk_id="doc_ma:node:1",
            document_id="doc_ma",
            document_name="马飞报告.pdf",
            path="node:1",
            content="马博士是光明实验室媒体智能团队负责人。",
            metadata_text="马博士资料",
        ),
    ]

    ranked = rank_candidates(
        "介绍马博士，然后推荐光明区酒店",
        chunks,
        top_k=2,
        ensure_document_coverage=True,
    )

    assert {chunk.document_id for chunk in ranked} == {"doc_hotel", "doc_ma"}


def test_fetch_candidate_chunks_filters_by_kb_without_user_binding(monkeypatch):
    captured = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params):
            captured.append((sql, params))

        def fetchall(self):
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr("app.rag.postgres_retriever.psycopg.connect", lambda dsn: FakeConnection())

    chunks = _fetch_candidate_chunks_sync(
        kb_id="kb-demo",
        user_id="user-demo",
        query="马飞是谁",
        session_id="sess",
        doc_ids=["doc_a"],
        temp_doc_ids=None,
        max_candidates=10,
    )

    assert chunks == []
    assert len(captured) == 2
    for sql, params in captured:
        assert "b.kb_id = %s" in sql
        assert "b.user_id = %s" not in sql
        assert params == ("kb-demo", ["doc_a"], 10)


@pytest.mark.asyncio
async def test_retrieve_maps_ranked_candidates_to_provided_chunks(monkeypatch):
    async def fake_fetch(**kwargs):
        assert kwargs["user_id"] == "user-demo"
        assert kwargs["doc_ids"] == ["doc_a"]
        return [
            CandidateChunk(
                chunk_id="doc_a:node:2",
                document_id="doc_a",
                document_name="马飞报告.pdf",
                path="node:2",
                content="团队负责人为研究员马飞，清华大学博士，曾就职华为。",
                metadata_text="马飞报告",
            )
        ]

    monkeypatch.setattr("app.rag.postgres_retriever.fetch_candidate_chunks", fake_fetch)

    chunks = await retrieve(
        kb_id="kb-demo",
        user_id="user-demo",
        query="马飞是谁",
        session_id="sess",
        doc_ids=["doc_a"],
        top_k=3,
    )

    assert len(chunks) == 1
    assert chunks[0].chunk_id == "doc_a:node:2"
    assert chunks[0].document_name == "马飞报告.pdf"
