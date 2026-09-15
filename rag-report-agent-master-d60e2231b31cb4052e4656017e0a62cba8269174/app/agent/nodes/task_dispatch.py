from app.agent.nodes.outline_generate import outline_generate_node
from app.agent.nodes.outline_modify import outline_modify_node
from app.agent.nodes.qa_generate import qa_generate_node
from app.agent.nodes.report_edit import report_edit_node
from app.agent.nodes.report_write import report_write_node


def _active_intents(state) -> list[str]:
    items = state.get("intents") or []
    intents = [
        "general_qa" if item.get("intent_type") == "chitchat" else item.get("intent_type")
        for item in items
        if item.get("intent_type")
    ]
    return intents or [state.get("intent", "knowledge_qa")]


async def task_dispatch_node(state, config):
    intents = _active_intents(state)
    current = dict(state)
    output = {}

    has_conversation_task = any(intent in intents for intent in ["knowledge_qa", "general_qa"])
    has_report_task = any(intent in intents for intent in ["report_outline", "report_write", "report_edit", "outline_modify"])

    if has_conversation_task or state.get("identity_intro_requested"):
        result = await qa_generate_node(current, config)
        output.update(result)
        current.update(result)

    for intent, node in [
        ("outline_modify", outline_modify_node),
        ("report_outline", outline_generate_node),
        ("report_write", report_write_node),
        ("report_edit", report_edit_node),
    ]:
        if intent not in intents:
            continue
        result = await node(current, config)
        output.update(result)
        current.update(result)

    return output
