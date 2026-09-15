from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from app.adapter.service import AdapterService
from app.api.application import create_app
from app.api.schemas import ResponseRequest


def test_request_cannot_enable_debug_when_server_switch_is_off() -> None:
    service = object.__new__(AdapterService)
    service.settings = cast(Any, SimpleNamespace(debug_enabled=False))
    request = ResponseRequest.model_validate(
        {
            "input": [{"role": "user", "content": "生成文章"}],
            "context": {"session_id": "session-1", "debug": True},
            "stream": True,
        }
    )

    assert service._runtime_context(request, response_id="resp-1")["debug_enabled"] is False
    assert service._typing_delay(request) == 0


def test_api_docs_remain_available_when_debug_is_off() -> None:
    settings = cast(Any, SimpleNamespace(debug_enabled=False))
    app = create_app(settings)
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/docs" in paths
    assert "/openapi.json" in paths
