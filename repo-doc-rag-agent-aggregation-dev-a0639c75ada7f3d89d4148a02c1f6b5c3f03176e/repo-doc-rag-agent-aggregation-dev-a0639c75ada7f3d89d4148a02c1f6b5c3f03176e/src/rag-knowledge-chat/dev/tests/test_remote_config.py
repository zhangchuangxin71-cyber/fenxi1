from __future__ import annotations

import httpx
import pytest

from dev.config import DEFAULT_CONFIG_PATH, load_dev_panel_config
from dev.server import DEFAULT_MANIFEST_DIR, create_dev_app


def test_load_dev_panel_config_reads_remote_chat_url(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        'chat_base_url: "https://chat.example.com:8130/knowledge"\n'
        'api_key: "panel-key"\n',
        encoding="utf-8",
    )

    config = load_dev_panel_config(path)

    assert config.chat_base_url == "https://chat.example.com:8130/knowledge"
    assert config.api_key == "panel-key"


def test_checked_in_config_targets_local_development_chat_without_auth() -> None:
    config = load_dev_panel_config(DEFAULT_CONFIG_PATH)

    assert config.chat_base_url == "http://127.0.0.1:8230"
    assert config.api_key == ""


def test_default_manifest_directory_uses_moved_eval_dataset() -> None:
    assert DEFAULT_MANIFEST_DIR.as_posix() == (
        "/workspace/rag-offline-eval/rag-eval-dataset/manifests"
    )


@pytest.mark.parametrize(
    ("content", "error_field"),
    (
        ("{}\n", "chat_base_url"),
        ('chat_base_url: "ftp://chat.example.com"\napi_key: ""\n', "chat_base_url"),
        ('chat_base_url: "http://"\napi_key: ""\n', "chat_base_url"),
        ('chat_base_url: "http://chat.example.com"\napi_key: []\n', "api_key"),
    ),
)
def test_load_dev_panel_config_rejects_invalid_config(tmp_path, content, error_field) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=error_field):
        load_dev_panel_config(path)


@pytest.mark.asyncio
async def test_dev_proxy_omits_authorization_when_deployed_chat_has_no_inbound_auth(tmp_path) -> None:
    seen: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b"data: [DONE]\n\n",
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_dev_app(
        chat_base_url="https://chat.example.com:8130",
        api_key="",
        manifest_dir=tmp_path,
        http_client=upstream,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://dev",
    ) as client:
        response = await client.post("/api/chat", json={"stream": True})

    await upstream.aclose()
    assert response.status_code == 200
    assert seen == {
        "authorization": None,
        "url": "https://chat.example.com:8130/v1/chat/completions",
    }


@pytest.mark.asyncio
async def test_dev_proxy_reports_remote_connection_failure_as_502(tmp_path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_dev_app(
        chat_base_url="https://chat.example.com:8130",
        api_key="",
        manifest_dir=tmp_path,
        http_client=upstream,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://dev",
    ) as client:
        response = await client.post("/api/chat", json={"stream": True})

    await upstream.aclose()
    assert response.status_code == 502
    assert response.json() == {
        "error": "deployed knowledge-chat service is unavailable",
        "type": "ConnectError",
    }
