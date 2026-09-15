from app.agent.nodes.common import collect_stream, emit_thinking, get_emitter
from app.agent.nodes.query_utils import AGENT_IDENTITY_REPLY, is_identity_only_question
from app.config import settings


async def chitchat_node(state, config):
    emitter = get_emitter(config)
    await emit_thinking(config, "思考：当前意图是闲聊，不需要检索知识库，直接生成简短回复。")
    if is_identity_only_question(state.get("query", "")):
        if emitter:
            await emitter.emit_text_delta(AGENT_IDENTITY_REPLY)
        return {"answer_text": AGENT_IDENTITY_REPLY, "usage": {}}

    messages = [
        {
            "role": "system",
            "content": (
                "你是广州日报粤传媒和光明实验室联合研发的智能体，当前用户在和你闲聊。"
                "回答要简洁友好。不要自称豆包、字节跳动助手或任何底层模型厂商的产品。"
            ),
        },
        {"role": "user", "content": state["query"]},
    ]
    text, usage = await collect_stream(
        messages,
        model=settings.model_fast,
        temperature=state.get("gen_temperature", 0.7),
        max_tokens=min(state.get("gen_max_tokens", 4096), 1024),
        emitter=emitter,
        content_event="text_delta",
    )
    return {"answer_text": text, "usage": usage}
