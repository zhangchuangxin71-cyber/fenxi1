"""ASGI middleware: assign or echo X-Request-Id on every HTTP request."""

from __future__ import annotations

from videoaudiotext.api.request_context import (
    generate_request_id,
    normalize_request_id,
    reset_request_id,
    set_request_id,
)


class RequestIdMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = normalize_request_id(incoming) or generate_request_id()
        token = set_request_id(request_id)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                out_headers = list(message.get("headers") or [])
                out_headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": out_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_request_id(token)
