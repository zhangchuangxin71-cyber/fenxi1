from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.api.schemas import RetrieveOptions, RetrieveRequest
from app.config.settings import Settings
from app.container import AppContainer


def _settings() -> Settings:
    path = os.getenv("RAG_TEST_ENV_FILE", "")
    if not path or not Path(path).is_file():
        pytest.skip("live retrieval environment file is not configured")
    values = dotenv_values(path)
    required = {
        "postgres_dsn": values.get("POSTGRES_DSN"),
        "ark_api_key": values.get("ARK_API_KEY"),
        "ark_base_url": values.get("ARK_BASE_URL"),
        "rag_llm_model": values.get("RAG_LLM_MODEL"),
    }
    if not all(required.values()):
        pytest.skip("database or Ark configuration is incomplete")
    return Settings(
        _env_file=None,
        **required,
        rag_rate_limit_enabled=False,
        rag_debug_enabled=True,
        request_soft_deadline_seconds=45,
        request_hard_deadline_seconds=60,
        request_finalization_reserve_seconds=3,
        rag_default_return_tokens=2048,
        rag_max_return_tokens=8192,
    )


@pytest.mark.live_db
@pytest.mark.live_llm
@pytest.mark.asyncio
async def test_live_end_to_end_scope_direct_focused_and_broad() -> None:
    container = AppContainer.build(_settings())
    await container.start()
    try:

        def select_document_id() -> str:
            with container.pool.transaction(read_only=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT doc_id::text
                        FROM documents
                        WHERE status = 'ready' AND page_count BETWEEN 1 AND 20
                          AND node_count > 0
                        ORDER BY doc_id
                        LIMIT 1
                        """
                    )
                    row = cursor.fetchone()
            assert row is not None
            return str(row[0])

        document_id = await container.db_executor.run(select_document_id)
        profiles, _ = await container.db_executor.run(
            container.repository.fetch_scope,
            user_id="eval-user",
            kb_id="eval-rag-kb",
            doc_ids=[document_id],
            temp_doc_ids=[],
            session_id=None,
        )
        profile = profiles[0]
        first_pages = await container.db_executor.run(
            container.repository.fetch_pages,
            user_id="eval-user",
            kb_id="eval-rag-kb",
            doc_id=profile.doc_id,
            pages=[1],
        )
        assert first_pages and first_pages[0].content
        terms = re.findall(r"[\u4e00-\u9fff]{4,}", first_pages[0].content)
        focused_term = terms[0][:8] if terms else profile.doc_name[:8]
        common = {
            "user_id": "eval-user",
            "kb_id": "eval-rag-kb",
            "doc_ids": [profile.doc_id],
            "top_k": 3,
            "max_return_tokens": 2048,
            "search_mode": "semantic",
            "options": RetrieveOptions(include_debug=True),
        }
        scope = await container.engine.retrieve(
            RetrieveRequest(**{**common, "query": "你能看到哪些文档"})
        )
        direct = await container.engine.retrieve(
            RetrieveRequest(**{**common, "query": "这篇文档有几页"})
        )
        focused = await container.engine.retrieve(
            RetrieveRequest(**{**common, "query": f"这篇文档中关于{focused_term}的具体内容是什么"})
        )
        broad = await container.engine.retrieve(
            RetrieveRequest(**{**common, "query": "请详细总结这篇文档"})
        )
    finally:
        await container.close()

    assert scope.debug.state_summary["groups_by_category"]["scope_direct"] >= 1
    assert direct.debug.state_summary["groups_by_category"]["routed_direct"] >= 1
    assert focused.debug.state_summary["groups_by_category"]["routed_focused"] >= 1
    assert broad.debug.state_summary["groups_by_category"]["routed_broad"] >= 1
    assert all(
        response.usage.actual_mode == "semantic" for response in (scope, direct, focused, broad)
    )
    assert all(
        response.usage.llm_request_count >= 1 for response in (scope, direct, focused, broad)
    )
    responses = {
        "scope_direct": scope,
        "routed_direct": direct,
        "routed_focused": focused,
        "routed_broad": broad,
    }
    diagnostics = {
        name: {
            "chunk_count": len(response.chunks),
            "warning_codes": [warning.code for warning in response.warnings],
            "state_summary": response.debug.state_summary,
        }
        for name, response in responses.items()
    }
    assert all(response.chunks for response in responses.values()), diagnostics
    assert all(
        chunk.content for response in (scope, direct, focused, broad) for chunk in response.chunks
    )
