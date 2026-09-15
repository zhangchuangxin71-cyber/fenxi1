from app.agent.nodes.query_utils import is_identity_only_question


def _looks_like_obvious_chitchat(query: str) -> bool:
    text = (query or "").strip()
    return text in {"你好", "您好", "hello", "hi", "嗨"} or is_identity_only_question(text)


def route_from_start(state) -> str:
    if _looks_like_obvious_chitchat(state.get("query", "")):
        return "intent"
    if state.get("doc_ids") or state.get("temp_doc_ids"):
        return "need_doc_retrieval"
    return "intent"


def route_after_intent(state) -> str:
    if not (state.get("doc_ids") or state.get("temp_doc_ids")):
        return "task_dispatch"

    retrieval_checked = "retrieved_chunks" in state or "rag_fallback" in state

    intents = {item.get("intent_type") for item in state.get("intents", []) if item.get("intent_type")}
    if intents:
        if intents == {"general_qa"}:
            return "task_dispatch"
        if intents == {"outline_modify"}:
            return "outline_modify"
        if retrieval_checked:
            return "task_dispatch"
        return "need_rag"

    intent = state["intent"]
    if intent == "general_qa":
        return "task_dispatch"
    if intent == "outline_modify":
        return "outline_modify"
    if retrieval_checked:
        return "task_dispatch"
    return "need_rag"


def route_after_retrieve(state) -> str:
    return "intent"
