import logging
import re
from typing import Any

from app.agent.nodes.common import emit_step, emit_thinking, parse_json_object
from app.agent.nodes.query_utils import is_compound_query
from app.config import settings
from app.llm.registry import get_provider
from app.rag import stub
from app.utils.errors import FrontendServiceError

logger = logging.getLogger(__name__)

LOCATION_RE = re.compile(r"([\u4e00-\u9fff]{2,8}(?:市|区|县|镇|街道|新区))")
RELEVANCE_JUDGE_MAX_CHUNKS = 5
RELEVANCE_JUDGE_CHUNK_CHARS = 900


def _fallback_rewrite_query(query: str) -> dict[str, str]:
    return {
        "id": "q1",
        "question": query,
        "retrieval_query": query,
        "answer_focus": query,
    }


def _dedupe_chunks(chunks) -> list:
    seen: set[str] = set()
    unique = []
    for chunk in chunks:
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        unique.append(chunk)
    return unique


def _location_aliases(name: str) -> set[str]:
    clean = (name or "").strip()
    if not clean:
        return set()
    aliases = {clean}
    for suffix in ["街道", "新区", "市", "区", "县", "镇"]:
        if clean.endswith(suffix) and len(clean) > len(suffix) + 1:
            aliases.add(clean[: -len(suffix)])
    return aliases


def _extract_location_terms(query: str) -> list[set[str]]:
    terms = []
    for match in LOCATION_RE.findall(query or ""):
        aliases = _location_aliases(match)
        if aliases:
            terms.append(aliases)
    return terms


def _filter_location_mismatch(query: str, chunks: list) -> list:
    location_terms = _extract_location_terms(query)
    if not location_terms:
        return chunks

    filtered = []
    for chunk in chunks:
        haystack = f"{chunk.document_name}\n{chunk.path}\n{chunk.content}"
        if all(any(alias in haystack for alias in aliases) for aliases in location_terms):
            filtered.append(chunk)
    return filtered


def _format_chunks_for_relevance_judge(chunks: list) -> str:
    rows = []
    for idx, chunk in enumerate(chunks[:RELEVANCE_JUDGE_MAX_CHUNKS], start=1):
        content = (chunk.content or "").strip()
        if len(content) > RELEVANCE_JUDGE_CHUNK_CHARS:
            content = f"{content[:RELEVANCE_JUDGE_CHUNK_CHARS]}..."
        rows.append(
            f"[{idx}] 来源=《{chunk.document_name}》 路径={chunk.path}\n{content}"
        )
    return "\n\n".join(rows) or "无"


async def _judge_chunks_relevant(query: str, chunks: list) -> bool:
    if not chunks:
        return False

    provider = get_provider("doubao")
    messages = [
        {
            "role": "system",
            "content": (
                "你是知识库检索结果相关性判定器。判断候选资料是否能够直接支持回答用户问题。\n"
                "判定原则：\n"
                "1. 只有资料主题、实体、地域、对象或核心概念与问题明确匹配，才判为 relevant=true。\n"
                "2. 仅有宽泛词相同不能算相关，例如都包含“深圳/旅游/报告/发展”等泛词但具体对象不同，应判为 false。\n"
                "3. 如果用户问“这个文件/这篇文档/主要内容/总结一下”，且资料来自候选文档正文，可判为 true。\n"
                "4. 只输出 JSON：{\"relevant\": true|false, \"reason\": \"...\"}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户问题：{query}\n\n"
                f"候选资料：\n{_format_chunks_for_relevance_judge(chunks)}"
            ),
        },
    ]
    try:
        result = await provider.chat(
            messages,
            model=settings.model_fast,
            temperature=0,
            max_tokens=160,
        )
        parsed = parse_json_object(result.get("text", ""), {"relevant": True})
        return bool(parsed.get("relevant", True))
    except Exception:
        logger.exception("retrieval relevance judge failed")
        return True


def _resolve_retrieval_options(profile: str | None, query: str, rewritten_count: int) -> tuple[int, bool]:
    normalized = (profile or "precise").strip().lower()
    if normalized == "report":
        return 20, True
    if normalized == "compare":
        return 20, True
    if normalized == "overview":
        return 15, True
    return 5, is_compound_query(query) or rewritten_count > 1


async def retrieve_node(state, config):
    await emit_step(config, "retrieving", "正在检索知识库...")
    query = state.get("effective_query") or state["query"]
    rewritten_queries = state.get("rewritten_queries") or [_fallback_rewrite_query(query)]
    groups: list[dict[str, Any]] = []
    all_chunks = []
    try:
        for item in rewritten_queries:
            retrieval_query = item.get("retrieval_query") or item.get("question") or query
            if item.get("requires_retrieval") is False:
                await emit_thinking(
                    config,
                    f"思考: 子问题 {item.get('id', '')} 属于闲聊/身份类问题，跳过知识库检索。",
                )
                groups.append({**item, "chunks": [], "skipped_retrieval": True})
                continue

            retrieval_profile = item.get("retrieval_profile") or "precise"
            top_k, ensure_coverage = _resolve_retrieval_options(
                retrieval_profile,
                retrieval_query,
                1,
            )
            await emit_thinking(
                config,
                f"思考: 子问题 {item.get('id', '')} 使用检索模式：{retrieval_profile}, top_k={top_k}.",
            )
            chunks = await stub.retrieve(
                kb_id=state["kb_id"],
                user_id=state["user_id"],
                query=retrieval_query,
                session_id=state["session_id"],
                doc_ids=state.get("selected_doc_ids") or None,
                temp_doc_ids=state.get("selected_temp_doc_ids") or None,
                top_k=top_k,
                search_mode=state.get("retrieval_search_mode", "hybrid"),
                ensure_document_coverage=ensure_coverage,
            )
            chunks = _filter_location_mismatch(retrieval_query, chunks)
            if chunks and not await _judge_chunks_relevant(retrieval_query, chunks):
                await emit_thinking(
                    config,
                    f"思考：子问题 {item.get('id', '')} 的检索结果与问题相关性不足，将不使用这些资料作为参考。",
                )
                chunks = []
            all_chunks.extend(chunks)
            groups.append({**item, "retrieval_profile": retrieval_profile, "top_k": top_k, "chunks": chunks})
        chunks = _dedupe_chunks(all_chunks)
    except FrontendServiceError:
        logger.exception("rag retrieve failed with frontend service error")
        raise
    except Exception:
        logger.exception("rag retrieve failed")
        chunks = []
        groups = []
    await emit_thinking(config, f"思考：资料检索完成，找到 {len(chunks)} 条可参考内容。")
    return {"retrieved_chunks": chunks, "retrieved_query_groups": groups, "rag_fallback": len(chunks) == 0}
