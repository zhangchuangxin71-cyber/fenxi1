from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from dev.server import create_dev_app, load_runtime_api_key


def test_default_panel_key_uses_shared_settings(monkeypatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_CHAT_INBOUND_API_KEYS", "dev-panel:shared-key")

    assert load_runtime_api_key() == "shared-key"


@pytest.mark.asyncio
async def test_dev_page_has_three_work_areas_and_does_not_expose_key(tmp_path) -> None:
    (tmp_path / "query_labels.jsonl").write_text(
        '{"query_id":"q1","query_text":"问题","doc_id":"doc-1"}\n'
        '{"query_id":"q2","query_text":"问题二","doc_id":"doc-2"}\n',
        encoding="utf-8",
    )
    app = create_dev_app(
        chat_base_url="http://chat",
        api_key="super-secret",
        manifest_dir=tmp_path,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dev") as client:
        response = await client.get("/")
        config = await client.get("/api/config")

    assert response.status_code == 200
    assert 'id="request-panel"' in response.text
    assert 'id="conversation-panel"' in response.text
    assert 'id="trace-panel"' in response.text
    assert 'id="kb-id" value="eval-rag-kb"' in response.text
    assert 'id="top-k"' in response.text
    assert 'id="max-return-tokens"' in response.text
    assert 'data-trace-view="chat"' in response.text
    assert 'data-trace-view="retrieval"' in response.text
    assert "super-secret" not in response.text
    assert "super-secret" not in config.text


@pytest.mark.asyncio
async def test_dev_script_creates_user_and_assistant_avatars(tmp_path) -> None:
    app = create_dev_app(
        chat_base_url="http://chat",
        api_key="test-key",
        manifest_dir=tmp_path,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://dev",
    ) as client:
        response = await client.get("/static/app.js")

    assert response.status_code == 200
    assert "message-avatar" in response.text
    assert 'role === "user" ? "U" : "AI"' in response.text


@pytest.mark.asyncio
async def test_dev_assets_render_structured_references_and_thinking_state(tmp_path) -> None:
    app = create_dev_app(
        chat_base_url="http://chat",
        api_key="test-key",
        manifest_dir=tmp_path,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://dev",
    ) as client:
        script = await client.get("/static/app.js")
        styles = await client.get("/static/styles.css")

    assert script.status_code == 200
    assert styles.status_code == 200
    for token in (
        "renderReferences",
        "citation_index",
        "reference.doc_name",
        "reference.page_number",
        "chunk_meta",
        "textContent",
        "setThinking",
        "workflow-status",
        "stage-duration",
        "startStageTimer",
        "message-content",
        "insertBefore(node, assistant)",
        "renderRetrievalTrace",
        "retrieval-debug",
        "node_durations_ms",
        "state_summary",
        "debug.input",
        "debug.classification_trace",
        "debug.groups",
        "debug.routes",
        "debug.llm_calls",
        "debug.candidates",
        "检索服务收到的问题（知识库问答改写后）",
        "Robust 分类内部过程",
        "LLM 调用明细",
        "工具调用",
        "工具结果",
        "debugCandidates",
        "chunk?.hint || debugChunk?.hint",
        "previewParts",
        "appendReasoning",
        "reasoning_content",
        "reasoning-details",
        "reasoning-content",
        "思考过程",
    ):
        assert token in script.text
    assert 'stage === "thinking"' in script.text
    for token in (
        ".reference-list",
        ".reference-item",
        ".workflow-status",
        ".workflow-status.active",
        "#connection-state.thinking",
        ".reasoning-details",
        ".reasoning-content",
        "@keyframes",
    ):
        assert token in styles.text


@pytest.mark.asyncio
async def test_dev_generate_request_reads_manifest(tmp_path) -> None:
    rows = [
        {"query_id": "q1", "query_text": "问题", "doc_id": "doc-1"},
        {"query_id": "q2", "query_text": "问题二", "doc_id": "doc-2"},
    ]
    (tmp_path / "query_labels.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    app = create_dev_app(chat_base_url="http://chat", api_key="key", manifest_dir=tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dev") as client:
        response = await client.post(
            "/api/generate-request",
            json={
                "query_id": "q1",
                "user_id": "u",
                "kb_id": "k",
                "noise_count": 1,
                "seed": 3,
            },
        )

    assert response.status_code == 200
    assert response.json()["rag"]["doc_ids"] == ["doc-1", "doc-2"]
    assert response.json()["rag"]["incremental_doc_ids"] == []


@pytest.mark.asyncio
async def test_dev_generate_request_applies_top_k_and_return_budget(tmp_path) -> None:
    rows = [
        {"query_id": "q1", "query_text": "问题", "doc_id": "doc-1"},
        {"query_id": "q2", "query_text": "问题二", "doc_id": "doc-2"},
    ]
    (tmp_path / "query_labels.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    app = create_dev_app(chat_base_url="http://chat", api_key="key", manifest_dir=tmp_path)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dev") as client:
        response = await client.post(
            "/api/generate-request",
            json={
                "query_id": "q1",
                "user_id": "u",
                "kb_id": "k",
                "noise_count": 1,
                "seed": 3,
                "top_k": 9,
                "max_return_tokens": 200000,
            },
        )

    assert response.status_code == 200
    assert response.json()["rag"]["top_k"] == 9
    assert response.json()["rag"]["max_return_tokens"] == 200000


def test_dev_return_token_input_has_no_obsolete_browser_range() -> None:
    html = (Path(__file__).parents[2] / "dev" / "static" / "index.html").read_text(encoding="utf-8")

    field = next(line for line in html.splitlines() if 'id="max-return-tokens"' in line)
    assert 'min="' not in field
    assert 'max="' not in field
    assert 'step="' not in field


@pytest.mark.asyncio
async def test_dev_proxy_injects_key_server_side(tmp_path) -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b'data: {"choices":[]}\n\ndata: [DONE]\n\n',
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_dev_app(
        chat_base_url="http://chat",
        api_key="server-key",
        manifest_dir=tmp_path,
        http_client=upstream,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dev") as client:
        response = await client.post(
            "/api/chat",
            json={
                "model": "rag-knowledge-chat",
                "messages": [{"role": "user", "content": "问题"}],
                "stream": True,
                "rag": {"user_id": "u", "kb_id": "k"},
            },
        )

    await upstream.aclose()
    assert response.status_code == 200
    assert seen["authorization"] == "Bearer server-key"
    assert response.text.endswith("data: [DONE]\n\n")
