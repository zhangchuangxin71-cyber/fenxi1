import re

from app.agent.nodes.common import emit_step, emit_thinking, history_brief, parse_json_object
from app.agent.nodes.query_utils import CONNECTORS
from app.agent.prompts.query_rewrite import QUERY_REWRITE_SYSTEM, QUERY_REWRITE_USER_TEMPLATE
from app.config import settings
from app.llm.registry import get_provider

MAX_REWRITE_QUERIES = 4
VALID_RETRIEVAL_PROFILES = {"precise", "overview", "report", "compare"}


def _normalize_retrieval_profile(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip().lower()
    return clean if clean in VALID_RETRIEVAL_PROFILES else None


def _normalize_requires_retrieval(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        clean = value.strip().lower()
        if clean in {"true", "yes", "1", "需要", "检索", "需要检索"}:
            return True
        if clean in {"false", "no", "0", "不需要", "无需", "无需检索"}:
            return False
    return None


def _infer_requires_retrieval(*texts: str) -> bool:
    text = "".join(item or "" for item in texts).strip().lower()
    if not text:
        return False

    identity_markers = [
        "你是谁", "你是什么", "你是什么模型", "你是哪个模型", "你用的什么模型",
        "介绍一下你自己", "自我介绍", "你能做什么", "你可以做什么",
    ]
    chitchat_markers = [
        "你好", "您好", "hello", "hi", "嗨", "在吗", "谢谢", "感谢",
        "早上好", "下午好", "晚上好",
    ]
    if any(marker in text for marker in identity_markers):
        return False
    if text in chitchat_markers:
        return False
    return True


def _is_document_summary_request(*texts: str) -> bool:
    text = " ".join(item or "" for item in texts)
    summary_markers = ["总结一下", "总结下", "总结", "概括", "概述", "主要内容", "讲了什么", "梳理一下", "归纳一下"]
    return any(marker in text for marker in summary_markers)


def _is_current_doc_reference(*texts: str) -> bool:
    text = " ".join(item or "" for item in texts)
    markers = [
        "这篇文档",
        "这个文档",
        "本文档",
        "该文档",
        "当前文档",
        "这份文档",
        "这份材料",
        "这个文件",
        "这份文件",
        "当前文件",
        "该文件",
        "当前材料",
        "新上传的文档",
        "新上传文档",
        "新上传的文件",
        "新上传文件",
        "刚上传的文档",
        "刚上传的文件",
    ]
    return any(marker in text for marker in markers) or _is_document_summary_request(text)


def _infer_retrieval_profile(*texts: str) -> str:
    text = " ".join(item or "" for item in texts)
    report_markers = [
        "报告", "大纲", "提纲", "总结文档", "分析文档", "读后感",
        "写一份", "生成", "撰写", "起草", "输出一份", "形成一份",
    ]
    compare_markers = ["对比", "比较", "差异", "区别", "相同点", "不同点", "优劣", "分别说明"]
    overview_markers = [
        "主要内容", "总结", "概述", "介绍", "是什么", "讲了什么",
        "核心观点", "主要结论", "整体", "梳理", "归纳", "提炼",
    ]
    if any(marker in text for marker in report_markers):
        return "report"
    if any(marker in text for marker in compare_markers):
        return "compare"
    if any(marker in text for marker in overview_markers):
        return "overview"
    return "precise"


def _fallback_query_item(query: str, idx: int = 1) -> dict:
    clean = (query or "").strip()
    return {
        "id": f"q{idx}",
        "question": clean,
        "retrieval_query": clean,
        "answer_focus": clean,
        "requires_retrieval": _infer_requires_retrieval(clean),
        "retrieval_profile": _infer_retrieval_profile(clean),
    }


def _heuristic_split(query: str) -> list[dict]:
    clean = (query or "").strip()
    if not clean:
        return []
    pattern = "|".join(re.escape(item) for item in CONNECTORS)
    parts = [part.strip(" ，,。？?；;") for part in re.split(pattern, clean) if part.strip(" ，,。？?；;")]
    if len(parts) <= 1:
        return [_fallback_query_item(clean)]
    return [_fallback_query_item(part, idx) for idx, part in enumerate(parts[:MAX_REWRITE_QUERIES], start=1)]


def normalize_rewritten_queries(raw: object, fallback_query: str) -> list[dict]:
    if not isinstance(raw, list):
        return _heuristic_split(fallback_query)

    normalized: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        retrieval_query = str(item.get("retrieval_query") or question).strip()
        answer_focus = str(item.get("answer_focus") or question).strip()
        if not question and not retrieval_query:
            continue
        idx = len(normalized) + 1
        final_question = question or retrieval_query
        final_retrieval_query = retrieval_query or question
        final_answer_focus = answer_focus or question or retrieval_query
        profile = _normalize_retrieval_profile(item.get("retrieval_profile")) or _infer_retrieval_profile(
            final_question,
            final_retrieval_query,
            final_answer_focus,
        )
        requires_retrieval = _normalize_requires_retrieval(
            item.get("requires_retrieval")
        )
        if requires_retrieval is None:
            requires_retrieval = _infer_requires_retrieval(
                final_question,
                final_retrieval_query,
                final_answer_focus,
            )
        if _is_document_summary_request(final_question, final_retrieval_query, final_answer_focus):
            requires_retrieval = True
            if profile == "precise":
                profile = "overview"
        normalized.append(
            {
                "id": str(item.get("id") or f"q{idx}").strip() or f"q{idx}",
                "question": final_question,
                "retrieval_query": final_retrieval_query,
                "answer_focus": final_answer_focus,
                "requires_retrieval": requires_retrieval,
                "retrieval_profile": profile,
            }
        )
        if len(normalized) >= MAX_REWRITE_QUERIES:
            break
    return normalized or _heuristic_split(fallback_query)


async def query_rewrite_node(state, config):
    await emit_step(config, "thinking", "正在拆解和改写问题...")
    query = state.get("effective_query") or state["query"]
    await emit_thinking(config, "思考：先整理用户问题，必要时拆分为多个查询，避免不同问题互相干扰。")
    has_docs = bool(state.get("doc_ids") or state.get("temp_doc_ids"))
    prefer_current_docs = has_docs and _is_current_doc_reference(state.get("query", ""), query)
    raw_history_brief = history_brief(state.get("history", []), limit=3)
    rewrite_history = raw_history_brief
    if prefer_current_docs:
        rewrite_history = (
            "注意：本轮请求已传入新的候选文档 doc_ids/temp_doc_ids。"
            "如果用户说“这篇文档”“当前文档”“该文档”“新上传的文档”，"
            "必须指向本轮传入的候选文档，不要用历史里的旧文档名补全。"
            "历史仅可用于理解用户表达习惯、格式要求或上下文，不可用于判断当前文档名。\n\n"
            f"{raw_history_brief}"
        )
    if prefer_current_docs:
        await emit_thinking(config, "思考：本轮问题指向当前/新上传文档，问题改写将优先使用本轮传入的文档，避免被历史文档带偏。")

    provider = get_provider("doubao")
    messages = [
        {"role": "system", "content": QUERY_REWRITE_SYSTEM},
        {
            "role": "user",
            "content": QUERY_REWRITE_USER_TEMPLATE.format(
                query=state.get("query", ""),
                effective_query=query,
                history_brief=rewrite_history,
            ),
        },
    ]

    try:
        result = await provider.chat(messages, model=settings.model_fast, temperature=0, max_tokens=1024)
        parsed = parse_json_object(result.get("text", ""), {})
        rewritten = normalize_rewritten_queries(parsed.get("queries"), query)
    except Exception:
        rewritten = _heuristic_split(query)

    if len(rewritten) > 1:
        await emit_thinking(config, f"思考：已拆解出 {len(rewritten)} 个子问题，将分别查找相关资料后再综合回答。")
    else:
        await emit_thinking(config, "思考：当前问题无需拆分，将作为一个查询继续处理。")

    effective_query = "；".join(item["question"] for item in rewritten) or query
    return {"rewritten_queries": rewritten, "effective_query": effective_query}
