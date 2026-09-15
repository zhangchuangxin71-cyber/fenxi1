from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from app.api.contracts import ChatCompletionRequest, ChatCompletionStreamChunk
from app.api.dependencies import ServiceContainer
from app.platform.errors import ApiError
from app.platform.settings import CHAT_MODEL_ALIAS
from app.platform.trace import TraceCollector
from app.streaming.openai_sse import StreamContext, encode_done, encode_event

router = APIRouter()


def _services(request: Request) -> ServiceContainer:
    return request.app.state.services


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post(
    "/v1/chat/completions",
    responses={
        200: {
            "model": ChatCompletionStreamChunk,
            "description": (
                "SSE 流。每个 data 帧是一个 ChatCompletionStreamChunk；最后以 data: [DONE] 结束。"
            ),
        }
    },
)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> StreamingResponse:
    services = _services(request)
    if (
        body.rag.max_return_tokens is not None
        and body.rag.max_return_tokens > services.settings.rag_max_return_tokens
    ):
        raise ApiError(
            400,
            "invalid_request",
            "request validation failed",
            details={
                "validation_errors": [
                    {
                        "location": ["body", "rag", "max_return_tokens"],
                        "message": (
                            f"Input should be less than or equal to {services.settings.rag_max_return_tokens}"
                        ),
                        "type": "less_than_equal",
                    }
                ]
            },
        )
    if body.model != CHAT_MODEL_ALIAS:
        raise ApiError(
            400,
            "unsupported_model",
            f"model must be {CHAT_MODEL_ALIAS}",
        )
    auth = services.authenticator.authenticate(authorization)
    await services.request_limiter.acquire(f"{auth.fingerprint}:{body.rag.user_id}")
    # The first Ark call is guaranteed, so reserve it before opening SSE.
    await services.ark_limiter.acquire("ark")

    request_id = request.state.request_id
    context = StreamContext(
        completion_id=f"chatcmpl_{uuid.uuid4().hex}",
        created=int(time.time()),
        model=CHAT_MODEL_ALIAS,
        request_id=request_id,
    )
    debug_enabled = services.settings.debug_enabled and body.rag.include_debug
    trace = TraceCollector(enabled=debug_enabled)
    trace.record("request_validated")

    async def event_stream():
        async for event in services.orchestrator.run(
            request=body,
            request_id=request_id,
            trace=trace,
            first_ark_permit_acquired=True,
        ):
            yield encode_event(event, context)
        yield encode_done()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
