from app.agent.nodes.common import (
    collect_stream,
    emit_outline_in_deltas,
    emit_thinking,
    filter_used_chunks,
    format_chunks,
    get_emitter,
    parse_outline_from_text,
)
from app.agent.prompts.outline import OUTLINE_SYSTEM
from app.config import settings


async def outline_generate_node(state, config):
    emitter = get_emitter(config)
    chunks = state.get("retrieved_chunks", [])
    await emit_thinking(config, f"思考：当前任务是生成报告大纲，已获得 {len(chunks)} 条参考内容，接下来组织章节结构。")
    messages = [
        {"role": "system", "content": OUTLINE_SYSTEM},
        {
            "role": "user",
            "content": f"用户需求：{state['query']}\n\n参考资料：\n{format_chunks(chunks)}",
        },
    ]
    text, usage = await collect_stream(
        messages,
        model=settings.model_smart,
        temperature=state.get("gen_temperature", 0.7),
        max_tokens=state.get("gen_max_tokens", 4096),
        emitter=emitter,
    )
    outline = parse_outline_from_text(text)
    await emit_outline_in_deltas(emitter, text)
    if emitter:
        await emitter.emit_outline_complete(outline)
    used = filter_used_chunks(chunks, text)
    if emitter:
        await emitter.emit_references(used)
    return {"outline_result": outline, "used_chunks": used, "usage": usage}
