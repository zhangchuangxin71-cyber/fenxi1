from app.agent.nodes.common import (
    collect_stream,
    emit_step,
    emit_thinking,
    extract_used_ids,
    filter_used_chunks,
    format_chunks,
    format_outline_for_report,
    get_emitter,
)
from app.agent.prompts.report import REPORT_EDIT_SYSTEM
from app.config import settings


async def report_edit_node(state, config):
    emitter = get_emitter(config)
    await emit_step(config, "report_writing", "正在改写报告...")
    await emit_thinking(config, "思考：当前已有完整报告，用户要求整体编辑，因此基于原报告、修改指令和参考资料输出完整新版报告。")
    if emitter:
        await emitter.emit_report_start()
    chunks = state.get("retrieved_chunks", [])
    messages = [
        {"role": "system", "content": REPORT_EDIT_SYSTEM},
        {
            "role": "user",
            "content": (
                f"原报告：\n{state.get('current_report') or ''}\n\n"
                f"修改要求：{state['query']}\n\n"
                f"当前大纲：\n{format_outline_for_report(state.get('current_outline'))}\n\n"
                f"参考资料：\n{format_chunks(chunks)}"
            ),
        },
    ]
    text, usage = await collect_stream(
        messages,
        model=settings.model_smart,
        temperature=state.get("gen_temperature", 0.7),
        max_tokens=max(state.get("gen_max_tokens", 4096), 8192),
        emitter=emitter,
        content_event="report_text_delta",
    )
    clean, explicit_ids = extract_used_ids(text)
    used = filter_used_chunks(chunks, clean, explicit_ids)
    if emitter:
        await emitter.emit_references(used)
        await emitter.emit_report_end()
    return {"report_result": clean, "used_chunks": used, "usage": usage}
