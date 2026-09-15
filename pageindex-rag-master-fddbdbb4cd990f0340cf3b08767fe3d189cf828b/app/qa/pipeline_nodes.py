# 模块说明：流水线问答节点，负责选文档、取证据、生成答案。
import json
import logging
import re
import time
import uuid
from typing import NotRequired, TypedDict

import pageindex.utils as utils

from core.document_tools import get_document_structure_tool, get_page_content_tool, list_documents_tool
from core.llm_ops import DEFAULT_NO_INFO_ANSWER, extract_json_tolerant, safe_llm_completion_async
from models.schemas import Answer, EvidenceItem
from utils.runtime import get_client_runtime

logger = logging.getLogger(__name__)

DEFAULT_BM25_PREFILTER_TOP_K = 8
DEFAULT_SHORT_TERM_TURNS = 4
MAX_HISTORY_MESSAGES = 40
MAX_MEMORY_SLOT_ITEMS = 6
_REWRITE_PRONOUN_MARKERS = ("这", "这个", "那", "那个", "它", "他", "她", "其", "上述", "前者", "后者", "该")


class PipelineQAState(TypedDict):
    """Pipeline 模式的状态对象。

    该结构在 LangGraph 节点间传递，包含查询、证据、答案与会话记忆等字段。
    """
    query: str
    rewritten_query: str
    top_k_docs: int
    allowed_doc_ids: set[str] | None
    bm25_prefilter_top_k: int
    selected_documents: list[dict]
    evidence: list[dict]
    answer: str
    trace_id: str
    fallback_needed: bool
    messages: list[dict]
    short_term_messages: list[dict]
    short_term_window: int
    conversation_summary: str
    long_term_memory: dict
    last_selected_documents: list[dict]
    last_answer: str
    error: NotRequired[str]


def build_document_catalog(client, allowed_doc_ids: set[str] | None = None) -> list[dict]:
    """构建可用文档目录（可按 allowed_doc_ids 限制范围）。"""
    return list_documents_tool(client, allowed_doc_ids=allowed_doc_ids)


async def select_candidate_documents(
    client,
    query: str,
    *,
    prompts,
    retrieve_model: str,
    top_k: int = 3,
    allowed_doc_ids: set[str] | None = None,
    bm25_prefilter=None,
    bm25_prefilter_top_k: int = DEFAULT_BM25_PREFILTER_TOP_K,
) -> list[dict]:
    """选择候选文档。

    先做 BM25 预筛，再用 LLM 从候选目录中选出最相关文档。
    """
    runtime = get_client_runtime(client)
    catalog = build_document_catalog(client, allowed_doc_ids=allowed_doc_ids)
    if not catalog:
        return []
    filtered_catalog = catalog
    if bm25_prefilter is not None and bm25_prefilter_top_k > 0:
        filtered_catalog = bm25_prefilter.retrieve(
            query=query,
            top_k=max(top_k, bm25_prefilter_top_k),
            allowed_doc_ids=allowed_doc_ids,
        )
        if not filtered_catalog:
            filtered_catalog = catalog
    prompt = (
        f"{prompts.doc_selection}\n\n"
        f"User question:\n{query}\n\n"
        f"Document catalog:\n{json.dumps(filtered_catalog, ensure_ascii=False, indent=2)}\n"
    )
    response = await safe_llm_completion_async(
        model=retrieve_model,
        prompt=prompt,
        breaker=runtime.llm_breaker,
        runtime=runtime,
    )
    result = extract_json_tolerant(response, default={"doc_ids": []})
    selected_ids = result.get("doc_ids", []) if isinstance(result, dict) else []
    selected = []
    seen = set()
    catalog_by_id = {item["doc_id"]: item for item in filtered_catalog}
    for doc_id in selected_ids if isinstance(selected_ids, list) else []:
        if doc_id in catalog_by_id and doc_id not in seen:
            selected.append(catalog_by_id[doc_id])
            seen.add(doc_id)
        if len(selected) >= top_k:
            break
    if not selected:
        selected = filtered_catalog[:top_k]
    return selected[:top_k]


async def choose_relevant_pages(client, doc_id: str, query: str, *, prompts, retrieve_model: str, max_ranges: int = 3) -> list[str]:
    """从文档结构树中选择与问题最相关的页码区间。"""
    runtime = get_client_runtime(client)
    structure = get_document_structure_tool(client, doc_id)
    if not structure:
        return []
    structure_brief = []
    for node in utils.structure_to_list(structure):
        structure_brief.append(
            {
                "title": node.get("title", ""),
                "node_id": node.get("node_id", ""),
                "start_index": node.get("start_index"),
                "end_index": node.get("end_index"),
                "summary": node.get("summary", ""),
            }
        )
    prompt = (
        f"{prompts.page_selection}\n\n"
        f"User question:\n{query}\n\n"
        f"Document structure:\n{json.dumps(structure_brief, ensure_ascii=False, indent=2)}\n"
    )
    response = await safe_llm_completion_async(
        model=retrieve_model,
        prompt=prompt,
        breaker=runtime.llm_breaker,
        runtime=runtime,
    )
    result = extract_json_tolerant(response, default={"ranges": []})
    ranges = result.get("ranges", []) if isinstance(result, dict) else []
    cleaned = []
    seen = set()
    for page_range in ranges:
        if not isinstance(page_range, str):
            continue
        page_range = page_range.strip()
        if not page_range or page_range in seen:
            continue
        seen.add(page_range)
        cleaned.append(page_range)
        if len(cleaned) >= max_ranges:
            break
    return cleaned


def merge_page_ranges(ranges: list[str], max_pages: int = 8) -> str:
    """合并页码区间并截断到最大页数上限。"""
    pages = []
    for part in ranges:
        if "-" in part:
            start, end = part.split("-", 1)
            pages.extend(range(int(start.strip()), int(end.strip()) + 1))
        else:
            pages.append(int(part.strip()))
    unique_pages = []
    seen = set()
    for page in sorted(pages):
        if page not in seen:
            seen.add(page)
            unique_pages.append(page)
    return ",".join(str(page) for page in unique_pages[:max_pages])


async def collect_evidence_for_document(client, doc_meta: dict, query: str, *, prompts, retrieve_model: str) -> dict:
    """为单文档采集证据（选页 -> 取页内容 -> 结构化输出）。"""
    doc_id = doc_meta["doc_id"]
    try:
        page_ranges = await choose_relevant_pages(client, doc_id, query, prompts=prompts, retrieve_model=retrieve_model)
    except Exception:
        logger.exception("Failed to select pages for doc_id=%s", doc_id)
        page_ranges = []
    try:
        pages = merge_page_ranges(page_ranges) if page_ranges else ""
    except Exception:
        logger.exception("Failed to merge page ranges for doc_id=%s", doc_id)
        pages = ""
    try:
        page_content = get_page_content_tool(client, doc_id, pages) if pages else []
    except Exception:
        logger.exception("Failed to fetch page content for doc_id=%s", doc_id)
        page_content = []
    payload = {
        "doc_id": doc_id,
        "doc_name": doc_meta.get("doc_name", ""),
        "doc_description": doc_meta.get("doc_description", ""),
        "selected_page_ranges": page_ranges,
        "fetched_pages": pages,
        "page_content": page_content,
    }
    return EvidenceItem.model_validate(payload).model_dump()


async def generate_final_answer(client, query: str, evidence: list[dict], *, prompts, retrieve_model: str) -> str:
    """基于证据生成最终答案；无结果时返回统一无信息文案。"""
    runtime = get_client_runtime(client)
    compact_evidence = [
        {
            "doc_name": item["doc_name"],
            "selected_page_ranges": item["selected_page_ranges"],
            "page_content": item["page_content"],
        }
        for item in evidence
    ]
    prompt = (
        f"{prompts.answer}\n\n"
        f"User question:\n{query}\n\n"
        f"Evidence:\n{json.dumps(compact_evidence, ensure_ascii=False, indent=2)}\n"
    )
    answer = await safe_llm_completion_async(
        model=retrieve_model,
        prompt=prompt,
        breaker=runtime.llm_breaker,
        runtime=runtime,
    )
    return answer if answer else DEFAULT_NO_INFO_ANSWER


def init_pipeline_state(query: str, *, top_k_docs: int, allowed_doc_ids: set[str] | None, bm25_prefilter_top_k: int) -> PipelineQAState:
    """初始化 Pipeline 状态对象。"""
    return PipelineQAState(
        query=query,
        rewritten_query=query,
        top_k_docs=top_k_docs,
        allowed_doc_ids=allowed_doc_ids,
        bm25_prefilter_top_k=bm25_prefilter_top_k,
        selected_documents=[],
        evidence=[],
        answer="",
        trace_id=uuid.uuid4().hex[:8],
        fallback_needed=False,
        messages=[],
        short_term_messages=[],
        short_term_window=DEFAULT_SHORT_TERM_TURNS,
        conversation_summary="",
        long_term_memory={
            "user_preferences": [],
            "task_goals": [],
        },
        last_selected_documents=[],
        last_answer="",
    )


def build_turn_input(query: str, *, top_k_docs: int, allowed_doc_ids: set[str] | None, bm25_prefilter_top_k: int) -> dict:
    """构造单轮问答输入（供 LangGraph ainvoke 使用）。"""
    return {
        "query": query,
        "rewritten_query": query,
        "top_k_docs": top_k_docs,
        "allowed_doc_ids": allowed_doc_ids,
        "bm25_prefilter_top_k": bm25_prefilter_top_k,
        "selected_documents": [],
        "evidence": [],
        "answer": "",
        "trace_id": uuid.uuid4().hex[:8],
        "fallback_needed": False,
        "messages": [],
        "short_term_messages": [],
        "short_term_window": DEFAULT_SHORT_TERM_TURNS,
        "conversation_summary": "",
        "long_term_memory": {
            "user_preferences": [],
            "task_goals": [],
        },
        "last_selected_documents": [],
        "last_answer": "",
    }


def _format_recent_messages(messages: list[dict], limit: int = 6) -> str:
    """将最近消息格式化为可用于提示词拼接的文本。"""
    recent = messages[-limit:] if limit > 0 else messages
    rows = []
    for item in recent:
        role = str(item.get("role", "") or "").strip() or "unknown"
        content = str(item.get("content", "") or "").strip()
        if not content:
            continue
        rows.append(f"{role}: {content}")
    return "\n".join(rows)


def _extract_rewrite_guard_terms(text: str) -> set[str]:
    raw = str(text or "").strip().lower()
    if not raw:
        return set()
    terms = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{3,}|\d+(?:\.\d+)?", raw))
    stop_terms = {"什么", "哪些", "哪个", "多少", "怎么", "如何", "吗", "呢", "请问"}
    return {term for term in terms if term and term not in stop_terms}


def _query_looks_standalone(query: str) -> bool:
    text = str(query or "").strip()
    if not text:
        return False
    compact = re.sub(r"\s+", "", text)
    has_pronoun = any(marker in compact for marker in _REWRITE_PRONOUN_MARKERS)
    # Short factual queries without pronoun-style references are usually self-contained.
    return (not has_pronoun) and len(compact) <= 32


def _should_keep_original_query(original: str, rewritten: str) -> bool:
    orig = str(original or "").strip()
    new = str(rewritten or "").strip()
    if not new:
        return True
    if not orig or orig == new:
        return False
    if _query_looks_standalone(orig):
        return True
    orig_terms = _extract_rewrite_guard_terms(orig)
    if not orig_terms:
        return False
    new_terms = _extract_rewrite_guard_terms(new)
    if not new_terms:
        return True
    overlap_ratio = len(orig_terms & new_terms) / max(1, len(orig_terms))
    return overlap_ratio < 0.5


def _normalize_memory_items(values: list, *, max_items: int = MAX_MEMORY_SLOT_ITEMS) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if len(text) > 100:
            text = text[:100]
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(text)
        if len(normalized) >= max_items:
            break
    return normalized


def _normalize_long_term_memory(memory_payload: dict | None) -> dict:
    payload = memory_payload if isinstance(memory_payload, dict) else {}
    preferences = payload.get("user_preferences", [])
    goals = payload.get("task_goals", [])
    return {
        "user_preferences": _normalize_memory_items(preferences if isinstance(preferences, list) else []),
        "task_goals": _normalize_memory_items(goals if isinstance(goals, list) else []),
    }


def _merge_long_term_memory(base: dict | None, delta: dict | None) -> dict:
    base_norm = _normalize_long_term_memory(base)
    delta_norm = _normalize_long_term_memory(delta)
    merged = {
        "user_preferences": _normalize_memory_items(
            list(base_norm.get("user_preferences", [])) + list(delta_norm.get("user_preferences", []))
        ),
        "task_goals": _normalize_memory_items(
            list(base_norm.get("task_goals", [])) + list(delta_norm.get("task_goals", []))
        ),
    }
    return merged


async def extract_long_term_memory_slots(client, state: PipelineQAState, *, retrieve_model: str) -> dict:
    runtime = get_client_runtime(client)
    short_term_messages = state.get("short_term_messages", []) or state.get("messages", [])
    history_text = _format_recent_messages(short_term_messages, limit=10)
    if not history_text:
        return _normalize_long_term_memory(state.get("long_term_memory", {}))

    previous_memory = _normalize_long_term_memory(state.get("long_term_memory", {}))
    prompt = (
        "你是会话长期记忆提取器。请从最近对话中提取稳定信息，输出 JSON。\n"
        "只保留可长期复用的信息：\n"
        "1) 用户偏好（表达风格、格式、语言偏好等）；\n"
        "2) 任务目标（持续关注的问题、明确目标）。\n"
        "不要提取一次性细节。\n\n"
        'JSON schema: {"user_preferences": ["..."], "task_goals": ["..."]}\n\n'
        f"已有长期记忆:\n{json.dumps(previous_memory, ensure_ascii=False)}\n\n"
        f"最近对话:\n{history_text}\n"
    )
    response = await safe_llm_completion_async(
        model=retrieve_model,
        prompt=prompt,
        breaker=runtime.llm_breaker,
        runtime=runtime,
    )
    extracted = extract_json_tolerant(response, default={})
    return _merge_long_term_memory(previous_memory, extracted if isinstance(extracted, dict) else {})


async def rewrite_query_from_history(client, state: PipelineQAState, *, retrieve_model: str) -> str:
    """结合会话摘要/短期历史/长期记忆改写当前问题。"""
    runtime = get_client_runtime(client)
    short_term_messages = state.get("short_term_messages", []) or state.get("messages", [])
    history_text = _format_recent_messages(short_term_messages, limit=max(2, int(state.get("short_term_window", DEFAULT_SHORT_TERM_TURNS)) * 2))
    summary_text = str(state.get("conversation_summary", "") or "").strip()
    long_term_memory = _normalize_long_term_memory(state.get("long_term_memory", {}))
    long_term_text = json.dumps(long_term_memory, ensure_ascii=False)
    if not history_text and not summary_text and long_term_text == '{"user_preferences": [], "task_goals": []}':
        return state["query"]

    prompt = (
        "你是多轮文档问答中的问题改写助手。"
        "请基于会话摘要、短期上下文和长期记忆，把用户当前问题改写成一个完整、独立、可用于文档检索的问题。"
        "不要回答问题，只输出改写后的问题一句话。\n\n"
        f"会话摘要:\n{summary_text or '(无)'}\n\n"
        f"长期记忆:\n{long_term_text}\n\n"
        f"最近对话:\n{history_text or '(无)'}\n\n"
        f"当前用户问题:\n{state['query']}\n"
    )
    rewritten = await safe_llm_completion_async(
        model=retrieve_model,
        prompt=prompt,
        breaker=runtime.llm_breaker,
        runtime=runtime,
    )
    cleaned = (rewritten or "").strip()
    original = str(state["query"] or "").strip()
    if _should_keep_original_query(original, cleaned):
        return original
    return cleaned if cleaned else original


async def summarize_conversation_memory(client, state: PipelineQAState, *, retrieve_model: str) -> str:
    """压缩会话摘要，供后续多轮检索改写使用。"""
    runtime = get_client_runtime(client)
    history_text = _format_recent_messages(state.get("short_term_messages", []) or state.get("messages", []), limit=10)
    selected_doc_names = ", ".join(
        str(item.get("doc_name", "") or "").strip()
        for item in state.get("selected_documents", [])
        if str(item.get("doc_name", "") or "").strip()
    )
    prompt = (
        "你是会话记忆压缩助手。"
        "请把下面的文档问答会话压缩成简洁中文摘要，保留：当前讨论主题、用户约束、代词指代对象、最近确认结论。"
        "不要展开细节，不要超过120字。\n\n"
        f"历史摘要:\n{state.get('conversation_summary', '') or '(无)'}\n\n"
        f"最近对话:\n{history_text or '(无)'}\n\n"
        f"本轮改写问题:\n{state.get('rewritten_query', '')}\n\n"
        f"本轮命中文档:\n{selected_doc_names or '(无)'}\n\n"
        f"本轮回答:\n{state.get('answer', '') or '(无)'}\n"
    )
    summary = await safe_llm_completion_async(
        model=retrieve_model,
        prompt=prompt,
        breaker=runtime.llm_breaker,
        runtime=runtime,
    )
    cleaned = (summary or "").strip()
    return cleaned if cleaned else str(state.get("conversation_summary", "") or "")


async def node_rewrite_query(state: PipelineQAState, *, client, retrieve_model: str):
    """LangGraph 节点：改写用户问题。"""
    state["rewritten_query"] = await rewrite_query_from_history(
        client,
        state,
        retrieve_model=retrieve_model,
    )
    return state


async def node_select_documents(state: PipelineQAState, *, client, prompts, retrieve_model: str, bm25_prefilter):
    """LangGraph 节点：选择候选文档。"""
    state["selected_documents"] = await select_candidate_documents(
        client,
        state["rewritten_query"],
        prompts=prompts,
        retrieve_model=retrieve_model,
        top_k=state["top_k_docs"],
        allowed_doc_ids=state["allowed_doc_ids"],
        bm25_prefilter=bm25_prefilter,
        bm25_prefilter_top_k=state["bm25_prefilter_top_k"],
    )
    return state


async def node_collect_evidence(state: PipelineQAState, *, client, prompts, retrieve_model: str):
    """LangGraph 节点：按候选文档采集证据。"""
    evidence = []
    for item in state["selected_documents"]:
        evidence.append(await collect_evidence_for_document(client, item, state["rewritten_query"], prompts=prompts, retrieve_model=retrieve_model))
    state["evidence"] = evidence
    return state


async def node_generate_answer(state: PipelineQAState, *, client, prompts, retrieve_model: str):
    """LangGraph 节点：基于证据生成答案。"""
    state["answer"] = await generate_final_answer(client, state["rewritten_query"], state["evidence"], prompts=prompts, retrieve_model=retrieve_model)
    state["fallback_needed"] = state["answer"] == DEFAULT_NO_INFO_ANSWER
    return state


async def node_update_memory(state: PipelineQAState, *, client, retrieve_model: str):
    """LangGraph 节点：更新短期消息、摘要和长期记忆。"""
    messages = list(state.get("messages", []))
    messages.append({"role": "user", "content": state["query"]})
    messages.append({"role": "assistant", "content": state.get("answer", "")})
    state["messages"] = messages[-MAX_HISTORY_MESSAGES:]
    short_term_window = max(2, min(8, int(state.get("short_term_window", DEFAULT_SHORT_TERM_TURNS))))
    state["short_term_window"] = short_term_window
    state["short_term_messages"] = state["messages"][-(short_term_window * 2):]
    state["last_selected_documents"] = list(state.get("selected_documents", []))
    state["last_answer"] = state.get("answer", "")
    state["conversation_summary"] = await summarize_conversation_memory(
        client,
        state,
        retrieve_model=retrieve_model,
    )
    state["long_term_memory"] = await extract_long_term_memory_slots(
        client,
        state,
        retrieve_model=retrieve_model,
    )
    return state


async def run_pipeline_qa(client, query: str, *, prompts, retrieve_model: str, top_k_docs: int, allowed_doc_ids: set[str] | None, bm25_prefilter=None, bm25_prefilter_top_k: int = DEFAULT_BM25_PREFILTER_TOP_K, log_event=None) -> dict:
    """顺序执行 Pipeline 版 QA（非 LangGraph 编排路径）。"""
    state = init_pipeline_state(query, top_k_docs=top_k_docs, allowed_doc_ids=allowed_doc_ids, bm25_prefilter_top_k=bm25_prefilter_top_k)
    started = time.perf_counter()
    if log_event is not None:
        log_event("qa_trace_start", trace_id=state["trace_id"], query_preview=query[:120])
    try:
        await node_select_documents(state, client=client, prompts=prompts, retrieve_model=retrieve_model, bm25_prefilter=bm25_prefilter)
    except Exception:
        if log_event is not None:
            log_event("qa_trace_select_failed", level=logging.ERROR, trace_id=state["trace_id"])
        logger.exception("qa_trace_select_failed")
        state["selected_documents"] = []
    evidence = []
    for item in state["selected_documents"]:
        try:
            evidence.append(await collect_evidence_for_document(client, item, query, prompts=prompts, retrieve_model=retrieve_model))
        except Exception:
            if log_event is not None:
                log_event("qa_trace_evidence_failed", level=logging.ERROR, trace_id=state["trace_id"], doc_id=item.get("doc_id"))
            logger.exception("qa_trace_evidence_failed")
    state["evidence"] = evidence
    try:
        await node_generate_answer(state, client=client, prompts=prompts, retrieve_model=retrieve_model)
    except Exception:
        if log_event is not None:
            log_event("qa_trace_answer_failed", level=logging.ERROR, trace_id=state["trace_id"])
        logger.exception("qa_trace_answer_failed")
        state["answer"] = DEFAULT_NO_INFO_ANSWER
    payload = {
        "answer": state["answer"],
        "selected_documents": [
            {
                "doc_id": item["doc_id"],
                "doc_name": item["doc_name"],
                "doc_description": item.get("doc_description", ""),
                "index_mode": item.get("index_mode", "standard"),
            }
            for item in state["selected_documents"]
        ],
        "evidence": [
            {
                "doc_id": item["doc_id"],
                "doc_name": item["doc_name"],
                "selected_page_ranges": item["selected_page_ranges"],
                "fetched_pages": item["fetched_pages"],
            }
            for item in state["evidence"]
        ],
    }
    if log_event is not None:
        log_event("qa_trace_done", trace_id=state["trace_id"], elapsed_ms=round((time.perf_counter() - started) * 1000, 2), selected_docs=len(payload["selected_documents"]), evidence_docs=len(payload["evidence"]))
    return Answer.model_validate(payload).model_dump()

