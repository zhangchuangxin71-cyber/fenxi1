import json

from app.agent.nodes.common import (
    collect_stream,
    emit_outline_in_deltas,
    emit_thinking,
    get_emitter,
    parse_outline_from_text,
)
from app.agent.prompts.outline import OUTLINE_MODIFY_SYSTEM
from app.config import settings


async def outline_modify_node(state, config):
    emitter = get_emitter(config)
    await emit_thinking(config, "思考：当前已有未确认大纲，用户请求修改大纲，因此不检索知识库，直接基于 current_outline 生成新版结构。")
    messages = [
        {"role": "system", "content": OUTLINE_MODIFY_SYSTEM},
        {
            "role": "user",
            "content": (
                f"旧大纲：\n{json.dumps(state.get('current_outline') or {}, ensure_ascii=False)}\n\n"
                f"修改要求：{state['query']}"
            ),
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
    return {"outline_result": outline, "usage": usage}
