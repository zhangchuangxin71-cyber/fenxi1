import asyncio

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.agent.graph import build_graph
from app.api.schemas import ChatRequest, normalize_outline
from app.api.sse import EventEmitter
from app.utils.ids import new_conversation_id
from app.utils.safety import safe_run_graph

router = APIRouter(prefix="/agent/v1")
_graph = build_graph()


@router.post("/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    if req.generation_config.enable_images is True and (
        not req.generation_config.image_api_key or not req.generation_config.image_tenant_code
    ):
        raise HTTPException(
            status_code=422,
            detail="generation_config.image_api_key and generation_config.image_tenant_code are required when enable_images=true",
        )

    emitter = EventEmitter()
    conversation_id = new_conversation_id()
    normalized_outline = normalize_outline(req.report_context.current_outline)
    state = {
        "session_id": req.session_id,
        "user_id": req.user_id,
        "conversation_id": conversation_id,
        "kb_id": req.kb_id,
        "query": req.query,
        "history": [item.model_dump() for item in req.history],
        "doc_ids": req.doc_ids,
        "temp_doc_ids": req.temp_doc_ids,
        "retrieval_top_k": req.retrieval_config.top_k,
        "retrieval_search_mode": req.retrieval_config.search_mode,
        "gen_temperature": req.generation_config.temperature,
        "gen_max_tokens": req.generation_config.max_tokens,
        "report_image_enabled": req.generation_config.enable_images,
        "report_image_api_key": req.generation_config.image_api_key,
        "report_image_tenant_code": req.generation_config.image_tenant_code,
        "current_outline": normalized_outline.model_dump() if normalized_outline else req.report_context.current_outline,
        "outline_confirmed": req.report_context.outline_confirmed,
        "current_report": req.report_context.current_report,
    }

    async def runner():
        await emitter.emit_stream_start(conversation_id, req.session_id)
        try:
            final_state = await safe_run_graph(_graph, state, emitter)
            await emitter.emit_stream_end(final_state.get("usage", {}), "complete")
        finally:
            await emitter.close()

    async def event_generator():
        task = asyncio.create_task(runner())
        try:
            async for item in emitter.stream():
                if await request.is_disconnected():
                    break
                yield item
        finally:
            if not task.done():
                task.cancel()

    return EventSourceResponse(event_generator())
