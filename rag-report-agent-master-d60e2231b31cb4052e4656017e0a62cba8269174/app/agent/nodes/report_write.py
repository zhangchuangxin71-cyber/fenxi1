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
from app.agent.nodes.report_images import (
    collect_report_images,
    format_image_candidates,
    format_image_insertion_plans,
    image_candidates_as_dicts,
    image_insertions_as_dicts,
    plan_image_insertions,
)
from app.agent.prompts.report import REPORT_WRITE_SYSTEM
from app.config import settings


async def report_write_node(state, config):
    emitter = get_emitter(config)
    await emit_step(config, "report_writing", "正在撰写报告...")
    await emit_thinking(
        config,
        "思考：大纲已确认，当前进入整篇报告生成流程，将基于大纲、参考资料和可用图片生成正文。",
    )
    if emitter:
        await emitter.emit_report_start()

    chunks = state.get("retrieved_chunks", [])
    image_enabled = state.get("report_image_enabled")
    image_candidates = await collect_report_images(
        state.get("current_outline"),
        chunks,
        enabled=image_enabled,
        api_key=state.get("report_image_api_key"),
        tenant_code=state.get("report_image_tenant_code"),
    )
    image_insertions = []
    if image_candidates:
        image_insertions = await plan_image_insertions(state.get("current_outline"), chunks, image_candidates)
        await emit_thinking(
            config,
            f"思考：已通过图片 MCP 检索到 {len(image_candidates)} 张候选图片，并规划了 {len(image_insertions)} 个插图位置。",
        )

    messages = [
        {"role": "system", "content": REPORT_WRITE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"大纲：\n{format_outline_for_report(state.get('current_outline'))}\n\n"
                f"参考资料：\n{format_chunks(chunks)}\n\n"
                f"可用图片候选：\n{format_image_candidates(image_candidates)}\n\n"
                f"插图计划：\n{format_image_insertion_plans(image_insertions)}"
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
    return {
        "report_result": clean,
        "used_chunks": used,
        "usage": usage,
        "report_images": image_candidates_as_dicts(image_candidates),
        "report_image_insertions": image_insertions_as_dicts(image_insertions),
    }
