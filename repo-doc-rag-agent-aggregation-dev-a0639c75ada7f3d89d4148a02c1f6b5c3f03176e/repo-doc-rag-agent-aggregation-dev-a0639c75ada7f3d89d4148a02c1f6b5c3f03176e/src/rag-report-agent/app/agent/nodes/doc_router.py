from difflib import SequenceMatcher
import re
from pathlib import Path

from app.agent.nodes.common import emit_step, emit_thinking, history_brief, parse_json_object
from app.agent.nodes.query_utils import is_compound_query
from app.agent.prompts.doc_router import DOC_ROUTER_SYSTEM, DOC_ROUTER_USER_TEMPLATE
from app.config import settings
from app.db.documents import load_doc_meta_async
from app.llm.registry import get_provider


def _format_docs(metas: dict[str, dict]) -> str:
    rows = []
    for idx, (doc_id, meta) in enumerate(metas.items(), start=1):
        rows.append(
            f"[{idx}] doc_id={doc_id}, 文档名=《{meta.get('doc_name', '')}》\n"
            f"    描述：{meta.get('doc_description', '')}"
        )
    return "\n".join(rows) or "无"


def _normalize_doc_name(text: str) -> str:
    value = Path(str(text or "").strip()).stem.lower()
    for token in ["《", "》", "「", "」", "“", "”", "\"", "'", " ", "\t", "\n", "_", "-", "：", ":", "（", "）", "(", ")"]:
        value = value.replace(token, "")
    return value


def _extract_doc_mentions(query: str) -> list[str]:
    text = query or ""
    return [item.strip() for item in re.findall(r"《([^》]+)》", text) if item.strip()]


def _char_ngrams(text: str, size: int = 2) -> set[str]:
    if len(text) < size:
        return {text} if text else set()
    return {text[idx : idx + size] for idx in range(len(text) - size + 1)}


def _doc_name_query_score(doc_name: str, query: str) -> float:
    doc = _normalize_doc_name(doc_name)
    query_text = _normalize_doc_name(query)
    if not doc or not query_text:
        return 0.0
    if doc in query_text:
        return 1.0

    doc_grams = _char_ngrams(doc, 2)
    query_grams = _char_ngrams(query_text, 2)
    coverage = len(doc_grams & query_grams) / max(len(doc_grams), 1)
    longest = max(
        (match.size for match in SequenceMatcher(None, doc, query_text).get_matching_blocks()),
        default=0,
    ) / max(len(doc), 1)
    ratio = SequenceMatcher(None, doc, query_text).ratio()
    return max(ratio, coverage * 0.7 + longest * 0.3)


def _select_docs_by_name(query: str, metas: dict[str, dict]) -> list[str]:
    mentions = [_normalize_doc_name(item) for item in _extract_doc_mentions(query)]
    mentions = [item for item in mentions if item]
    if mentions:
        selected: list[str] = []
        for doc_id, meta in metas.items():
            doc_name = _normalize_doc_name(meta.get("doc_name", ""))
            if any(mention in doc_name or doc_name in mention for mention in mentions):
                selected.append(doc_id)
        if selected:
            return selected

    scored: list[tuple[str, float]] = []
    for doc_id, meta in metas.items():
        doc_name = str(meta.get("doc_name") or "")
        if not doc_name:
            continue
        if mentions:
            score = max(
                SequenceMatcher(None, mention, _normalize_doc_name(doc_name)).ratio()
                for mention in mentions
            )
        else:
            score = _doc_name_query_score(doc_name, query)
        scored.append((doc_id, score))

    if not scored:
        return []
    scored.sort(key=lambda item: item[1], reverse=True)
    best_doc_id, best_score = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0.0
    if best_score >= 0.5 and (best_score - second_score >= 0.08 or second_score < 0.45):
        return [best_doc_id]
    return []


def _select_latest_doc(metas: dict[str, dict]) -> list[str]:
    if not metas:
        return []
    latest_doc_id = max(
        metas,
        key=lambda doc_id: str(metas[doc_id].get("binding_created_at") or ""),
    )
    return [latest_doc_id]


def _is_current_doc_reference(query: str) -> bool:
    text = query or ""
    markers = [
        "这篇文档",
        "这个文档",
        "本文档",
        "该文档",
        "当前文档",
        "这份文档",
        "这个文件",
        "这份文件",
        "当前文件",
        "该文件",
        "这份材料",
        "当前材料",
        "新上传的文档",
        "新上传文档",
        "新上传的文件",
        "新上传文件",
        "刚上传的文档",
        "刚上传的文件",
        "总结一下",
        "总结下",
        "概括一下",
        "概括下",
        "主要内容",
        "讲了什么",
    ]
    return any(marker in text for marker in markers)


def _looks_like_report_request(query: str) -> bool:
    text = query or ""
    report_words = ["报告", "文章", "文档", "材料", "分析稿", "总结稿", "综述", "大纲", "提纲"]
    action_words = ["写", "撰写", "生成", "输出", "形成", "整理成", "做", "制作", "起草", "拟一份", "帮我写", "帮我生成"]
    return any(action in text for action in action_words) and any(word in text for word in report_words)


def _split_selected_docs(selected: list[str], temp_doc_ids: list[str]) -> dict:
    temp_set = set(temp_doc_ids)
    return {
        "selected_doc_ids": [doc_id for doc_id in selected if doc_id not in temp_set],
        "selected_temp_doc_ids": [doc_id for doc_id in selected if doc_id in temp_set],
    }


async def doc_router_node(state, config):
    all_doc_ids = list(state.get("doc_ids", [])) + list(state.get("temp_doc_ids", []))
    raw_query = state["query"]
    query = state.get("effective_query") or raw_query
    is_multi_query = len(state.get("rewritten_queries") or []) > 1
    if not all_doc_ids:
        await emit_thinking(config, "思考：用户没有指定候选文档，因此后续检索会在当前可访问范围内进行。")
        return {"selected_doc_ids": [], "selected_temp_doc_ids": []}
    if len(all_doc_ids) == 1:
        await emit_thinking(config, "思考：当前只有 1 篇候选文档，将直接围绕这篇文档查找答案。")
        return {
            "selected_doc_ids": list(state.get("doc_ids", [])),
            "selected_temp_doc_ids": list(state.get("temp_doc_ids", [])),
        }

    await emit_step(config, "thinking", "正在确定相关文档...")
    await emit_thinking(config, f"思考：当前有 {len(all_doc_ids)} 篇候选文档，需要根据问题“{query}”筛选最相关的参考资料。")
    metas = await load_doc_meta_async(state["kb_id"], all_doc_ids)
    if not metas:
        await emit_thinking(config, "思考：暂时无法读取完整文档信息，将使用用户传入的候选文档继续检索。")
        return {
            "selected_doc_ids": list(state.get("doc_ids", [])),
            "selected_temp_doc_ids": list(state.get("temp_doc_ids", [])),
        }

    should_use_current_doc_rule = (
        state.get("intent") == "report_outline"
        or _looks_like_report_request(raw_query)
        or _is_current_doc_reference(raw_query)
        or (state.get("intent") == "knowledge_qa" and _is_current_doc_reference(raw_query))
    )
    if should_use_current_doc_rule:
        selected = _select_docs_by_name(raw_query, metas)
        if selected:
            await emit_thinking(config, "思考：用户已在问题中指定文档名，将优先基于指定文档查找资料。")
            return _split_selected_docs(selected, list(state.get("temp_doc_ids", [])))

        selected = _select_latest_doc(metas)
        if selected:
            await emit_thinking(config, "思考：用户未明确指定文档名，将默认基于最近上传/绑定的文档查找资料。")
            return _split_selected_docs(selected, list(state.get("temp_doc_ids", [])))

    if is_multi_query or is_compound_query(query):
        await emit_thinking(config, "思考：当前问题包含多个部分，将优先覆盖全部候选文档以避免遗漏信息。")
        temp_set = set(state.get("temp_doc_ids", []))
        return {
            "selected_doc_ids": [doc_id for doc_id in all_doc_ids if doc_id not in temp_set],
            "selected_temp_doc_ids": [doc_id for doc_id in all_doc_ids if doc_id in temp_set],
        }
    if len(metas) > settings.doc_router_max_docs:
        metas = dict(list(metas.items())[: settings.doc_router_max_docs])

    provider = get_provider("doubao")
    messages = [
        {"role": "system", "content": DOC_ROUTER_SYSTEM},
        {
            "role": "user",
            "content": DOC_ROUTER_USER_TEMPLATE.format(
                query=query,
                history_brief=history_brief(state.get("history", []), limit=3),
                docs_list=_format_docs(metas),
            ),
        },
    ]
    try:
        result = await provider.chat(messages, model=settings.model_fast, temperature=0, max_tokens=512)
        parsed = parse_json_object(result.get("text", ""), {})
        selected = [doc_id for doc_id in parsed.get("selected_doc_ids", []) if doc_id in all_doc_ids]
    except Exception:
        selected = []

    if not selected:
        selected = all_doc_ids
    await emit_thinking(config, f"思考：已确定最相关的参考文档，接下来进入资料检索。")
    temp_set = set(state.get("temp_doc_ids", []))
    return {
        "selected_doc_ids": [doc_id for doc_id in selected if doc_id not in temp_set],
        "selected_temp_doc_ids": [doc_id for doc_id in selected if doc_id in temp_set],
    }
