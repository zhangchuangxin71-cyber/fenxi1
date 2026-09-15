# 模块说明：将 QA 节点适配为 LangGraph 执行图。
import importlib.util
import json

from core.document_tools import parse_pages_expression
from core.llm_ops import (
    DEFAULT_NO_INFO_ANSWER,
    extract_json_tolerant,
    is_no_info_answer,
    safe_llm_completion_async,
)
from qa.agent_mode import query_agent_multi_doc
from qa.pipeline_nodes import (
    build_turn_input,
    node_collect_evidence,
    node_generate_answer,
    node_rewrite_query,
    node_select_documents,
    node_update_memory,
)
from utils.runtime import get_client_runtime

_COMPILED_GRAPH_CACHE: dict[tuple[int, str, str, int | None], object] = {}
_COMPILED_AGENT_GRAPH_CACHE: dict[tuple[int, str, str, int | None], object] = {}


def build_langgraph_pipeline(*, client, prompts, retrieve_model: str, bm25_prefilter=None, log_event=None):
    """构建经典 Pipeline 版 LangGraph（改写->选文档->取证->作答->记忆更新）。"""
    try:
        from langgraph.graph import END, START, StateGraph
        from langgraph.checkpoint.memory import MemorySaver
    except Exception as exc:
        raise RuntimeError(
            "LangGraph is not installed. Install `langgraph` to enable graph execution."
        ) from exc

    cache_key = (
        id(client),
        str(retrieve_model),
        str(getattr(prompts, "agent_system", "") or ""),
        id(bm25_prefilter) if bm25_prefilter is not None else None,
    )
    cached = _COMPILED_GRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    memory = MemorySaver()

    async def rewrite_query_node(state: dict):
        return await node_rewrite_query(
            state,
            client=client,
            retrieve_model=retrieve_model,
        )

    async def select_documents_node(state: dict):
        return await node_select_documents(
            state,
            client=client,
            prompts=prompts,
            retrieve_model=retrieve_model,
            bm25_prefilter=bm25_prefilter,
        )

    async def collect_evidence_node(state: dict):
        return await node_collect_evidence(
            state,
            client=client,
            prompts=prompts,
            retrieve_model=retrieve_model,
        )

    async def generate_answer_node(state: dict):
        return await node_generate_answer(
            state,
            client=client,
            prompts=prompts,
            retrieve_model=retrieve_model,
        )

    async def update_memory_node(state: dict):
        return await node_update_memory(
            state,
            client=client,
            retrieve_model=retrieve_model,
        )

    graph = StateGraph(dict)
    graph.add_node("rewrite_query", rewrite_query_node)
    graph.add_node("select_documents", select_documents_node)
    graph.add_node("collect_evidence", collect_evidence_node)
    graph.add_node("generate_answer", generate_answer_node)
    graph.add_node("update_memory", update_memory_node)
    graph.add_edge(START, "rewrite_query")
    graph.add_edge("rewrite_query", "select_documents")
    graph.add_edge("select_documents", "collect_evidence")
    graph.add_edge("collect_evidence", "generate_answer")
    graph.add_edge("generate_answer", "update_memory")
    graph.add_edge("update_memory", END)
    compiled = graph.compile(checkpointer=memory)
    _COMPILED_GRAPH_CACHE[cache_key] = compiled
    return compiled


def _compact_pages(pages: list[int]) -> str:
    """将页码列表压缩为紧凑区间字符串（如 1-3,5,8-9）。"""
    unique = sorted({int(page) for page in pages if isinstance(page, int) and page > 0})
    if not unique:
        return ""
    chunks: list[str] = []
    start = prev = unique[0]
    for page in unique[1:]:
        if page == prev + 1:
            prev = page
            continue
        chunks.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = page
    chunks.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(chunks)


def _citation_unit_by_doc_type(doc_type: str) -> str:
    """根据文档类型返回引用单位（pdf=页，其它=分片）。"""
    t = str(doc_type or "").strip().lower()
    return "页" if t == "pdf" else "分片"


def _normalize_evidence_items(evidence: list[dict]) -> list[dict]:
    """规范化证据结构，确保后续引用与校验处理字段稳定。"""
    normalized: list[dict] = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        doc_id = str(item.get("doc_id", "") or "").strip()
        doc_name = str(item.get("doc_name", "") or "").strip()
        doc_type = str(item.get("doc_type", "") or "").strip().lower()
        selected_ranges = [str(value).strip() for value in item.get("selected_page_ranges", []) if str(value).strip()]
        verified_pages = [int(page) for page in item.get("verified_pages", []) if isinstance(page, int) and page > 0]
        if not verified_pages:
            fetched_pages = str(item.get("fetched_pages", "") or "").strip()
            if fetched_pages:
                try:
                    verified_pages = [int(page) for page in parse_pages_expression(fetched_pages) if int(page) > 0]
                except Exception:
                    verified_pages = []
        normalized.append(
            {
                "doc_id": doc_id,
                "doc_name": doc_name,
                "doc_type": doc_type,
                "selected_page_ranges": selected_ranges,
                "verified_pages": sorted({int(page) for page in verified_pages if int(page) > 0}),
                "fetched_pages": str(item.get("fetched_pages", "") or "").strip(),
            }
        )
    return normalized


def _normalize_answer_citations(answer: str, evidence: list[dict]) -> tuple[str, list[dict]]:
    """为答案补充规范化引用块，并返回结构化 citation 列表。"""
    cleaned = str(answer or "").strip()
    if not cleaned:
        return "", []

    citation_items: list[dict] = []
    for item in _normalize_evidence_items(evidence):
        pages = item.get("verified_pages", [])
        if not pages:
            continue
        doc_name = str(item.get("doc_name", "") or "").strip()
        if not doc_name:
            continue
        page_expr = _compact_pages(pages)
        if not page_expr:
            continue
        citation_items.append(
            {
                "doc_id": item.get("doc_id", ""),
                "doc_name": doc_name,
                "pages": page_expr,
                "unit": _citation_unit_by_doc_type(item.get("doc_type", "")),
            }
        )

    if not citation_items:
        return cleaned, []

    if "参考依据：" in cleaned:
        return cleaned, citation_items

    lines = ["参考依据："]
    for citation in citation_items:
        lines.append(f"- 《{citation['doc_name']}》 第{citation['pages']}{citation.get('unit', '页')}")
    if len(citation_items) == 1:
        single_source_notice = "检索说明：在本次候选文档范围内，未发现其他文档的可核验证据。"
        if single_source_notice not in cleaned:
            lines.append(single_source_notice)
    return f"{cleaned}\n\n" + "\n".join(lines), citation_items


async def _evaluate_answer_support(*, client, query: str, answer: str, evidence: list[dict]) -> dict:
    """调用 LLM 进行轻量证据一致性判定。"""
    runtime = get_client_runtime(client)
    compact_evidence = []
    for item in _normalize_evidence_items(evidence):
        compact_evidence.append(
            {
                "doc_name": item.get("doc_name", ""),
                "doc_id": item.get("doc_id", ""),
                "selected_page_ranges": item.get("selected_page_ranges", []),
                "verified_pages": item.get("verified_pages", []),
                "fetched_pages": item.get("fetched_pages", ""),
            }
        )

    if not compact_evidence:
        return {"supported": False, "reason": "no_evidence"}

    prompt = (
        "你是文档问答的证据校验器。请判断回答是否被证据支持。"
        "如果证据中没有足够页面依据，必须判定为不支持。"
        "只输出 JSON，不要输出解释文字。\n\n"
        'JSON schema: {"supported": true/false, "reason": "简短中文原因"}\n\n'
        f"用户问题:\n{query}\n\n"
        f"回答:\n{answer}\n\n"
        f"证据:\n{json.dumps(compact_evidence, ensure_ascii=False, indent=2)}\n"
    )
    raw = await safe_llm_completion_async(
        model=client.retrieve_model,
        prompt=prompt,
        breaker=runtime.llm_breaker,
        runtime=runtime,
    )
    parsed = extract_json_tolerant(raw, default={"supported": True, "reason": ""})
    return {
        "supported": bool(parsed.get("supported", True)),
        "reason": str(parsed.get("reason", "") or ""),
    }


def build_langgraph_agent_pipeline(*, client, prompts, retrieve_model: str, bm25_prefilter=None, log_event=None):
    """构建 Agent 版 LangGraph（含证据校验与记忆更新节点）。"""
    try:
        from langgraph.graph import END, START, StateGraph
        from langgraph.checkpoint.memory import MemorySaver
    except Exception as exc:
        raise RuntimeError(
            "LangGraph is not installed. Install `langgraph` to enable graph execution."
        ) from exc

    cache_key = (
        id(client),
        str(retrieve_model),
        str(getattr(prompts, "agent_system", "") or ""),
        id(bm25_prefilter) if bm25_prefilter is not None else None,
    )
    cached = _COMPILED_AGENT_GRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    memory = MemorySaver()

    def emit(event: str, **fields):
        if log_event is not None:
            log_event(event, level=10, **fields)

    async def rewrite_query_node(state: dict):
        emit("agent_graph_rewrite_start")
        updated = await node_rewrite_query(
            state,
            client=client,
            retrieve_model=retrieve_model,
        )
        emit("agent_graph_rewrite_done", rewritten_query=str(updated.get("rewritten_query", "") or "")[:200])
        return updated

    async def agent_answer_node(state: dict):
        rewritten_query = str(state.get("rewritten_query", "") or "").strip()
        effective_query = rewritten_query if rewritten_query else str(state.get("query", "") or "")
        emit("agent_graph_answer_start", query_preview=effective_query[:120])
        result = await query_agent_multi_doc(
            client,
            effective_query,
            log_event=log_event,
            verbose=bool(state.get("verbose", False)),
            stream_output=bool(state.get("stream_output", True)),
            top_k_docs=int(state.get("top_k_docs", 6)),
            allowed_doc_ids=state.get("allowed_doc_ids"),
            bm25_prefilter=bm25_prefilter,
            bm25_prefilter_top_k=int(state.get("bm25_prefilter_top_k", 8)),
            return_details=True,
        )
        if not isinstance(result, dict):
            result = {
                "answer": str(result or ""),
                "selected_documents": [],
                "evidence": [],
                "citations": [],
            }
        state["answer"] = str(result.get("answer", "") or DEFAULT_NO_INFO_ANSWER)
        state["selected_documents"] = list(result.get("selected_documents", []))
        state["evidence"] = list(result.get("evidence", []))
        state["citations"] = list(result.get("citations", []))
        state["retrieval_attempts"] = 1
        state["mode"] = "agent-graph"
        emit(
            "agent_graph_answer_done",
            answer_preview=str(state["answer"])[:120],
            selected_docs=len(state["selected_documents"]),
            evidence_docs=len(state["evidence"]),
        )
        return state

    async def evidence_check_node(state: dict):
        query_text = str(state.get("rewritten_query", "") or state.get("query", "") or "").strip()
        answer = str(state.get("answer", "") or "").strip()
        evidence = list(state.get("evidence", []))
        if not query_text or not answer:
            state["evidence_check"] = {"supported": True, "reason": "skip_empty"}
            return state

        # Only retry when the first-round answer is explicitly "no information".
        if not is_no_info_answer(answer):
            state["evidence_check"] = {"supported": True, "reason": "skip_retry_non_no_info"}
            return state

        verdict = await _evaluate_answer_support(client=client, query=query_text, answer=answer, evidence=evidence)
        state["evidence_check"] = verdict
        if verdict.get("supported", True):
            emit("agent_graph_evidence_check_pass", reason=str(verdict.get("reason", "") or "")[:120])
            return state

        # Disable second retrieval round: keep single-pass result and let upper layer decide fallback.
        emit("agent_graph_evidence_check_no_retry", reason=str(verdict.get("reason", "") or "")[:120])
        state["retrieval_attempts"] = 1
        return state

    async def citation_normalize_node(state: dict):
        normalized_answer, citations = _normalize_answer_citations(
            str(state.get("answer", "") or ""),
            list(state.get("evidence", [])),
        )
        state["answer"] = normalized_answer or str(state.get("answer", "") or DEFAULT_NO_INFO_ANSWER)
        state["citations"] = citations
        return state

    async def update_memory_node(state: dict):
        emit("agent_graph_memory_start")
        updated = await node_update_memory(
            state,
            client=client,
            retrieve_model=retrieve_model,
        )
        emit("agent_graph_memory_done", message_count=len(updated.get("messages", [])))
        return updated

    graph = StateGraph(dict)
    graph.add_node("rewrite_query", rewrite_query_node)
    graph.add_node("agent_answer", agent_answer_node)
    graph.add_node("evidence_check", evidence_check_node)
    graph.add_node("citation_normalize", citation_normalize_node)
    graph.add_node("update_memory", update_memory_node)
    graph.add_edge(START, "rewrite_query")
    graph.add_edge("rewrite_query", "agent_answer")
    graph.add_edge("agent_answer", "evidence_check")
    graph.add_edge("evidence_check", "citation_normalize")
    graph.add_edge("citation_normalize", "update_memory")
    graph.add_edge("update_memory", END)
    compiled = graph.compile(checkpointer=memory)
    _COMPILED_AGENT_GRAPH_CACHE[cache_key] = compiled
    return compiled


def langgraph_available() -> bool:
    """检测当前环境是否安装 LangGraph。"""
    return importlib.util.find_spec("langgraph") is not None


async def run_langgraph_pipeline_qa(
    *,
    client,
    query: str,
    prompts,
    retrieve_model: str,
    top_k_docs: int,
    allowed_doc_ids: set[str] | None,
    bm25_prefilter=None,
    bm25_prefilter_top_k: int = 8,
    thread_id: str,
):
    """执行 Pipeline 版 LangGraph 单轮问答。"""
    graph = build_langgraph_pipeline(
        client=client,
        prompts=prompts,
        retrieve_model=retrieve_model,
        bm25_prefilter=bm25_prefilter,
    )
    turn_input = build_turn_input(
        query,
        top_k_docs=top_k_docs,
        allowed_doc_ids=allowed_doc_ids,
        bm25_prefilter_top_k=bm25_prefilter_top_k,
    )
    return await graph.ainvoke(
        turn_input,
        config={"configurable": {"thread_id": thread_id}},
    )


async def run_langgraph_agent_qa(
    *,
    client,
    query: str,
    prompts,
    retrieve_model: str,
    top_k_docs: int,
    verbose: bool,
    stream_output: bool,
    allowed_doc_ids: set[str] | None,
    bm25_prefilter=None,
    bm25_prefilter_top_k: int = 8,
    thread_id: str,
    log_event,
):
    """执行 Agent 版 LangGraph 单轮问答。"""
    graph = build_langgraph_agent_pipeline(
        client=client,
        prompts=prompts,
        retrieve_model=retrieve_model,
        bm25_prefilter=bm25_prefilter,
        log_event=log_event,
    )
    state = {
        "query": query,
        "rewritten_query": query,
        "verbose": verbose,
        "stream_output": stream_output,
        "top_k_docs": int(top_k_docs),
        "allowed_doc_ids": allowed_doc_ids,
        "bm25_prefilter_top_k": bm25_prefilter_top_k,
        "answer": "",
        "selected_documents": [],
        "evidence": [],
        "citations": [],
        "evidence_check": {},
        "retrieval_attempts": 0,
        "fallback_needed": False,
        "messages": [],
        "short_term_messages": [],
        "short_term_window": 4,
        "conversation_summary": "",
        "long_term_memory": {
            "user_preferences": [],
            "task_goals": [],
        },
        "last_selected_documents": [],
        "last_answer": "",
        "mode": "agent-graph",
    }
    return await graph.ainvoke(
        state,
        config={"configurable": {"thread_id": thread_id}},
    )
