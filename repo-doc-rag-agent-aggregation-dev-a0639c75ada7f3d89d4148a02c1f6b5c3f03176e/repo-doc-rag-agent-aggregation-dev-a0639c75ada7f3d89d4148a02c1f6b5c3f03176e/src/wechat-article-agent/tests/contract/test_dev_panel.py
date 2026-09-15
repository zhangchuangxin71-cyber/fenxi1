from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from dev.server import create_dev_app, load_panel_defaults


def test_panel_defaults_are_loaded_from_yaml(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "agent_base_url: https://agent.example.com",
                "user_id: user-from-yaml",
                "kb_id: kb-from-yaml",
                "session_id: session-from-yaml",
            ]
        ),
        encoding="utf-8",
    )

    defaults = load_panel_defaults(config)

    assert str(defaults.agent_base_url) == "https://agent.example.com/"
    assert defaults.user_id == "user-from-yaml"
    assert defaults.kb_id == "kb-from-yaml"
    assert defaults.session_id == "session-from-yaml"


@pytest.mark.contract
async def test_config_endpoint_returns_panel_defaults(tmp_path: Path) -> None:
    app = create_dev_app(
        agent_base_url="https://agent.example.com",
        retrieval_base_url="http://retrieval",
        manifest_file=tmp_path / "documents.jsonl",
        user_id="user-from-yaml",
        kb_id="kb-from-yaml",
        session_id="session-from-yaml",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel") as client:
        response = await client.get("/api/config")

    assert response.status_code == 200
    assert response.json() == {
        "agent_base_url": "https://agent.example.com",
        "retrieval_base_url": "http://retrieval",
        "manifest_file": str(tmp_path / "documents.jsonl"),
        "user_id": "user-from-yaml",
        "kb_id": "kb-from-yaml",
        "session_id": "session-from-yaml",
    }


@pytest.mark.contract
async def test_panel_exposes_distinct_tool_and_skill_views(tmp_path: Path) -> None:
    app = create_dev_app(
        agent_base_url="http://agent",
        retrieval_base_url="http://retrieval",
        manifest_file=tmp_path / "documents.jsonl",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel") as client:
        page = await client.get("/")
        script = await client.get("/static/app.js")

    assert page.status_code == 200
    assert 'data-tab="tool"' in page.text
    assert 'id="tool-view"' in page.text
    assert script.status_code == 200
    assert "renderCallableActivities" in script.text
    assert "renderToolTrace" in script.text
    assert "formatElapsed" in script.text
    assert "renderMaterialSources" in script.text
    assert "renderMaterialConflicts" in script.text
    assert "renderConflictResolutionForm" in script.text
    assert "本次将搜集与" in script.text


@pytest.mark.contract
@respx.mock
async def test_random_documents_resolves_retrieval_metadata(tmp_path: Path) -> None:
    manifest = tmp_path / "documents.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "doc_id": "doc-1",
                "status": "completed",
                "user_id": "user-1",
                "kb_id": "kb-1",
                "file_name": "report.pdf",
            }
        ),
        encoding="utf-8",
    )
    route = respx.post("http://retrieval/rag/v1/documents/meta").mock(
        return_value=httpx.Response(
            200,
            json={
                "documents": [{"doc_id": "doc-1", "doc_name": "report.pdf", "status": "ready"}],
                "missing_doc_ids": [],
            },
        )
    )
    app = create_dev_app(
        agent_base_url="http://agent",
        retrieval_base_url="http://retrieval",
        manifest_file=manifest,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel") as client:
        response = await client.post(
            "/api/random-documents",
            json={
                "manifest_file": str(manifest),
                "user_id": "user-1",
                "kb_id": "kb-1",
                "session_id": "session-1",
                "count": 3,
                "seed": 1,
                "retrieval_base_url": "http://retrieval",
            },
        )

    assert response.status_code == 200
    assert response.json()["doc_ids"] == ["doc-1"]
    assert route.called
    assert route.calls.last.request.content
    assert json.loads(route.calls.last.request.content)["doc_ids"] == ["doc-1"]


@pytest.mark.contract
@respx.mock
async def test_cancel_proxy_calls_product_cancel_endpoint(tmp_path: Path) -> None:
    route = respx.post("http://agent/v1/responses/resp_1/cancel").mock(
        return_value=httpx.Response(200, json={"id": "resp_1", "status": "cancelled"})
    )
    app = create_dev_app(
        agent_base_url="http://agent",
        retrieval_base_url="http://retrieval",
        manifest_file=tmp_path / "documents.jsonl",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel") as client:
        response = await client.post(
            "/api/responses/resp_1/cancel",
            json={"agent_base_url": "http://agent", "session_id": "session-1"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert json.loads(route.calls.last.request.content) == {"context": {"session_id": "session-1"}}
