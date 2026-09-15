from app.agent.nodes.common import (
    collect_stream,
    emit_step,
    emit_thinking,
    extract_used_ids,
    filter_used_chunks,
    format_chunks,
    format_query_groups,
    get_emitter,
)
from app.agent.prompts.qa import GENERAL_QA_SYSTEM, QA_GROUPED_USER_TEMPLATE, QA_SYSTEM, QA_USER_TEMPLATE
from app.config import settings


async def qa_generate_node(state, config):
    emitter = get_emitter(config)
    await emit_step(config, "generating", "正在生成回答...")
    chunks = state.get("retrieved_chunks", [])
    groups = state.get("retrieved_query_groups") or []
    query = state.get("effective_query") or state["query"]

    if chunks:
        doc_names = "、".join(sorted({chunk.document_name for chunk in chunks}))
        await emit_thinking(config, f"思考：已经拿到相关资料，来源文档包括《{doc_names}》，接下来基于这些内容生成回答。")
    else:
        await emit_thinking(config, "思考：没有找到直接相关资料，将基于模型通识谨慎回答。")
    prefix = ""
    if not chunks:
        prefix = "（抱歉，知识库中未找到直接相关的内容，以下为基于模型常识的回答，仅供参考。）\n\n"
        if emitter:
            await emitter.emit_text_delta(prefix)

    if chunks:
        user_content = (
            QA_GROUPED_USER_TEMPLATE.format(
                groups_formatted=format_query_groups(groups),
                original_query=state["query"],
            )
            if groups
            else QA_USER_TEMPLATE.format(
                chunks_formatted=format_chunks(chunks),
                query=query,
            )
        )
        messages = [
            {"role": "system", "content": QA_SYSTEM},
            {"role": "user", "content": user_content},
        ]
    else:
        messages = [
            {"role": "system", "content": GENERAL_QA_SYSTEM},
            {"role": "user", "content": state["query"]},
        ]
    raw_text, usage = await collect_stream(
        messages,
        model=settings.model_smart,
        temperature=state.get("gen_temperature", 0.7),
        max_tokens=state.get("gen_max_tokens", 4096),
        emitter=emitter,
        content_event="text_delta",
    )
    clean, explicit_ids = extract_used_ids(raw_text)
    full_text = prefix + clean
    used = filter_used_chunks(chunks, full_text, explicit_ids)
    if emitter:
        await emitter.emit_references(used)
    return {"answer_text": full_text, "used_chunks": used, "usage": usage}
