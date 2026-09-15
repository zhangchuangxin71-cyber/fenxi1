from app.agent.nodes.common import emit_intents, emit_step, emit_thinking, parse_json_object
from app.agent.nodes.query_utils import is_identity_only_question, split_identity_request
from app.agent.prompts.intent import (
    INTENT_CONFIRMED_SYSTEM,
    INTENT_FRESH_SYSTEM,
    INTENT_WITH_OUTLINE_SYSTEM,
    INTENT_WITH_REPORT_SYSTEM,
)
from app.config import settings
from app.llm.registry import get_provider

VALID_INTENTS = {
    "general_qa",
    "knowledge_qa",
    "report_outline",
    "outline_modify",
    "report_write",
    "report_edit",
}

INTENT_ORDER = [
    "general_qa",
    "knowledge_qa",
    "report_outline",
    "outline_modify",
    "report_write",
    "report_edit",
]

INTENT_LABELS = {
    "general_qa": "通识问答",
    "knowledge_qa": "知识问答",
    "report_outline": "报告大纲生成",
    "outline_modify": "大纲修改",
    "report_write": "报告撰写",
    "report_edit": "报告编辑",
}


def _looks_like_chitchat(query: str) -> bool:
    text = (query or "").strip()
    return text in {"你好", "您好", "hello", "hi", "嗨"} or is_identity_only_question(text)


def _looks_like_report_request(query: str) -> bool:
    text = query or ""

    report_words = ["报告", "文章", "文档", "材料", "分析稿", "总结稿", "综述"]
    action_words = ["写", "撰写", "生成", "输出", "形成", "整理成", "做", "制作", "起草", "拟一份", "帮我写", "帮我生成"]

    if any(action in text for action in action_words) and any(word in text for word in report_words):
        return True

    outline_words = ["大纲", "提纲", "目录结构", "章节结构"]
    if any(action in text for action in action_words) and any(word in text for word in outline_words):
        return True

    return False


def _looks_like_document_summary_qa(query: str) -> bool:
    text = (query or "").strip()
    report_artifact_words = ["报告", "大纲", "提纲", "文章", "文档", "材料", "综述"]
    summary_qa_words = ["总结一下", "总结下", "概括一下", "概括下", "概述一下", "归纳一下", "主要内容", "讲了什么"]
    return not any(word in text for word in report_artifact_words) and any(word in text for word in summary_qa_words)


def _looks_like_write_request(query: str) -> bool:
    return any(token in query for token in ["开始写", "开始生成", "生成报告", "确认", "继续写"])


def _looks_like_side_qa_request(query: str) -> bool:
    text = query or ""
    side_markers = ["顺便", "同时", "另外", "再", "以及", "并且", "回答", "介绍", "问一下", "说说"]
    qa_markers = ["回答", "介绍", "是谁", "什么", "经历", "成果", "推荐", "怎么", "如何", "多少", "哪里"]
    return any(marker in text for marker in side_markers) and any(marker in text for marker in qa_markers)


def _looks_like_outline_modify(query: str) -> bool:
    return any(token in query for token in ["删掉", "删除", "增加", "新增", "调整", "修改", "改成"])


def _intent_item(intent_type: str, confidence: float) -> dict:
    if intent_type == "chitchat":
        intent_type = "general_qa"
    return {"intent_type": intent_type, "confidence": round(float(confidence), 2)}


def _normalize_intents(parsed: dict, default: str, default_confidence: float = 0.9) -> list[dict]:
    raw = parsed.get("intents")
    items: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            intent_type = item.get("intent_type") or item.get("intent")
            if intent_type == "chitchat":
                intent_type = "general_qa"
            if intent_type in VALID_INTENTS:
                items.append(_intent_item(intent_type, item.get("confidence", default_confidence)))
    else:
        intent_type = parsed.get("intent")
        if intent_type == "chitchat":
            intent_type = "general_qa"
        if intent_type in VALID_INTENTS:
            items.append(_intent_item(intent_type, parsed.get("confidence", default_confidence)))

    if not items:
        items = [_intent_item(default, default_confidence)]
    return _dedupe_intents(items)


def _dedupe_intents(items: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for item in items:
        intent_type = item.get("intent_type")
        if intent_type not in VALID_INTENTS:
            continue
        if intent_type not in best or item.get("confidence", 0) > best[intent_type].get("confidence", 0):
            best[intent_type] = _intent_item(intent_type, item.get("confidence", 0.9))
    return [best[intent] for intent in INTENT_ORDER if intent in best]


def _primary_intent(intents: list[dict]) -> str:
    for intent in ["report_edit", "report_write", "outline_modify", "report_outline", "knowledge_qa", "general_qa"]:
        if any(item["intent_type"] == intent for item in intents):
            return intent
    return "knowledge_qa"


def _downgrade_knowledge_to_general_without_docs(intents: list[dict], has_docs: bool) -> list[dict]:
    if has_docs:
        return intents
    converted = []
    for item in intents:
        if item.get("intent_type") == "knowledge_qa":
            converted.append(_intent_item("general_qa", item.get("confidence", 0.9)))
        else:
            converted.append(item)
    return _dedupe_intents(converted)



async def _classify(state, system_prompt: str, default: str) -> list[dict]:
    provider = get_provider("doubao")
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"用户消息：{state.get('query', '')}\n历史：{state.get('history', [])}",
        },
    ]
    result = await provider.chat(messages, model=settings.model_fast, temperature=0, max_tokens=256)
    parsed = parse_json_object(result.get("text", ""), {"intent": default})
    return _normalize_intents(parsed, default)


def _with_heuristic_intents(query: str, intents: list[dict], *, report: bool, outline: bool, confirmed: bool) -> list[dict]:
    extra: list[dict] = []
    if _looks_like_chitchat(query):
        extra.append(_intent_item("general_qa", 0.8))
    if report and not _looks_like_chitchat(query):
        extra.append(_intent_item("report_edit", 0.9))
    elif outline and confirmed and _looks_like_write_request(query):
        extra.append(_intent_item("report_write", 0.95))
    elif outline and not confirmed:
        if _looks_like_write_request(query):
            extra.append(_intent_item("report_write", 0.95))
        elif _looks_like_outline_modify(query):
            extra.append(_intent_item("outline_modify", 0.95))
    return _dedupe_intents([*intents, *extra])


async def _classify_after_document_retrieval(state, config, query: str, query_for_intent: str) -> list[dict]:
    outline = state.get("current_outline")
    confirmed = state.get("outline_confirmed", False)
    report = state.get("current_report")
    has_related_content = bool(state.get("retrieved_chunks"))

    if has_related_content:
        await emit_thinking(
            config,
            "思考：本轮已先检索候选文档并找到相关内容，接下来只判断这些内容应用于问答还是报告生成。",
        )
    else:
        await emit_thinking(
            config,
            "思考：本轮已先检索候选文档，但没有找到直接相关内容，将按普通对话/普通报告生成处理。",
        )

    if _looks_like_chitchat(query):
        return [_intent_item("general_qa", 0.95)]

    if outline and confirmed and _looks_like_write_request(query_for_intent):
        return [_intent_item("report_write", 0.95)]

    if outline and not confirmed and _looks_like_outline_modify(query_for_intent) and not _looks_like_report_request(query_for_intent):
        return [_intent_item("outline_modify", 0.95)]

    if report:
        system_prompt = INTENT_WITH_REPORT_SYSTEM
    elif outline and confirmed:
        system_prompt = INTENT_CONFIRMED_SYSTEM
    elif outline and not confirmed:
        system_prompt = INTENT_WITH_OUTLINE_SYSTEM
    else:
        system_prompt = INTENT_FRESH_SYSTEM

    if _looks_like_report_request(query_for_intent):
        default_intent = "report_outline"
    elif has_related_content:
        default_intent = "knowledge_qa"
    else:
        default_intent = "knowledge_qa"

    intents = await _classify({**state, "query": query_for_intent}, system_prompt, default_intent)

    if _looks_like_document_summary_qa(query_for_intent):
        return [_intent_item("knowledge_qa", 0.95)]

    if _looks_like_report_request(query_for_intent):
        # A report request with retrieved content becomes a referenced report;
        # without retrieved content it still becomes a normal report request.
        return [_intent_item("report_outline", 0.95)]

    if has_related_content and not any(item["intent_type"] in {"report_outline", "report_write", "report_edit"} for item in intents):
        return [_intent_item("knowledge_qa", 0.9)]

    if not has_related_content and any(item["intent_type"] == "general_qa" for item in intents):
        return [_intent_item("general_qa", 0.9)]

    if not has_related_content:
        return [_intent_item("general_qa", 0.9)]

    return intents


async def intent_node(state, config):
    await emit_step(config, "thinking", "正在分析您的问题...")
    query = state.get("query", "")
    has_identity_request, effective_query = split_identity_request(query)
    query_for_intent = effective_query or query
    classify_state = {**state, "query": query_for_intent}
    outline = state.get("current_outline")
    confirmed = state.get("outline_confirmed", False)
    report = state.get("current_report")
    has_docs = bool(state.get("doc_ids") or state.get("temp_doc_ids"))
    retrieval_checked = has_docs and ("retrieved_chunks" in state or "rag_fallback" in state)
    if retrieval_checked:
        await emit_thinking(config, f"思考：已完成文档相关性检查，接下来根据问题“{query}”和检索结果判断回答方式。")
    else:
        await emit_thinking(config, f"思考：用户当前问题是“{query}”，接下来判断应该进入通识问答、文档问答还是报告生成流程。")
    confidence = 0.95
    if retrieval_checked:
        intents = await _classify_after_document_retrieval(classify_state, config, query, query_for_intent)
        confidence = max(item["confidence"] for item in intents)
    elif report:
        if _looks_like_chitchat(query):
            intents = [_intent_item("general_qa", confidence)]
        else:
            default_intent = "report_outline" if has_docs else "report_edit"
            intents = await _classify(classify_state, INTENT_WITH_REPORT_SYSTEM, default_intent)
            if has_docs and any(item["intent_type"] == "report_outline" for item in intents):
                intents = [item for item in intents if item["intent_type"] != "report_edit"]
            else:
                intents = _with_heuristic_intents(query_for_intent, intents, report=True, outline=False, confirmed=False)
    elif outline and confirmed:
        if _looks_like_chitchat(query):
            intents = [_intent_item("general_qa", confidence)]
        elif _looks_like_write_request(query):
            intents = [_intent_item("report_write", confidence)]
            if _looks_like_side_qa_request(query_for_intent):
                intents = _dedupe_intents([_intent_item("knowledge_qa", 0.9), *intents])
        else:
            intents = await _classify(classify_state, INTENT_CONFIRMED_SYSTEM, "report_write")
            confidence = max(item["confidence"] for item in intents)
    elif outline and not confirmed:
        if has_docs:
            intents = await _classify(classify_state, INTENT_WITH_OUTLINE_SYSTEM, "report_outline")
            if any(item["intent_type"] == "report_outline" for item in intents):
                intents = [
                    item
                    for item in intents
                    if item["intent_type"] not in {"report_write", "outline_modify"}
                ]
            confidence = max(item["confidence"] for item in intents)
        elif _looks_like_write_request(query):
            intents = [_intent_item("report_write", confidence)]
            if _looks_like_side_qa_request(query_for_intent):
                intents = _dedupe_intents([_intent_item("knowledge_qa", 0.9), *intents])
        elif _looks_like_outline_modify(query):
            intents = [_intent_item("outline_modify", confidence)]
        else:
            intents = await _classify(classify_state, INTENT_WITH_OUTLINE_SYSTEM, "outline_modify")
            confidence = max(item["confidence"] for item in intents)
    elif _looks_like_chitchat(query):
        intents = [_intent_item("general_qa", confidence)]
    elif _looks_like_document_summary_qa(query) and (state.get("doc_ids") or state.get("temp_doc_ids")):
        intents = [_intent_item("knowledge_qa", confidence)]
    else:
        default_intent = "knowledge_qa" if has_docs else "general_qa"
        intents = await _classify(classify_state, INTENT_FRESH_SYSTEM, default_intent)
        confidence = max(item["confidence"] for item in intents)

    intents = _downgrade_knowledge_to_general_without_docs(intents, has_docs)

    if has_identity_request and bool(effective_query):
        intents = _dedupe_intents([_intent_item("general_qa", 0.8), *intents])
    if _looks_like_report_request(query_for_intent) and not report and not outline:
        if any(item["intent_type"] == "knowledge_qa" for item in intents):
            intents = _dedupe_intents([*intents, _intent_item("report_outline", 0.9)])
    primary = _primary_intent(intents)
    await emit_intents(config, intents)
    labels = "、".join(f"「{INTENT_LABELS.get(item['intent_type'], item['intent_type'])}」" for item in intents)
    await emit_thinking(config, f"Thinking: intent classification completed; entering {labels} flow.")
    return {
        "intent": primary,
        "intents": intents,
        "intent_confidence": confidence,
        "effective_query": query_for_intent,
        "identity_intro_requested": has_identity_request and bool(effective_query),
    }
