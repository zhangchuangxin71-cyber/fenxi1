# 模块说明：Agent 工具调用式问答流程实现。
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import Agent, Runner, function_tool
from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent
from openai.types.responses import ResponseReasoningSummaryTextDeltaEvent, ResponseTextDeltaEvent

from core.document_tools import (
    get_document_tool,
    get_page_content_tool,
    list_documents_tool,
    parse_pages_expression,
)
from core.llm_ops import DEFAULT_NO_INFO_ANSWER, is_no_info_answer
from utils.runtime import get_client_runtime

logger = logging.getLogger(__name__)


def _print_step(prefix: str, text: str):
    """打印带时间戳的步骤日志（思考/调用工具/回答）。"""
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"{ts}，{prefix}：{text}")


def _replace_doc_ids_with_names(text: str, doc_name_by_id: dict[str, str]) -> str:
    """将日志文本中的 doc_id 替换为文档名，提升可读性。"""
    if not text:
        return text
    # 1) 先替换完整 UUID 形态的 doc_id。
    full_uuid_pattern = re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        flags=re.IGNORECASE,
    )

    def repl_full(match: re.Match) -> str:
        """完整 UUID 替换函数。"""
        doc_id = match.group(0)
        return doc_name_by_id.get(doc_id, doc_id)

    replaced = full_uuid_pattern.sub(repl_full, text)

    # 2) 再替换常见缩写形态（如 559f3820... / 559f3820…）。
    # 注意：只针对当前已知 doc_id 的前缀做替换，避免误替换普通文本。
    for doc_id, doc_name in doc_name_by_id.items():
        if not isinstance(doc_id, str) or len(doc_id) < 8:
            continue
        prefix = re.escape(doc_id[:8])
        short_pattern = re.compile(
            rf"(?<![0-9a-fA-F]){prefix}(?:\.\.\.|…)(?![0-9a-fA-F])",
            flags=re.IGNORECASE,
        )
        replaced = short_pattern.sub(doc_name, replaced)

    return replaced


def _compact_reasoning_preview(text: str) -> str:
    """Compress verbose chain-of-thought style text into a brief user-facing step summary."""
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return ""
    compact = re.sub(r"(哦不对|不对|对，因为|对，|不过其实|要不要|是不是应该先\??)", "", compact)
    sentences = [part.strip(" ，。；;") for part in re.split(r"[。；;!?？!]", compact) if part.strip()]
    if not sentences:
        return ""
    preferred = None
    keywords = ("先", "接下来", "现在", "确认", "查看", "获取", "定位", "回答")
    for sentence in sentences:
        if any(key in sentence for key in keywords):
            preferred = sentence
            break
    preview = preferred or sentences[0]
    preview = re.sub(r"(所以应该|所以我需要|我现在需要|我可以|应该先)", "", preview).strip(" ，。；;")
    return preview


def _answer_implies_multi_source_support(answer: str) -> bool:
    text = str(answer or "").strip()
    if not text:
        return False
    markers = (
        "多处提及",
        "相互验证",
        "交叉验证",
        "均将",
        "均提到",
        "多个地方",
        "多处提到",
        "内容可相互验证",
        "一致表述",
    )
    return any(marker in text for marker in markers)


def _parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    raw = str(arguments or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _compact_pages(pages: list[int]) -> str:
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


def _split_into_contiguous_page_sets(pages: list[int]) -> list[set[int]]:
    """Split expanded page numbers into contiguous page-set groups."""
    ordered = sorted({int(p) for p in pages if isinstance(p, int) and int(p) > 0})
    if not ordered:
        return []
    groups: list[set[int]] = []
    current: list[int] = [ordered[0]]
    for page in ordered[1:]:
        if page == current[-1] + 1:
            current.append(page)
            continue
        groups.append(set(current))
        current = [page]
    groups.append(set(current))
    return groups


def _build_quote_from_content(
    content: str,
    max_len: int = 600,
    *,
    query: str = "",
    answer: str = "",
) -> str:
    """Extract a short quote window that prefers query/answer term hits."""
    text = str(content or "")
    # Remove markdown image payload noise (especially data:image;base64...)
    text = re.sub(r"!\[[^\]]*\]\(\s*data:image[^)]*\)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\[PAGE\s+\d+\]\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) <= max_len:
        return text

    query_terms = [t for t in _extract_terms(query) if len(t) >= 2]
    answer_terms = [t for t in _extract_terms(answer) if len(t) >= 2]
    prioritized_terms = [*answer_terms, *query_terms]
    lower_text = text.lower()

    hit_pos: int | None = None
    for term in prioritized_terms:
        pos = lower_text.find(str(term).lower())
        if pos >= 0:
            hit_pos = pos
            break

    if hit_pos is None:
        return text[:max_len]

    half = max_len // 2
    start = max(0, hit_pos - half)
    end = min(len(text), start + max_len)
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def _extract_terms(text: str) -> set[str]:
    """Extract coarse terms for lightweight relevance scoring."""
    raw = str(text or "").lower()
    if not raw:
        return set()
    # Include numbers (e.g. 54, 36.9) so fact-centric QA can align evidence pages better.
    parts = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{3,}|\d+(?:\.\d+)?", raw)
    return {p for p in parts if p}


_GENERIC_FOCUS_TERMS = {
    "什么", "哪些", "怎么", "如何", "是否", "有没有", "介绍", "情况", "信息",
    "这个", "那个", "问题", "内容", "资料", "文档", "当前", "范围",
    "根据", "出自", "其中", "分别",
}


def _expand_cjk_ngrams(term: str) -> set[str]:
    raw = str(term or "").strip()
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,}", raw):
        return set()
    cleaned = raw
    for stop in ("是什么", "什么", "哪些", "哪个", "多少", "怎么", "如何", "吗", "呢", "的"):
        cleaned = cleaned.replace(stop, "")
    base = cleaned if len(cleaned) >= 2 else raw
    out: set[str] = set()
    if len(base) <= 4:
        out.add(base)
        return out
    max_n = min(4, len(base))
    for n in range(2, max_n + 1):
        for idx in range(0, len(base) - n + 1):
            out.add(base[idx: idx + n])
    return out


def _extract_focus_terms(query: str, answer: str) -> set[str]:
    query_terms = {t for t in _extract_terms(query) if len(t) >= 2}
    answer_terms = {t for t in _extract_terms(answer) if len(t) >= 2}
    expanded_query_terms: set[str] = set(query_terms)
    for term in query_terms:
        expanded_query_terms.update(_expand_cjk_ngrams(term))
    expanded_answer_terms: set[str] = set(answer_terms)
    for term in answer_terms:
        expanded_answer_terms.update(_expand_cjk_ngrams(term))
    merged = {t for t in (expanded_query_terms | expanded_answer_terms) if t not in _GENERIC_FOCUS_TERMS}
    # Keep set compact and bias to meaningful terms.
    return {
        t for t in merged
        if not (t.isdigit() and len(t) <= 2)
        and not re.fullmatch(r"[\u4e00-\u9fff]{1}", t)
    }


def _extract_query_anchor_terms(query: str) -> set[str]:
    raw = str(query or "")
    if not raw:
        return set()
    anchors: set[str] = set()
    for seq in re.findall(r"[\u4e00-\u9fff]{2,}", raw):
        cleaned = seq
        for stop in ("是什么", "什么", "哪些", "哪个", "多少", "怎么", "如何", "吗", "呢", "的"):
            cleaned = cleaned.replace(stop, "")
        if len(cleaned) >= 2:
            anchors.add(cleaned)
            max_n = min(4, len(cleaned))
            for n in range(2, max_n + 1):
                for idx in range(0, len(cleaned) - n + 1):
                    anchors.add(cleaned[idx: idx + n])
    for token in re.findall(r"[a-z0-9]{3,}", raw.lower()):
        anchors.add(token)
    return {t for t in anchors if t and t not in _GENERIC_FOCUS_TERMS}


def _extract_strict_query_anchor_phrases(query: str) -> set[str]:
    """Extract stricter query anchors (full cleaned phrases) to reduce near-match drift."""
    raw = str(query or "")
    if not raw:
        return set()
    phrases: set[str] = set()
    for seq in re.findall(r"[\u4e00-\u9fff]{3,}", raw):
        cleaned = seq
        for stop in ("是什么", "什么", "哪些", "哪个", "多少", "怎么", "如何", "吗", "呢", "的"):
            cleaned = cleaned.replace(stop, "")
        cleaned = cleaned.strip()
        if len(cleaned) >= 3 and cleaned not in _GENERIC_FOCUS_TERMS:
            phrases.add(cleaned)
    for token in re.findall(r"[a-z0-9]{4,}", raw.lower()):
        if token not in _GENERIC_FOCUS_TERMS:
            phrases.add(token)
    return phrases


def _snippet_hits_focus_terms(snippet: dict, focus_terms: set[str]) -> bool:
    if not focus_terms:
        return True
    body = f"{str(snippet.get('quote', '') or '')} {str(snippet.get('content', '') or '')}"
    lowered = body.lower()
    return any(str(term).lower() in lowered for term in focus_terms)


def _snippet_focus_hit_count(snippet: dict, focus_terms: set[str]) -> int:
    if not focus_terms:
        return 0
    body = f"{str(snippet.get('quote', '') or '')} {str(snippet.get('content', '') or '')}"
    lowered = body.lower()
    return sum(1 for term in focus_terms if str(term).lower() in lowered)


def _is_toc_heavy_content(text: str) -> bool:
    raw = str(text or "")
    if not raw:
        return False
    toc_anchor_hits = len(re.findall(r"\(#_toc[0-9a-z_]+\)", raw, flags=re.IGNORECASE))
    bracket_link_hits = len(re.findall(r"\[[^\]]{1,80}\]\([^)]+\)", raw))
    page_marker_hits = len(re.findall(r"\[\[PAGE\s+\d+\]\]", raw, flags=re.IGNORECASE))
    heading_catalog_hits = len(re.findall(r"(chapter\d+|目录|contents)", raw, flags=re.IGNORECASE))
    return (
        toc_anchor_hits >= 2
        or bracket_link_hits >= 8
        or (bracket_link_hits >= 4 and (page_marker_hits >= 1 or heading_catalog_hits >= 1))
    )


def _extract_anchor_terms_from_structure_text(text: str) -> set[str]:
    raw = str(text or "")
    if not raw:
        return set()
    anchors: set[str] = set()
    # Strict list-value extraction: keep only short value items from enumerations like "A、B、C".
    list_matches = re.findall(r"[—\-：:]\s*([^。；;\n]+)", raw)
    for tail in list_matches:
        tail_text = str(tail).strip()
        if not re.search(r"[、,，/]", tail_text):
            continue
        for part in re.split(r"[、,，/]", tail_text):
            token = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", part).strip()
            if 1 < len(token) <= 8 and token not in _GENERIC_FOCUS_TERMS:
                anchors.add(token)
    # Conservative fallback: do not broaden to arbitrary structure words.
    # Let focus_terms from query/answer drive matching when explicit list anchors are unavailable.
    return anchors


def _snippet_relevance_score(*, quote: str, query: str, answer: str) -> int:
    """Score snippet relevance to current query/answer with simple token overlap."""
    quote_terms = _extract_terms(quote)
    if not quote_terms:
        return 0
    query_terms = _extract_terms(query)
    answer_terms = _extract_terms(answer)
    score = 0
    score += len(quote_terms & query_terms) * 3
    score += len(quote_terms & answer_terms) * 2
    return score


def _extract_numeric_terms(text: str) -> set[str]:
    """Extract numeric facts as anchor terms, e.g. 54 / 36.9."""
    raw = str(text or "")
    if not raw:
        return set()
    return {token for token in re.findall(r"\d+(?:\.\d+)?", raw) if token}


def _estimate_recommendation_item_count(text: str) -> int:
    """Estimate list item count from answer text for evidence alignment."""
    raw = str(text or "").strip()
    if not raw:
        return 0
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    numbered = sum(1 for line in lines if re.match(r"^\d+\s*[\.\)、)]\s*", line))
    if numbered >= 2:
        return min(6, numbered)
    bullet = sum(1 for line in lines if re.match(r"^[-*•]\s+", line))
    if bullet >= 2:
        return min(6, bullet)
    return 0


def _extract_recommendation_focus_terms(text: str) -> list[str]:
    """Extract likely item labels from recommendation-style numbered lines."""
    raw = str(text or "").strip()
    if not raw:
        return []
    terms: list[str] = []
    for line in [ln.strip() for ln in raw.splitlines() if ln.strip()]:
        m = re.match(r"^\d+\s*[\.\)、)]\s*(.+)$", line)
        if not m:
            continue
        tail = m.group(1).strip()
        bold = re.match(r"^\*\*(.+?)\*\*", tail)
        if bold:
            term = bold.group(1).strip()
        else:
            term = re.split(r"[：:（(，,。 ]", tail, maxsplit=1)[0].strip()
        if len(term) >= 2:
            terms.append(term[:30])
    deduped: list[str] = []
    seen = set()
    for term in terms:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(term)
    return deduped[:6]


def _compact_page_range(start_page: int, end_page: int) -> str:
    if start_page <= 0:
        return ""
    if end_page <= 0 or end_page < start_page:
        end_page = start_page
    if start_page == end_page:
        return str(start_page)
    return f"{start_page}-{end_page}"


def _citation_unit_by_doc_type(doc_type: str) -> str:
    t = str(doc_type or "").strip().lower()
    return "页" if t == "pdf" else "分片"


def _extract_structure_candidates(structure: Any) -> list[dict[str, Any]]:
    """Extract page-range candidates from structure nodes."""
    candidates: list[dict[str, Any]] = []

    def walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            start_index = node.get("start_index")
            end_index = node.get("end_index")
            if isinstance(start_index, int) and start_index > 0:
                end_page = end_index if isinstance(end_index, int) and end_index >= start_index else start_index
                title = str(node.get("title", "") or "").strip()
                summary = str(node.get("summary", "") or "").strip()
                text = str(node.get("text", "") or "").strip()
                parts = [part for part in (title, summary, text) if part]
                content = re.sub(r"\s+", " ", " ".join(parts)).strip()
                if content:
                    content = content[:2000]
                    candidates.append(
                        {
                            "start_page": start_index,
                            "end_page": end_page,
                            "page_range": _compact_page_range(start_index, end_page),
                            "content": content,
                            "quote": _build_quote_from_content(content, max_len=600),
                        }
                    )
            if node.get("nodes"):
                walk(node.get("nodes"))

    walk(structure)
    return candidates


def _normalize_agent_answer_with_citations(answer: str, evidence: list[dict]) -> tuple[str, list[dict]]:
    cleaned_answer = str(answer or "").strip()
    if cleaned_answer:
        if "参考依据：" in cleaned_answer:
            cleaned_answer = cleaned_answer.split("参考依据：", 1)[0].rstrip()
        if "证据片段：" in cleaned_answer:
            cleaned_answer = cleaned_answer.split("证据片段：", 1)[0].rstrip()
        # Remove duplicated single-source disclaimer from body text.
        redundant_notices = (
            "当前检索范围内其他文档未检索到可核验信息。",
            "当前检索范围内其他文档未检索到可核验信息",
            "检索说明：在本次候选文档范围内，未发现其他文档的可核验证据。",
        )
        for notice in redundant_notices:
            cleaned_answer = cleaned_answer.replace(notice, "")
        cleaned_answer = re.sub(r"\n{3,}", "\n\n", cleaned_answer).strip()
    if not cleaned_answer:
        return "", []
    # Keep "no information" answer clean so upstream fallback can detect it reliably.
    if is_no_info_answer(cleaned_answer):
        return cleaned_answer, []

    citation_items: list[dict] = []
    for item in evidence:
        doc_name = str(item.get("doc_name", "") or "").strip()
        doc_type = str(item.get("doc_type", "") or "").strip().lower()
        verified_pages = [int(page) for page in item.get("verified_pages", []) if isinstance(page, int) and page > 0]
        if not verified_pages:
            fetched_pages = str(item.get("fetched_pages", "") or "").strip()
            with_pages = []
            if fetched_pages:
                try:
                    with_pages = parse_pages_expression(fetched_pages)
                except Exception:
                    with_pages = []
            verified_pages = [int(page) for page in with_pages if int(page) > 0]
        if not doc_name or not verified_pages:
            continue
        page_expr = _compact_pages(verified_pages)
        if not page_expr:
            continue
        citation_items.append(
            {
                "doc_name": doc_name,
                "doc_id": item.get("doc_id", ""),
                "pages": page_expr,
                "unit": _citation_unit_by_doc_type(doc_type),
            }
        )

    if not citation_items:
        return cleaned_answer, []

    lines = ["参考依据："]
    for citation in citation_items:
        lines.append(f"- 《{citation['doc_name']}》 第{citation['pages']}{citation.get('unit', '页')}")
    if len(citation_items) == 1:
        single_source_notice = "检索说明：在本次候选文档范围内，未发现其他文档的可核验证据。"
        if single_source_notice not in cleaned_answer:
            lines.append(single_source_notice)
    citation_block = "\n".join(lines)
    if "参考依据：" in cleaned_answer:
        return cleaned_answer, citation_items
    return f"{cleaned_answer}\n\n{citation_block}", citation_items


def _normalize_no_info_surface_text(answer: str) -> str:
    """Normalize no-info answers to the canonical default text for consistent UX."""
    text = str(answer or "").strip()
    if not text:
        return text
    if is_no_info_answer(text):
        return DEFAULT_NO_INFO_ANSWER
    return text


async def query_agent_multi_doc(
    client,
    prompt: str,
    *,
    log_event,
    verbose: bool = False,
    stream_output: bool = True,
    top_k_docs: int = 6,
    allowed_doc_ids: set[str] | None = None,
    bm25_prefilter=None,
    bm25_prefilter_top_k: int = 8,
    return_details: bool = False,
) -> str | dict:
    """Agent 工具调用式多文档问答主入口。"""
    runtime = get_client_runtime(client)
    top_k_docs = max(0, int(top_k_docs))
    doc_name_by_id = {
        str(doc_id): str((doc or {}).get("doc_name", "") or doc_id)
        for doc_id, doc in getattr(client, "documents", {}).items()
    }
    runtime_allowed_doc_ids = allowed_doc_ids
    if bm25_prefilter is not None and bm25_prefilter_top_k > 0:
        filtered_docs = bm25_prefilter.retrieve(
            query=prompt,
            top_k=bm25_prefilter_top_k,
            allowed_doc_ids=allowed_doc_ids,
        )
        filtered_ids: list[str] = []
        seen_filtered_ids: set[str] = set()
        for item in filtered_docs:
            doc_id = str(item.get("doc_id", "") or "").strip()
            if not doc_id or doc_id in seen_filtered_ids:
                continue
            seen_filtered_ids.add(doc_id)
            filtered_ids.append(doc_id)
        if top_k_docs > 0:
            filtered_ids = filtered_ids[:top_k_docs]
        runtime_allowed_doc_ids = set(filtered_ids)
        if not runtime_allowed_doc_ids and allowed_doc_ids is not None:
            runtime_allowed_doc_ids = allowed_doc_ids
    elif top_k_docs > 0:
        candidate_docs = list_documents_tool(client, allowed_doc_ids=allowed_doc_ids)
        limited_doc_ids = [
            str(item.get("doc_id", "") or "").strip()
            for item in candidate_docs
            if str(item.get("doc_id", "") or "").strip()
        ][:top_k_docs]
        runtime_allowed_doc_ids = set(limited_doc_ids)
        if not runtime_allowed_doc_ids and allowed_doc_ids is not None:
            runtime_allowed_doc_ids = allowed_doc_ids

    scoped_catalog = list_documents_tool(client, allowed_doc_ids=runtime_allowed_doc_ids)
    scoped_names: list[str] = []
    seen_scoped_names: set[str] = set()
    for item in scoped_catalog:
        raw_name = str(item.get("doc_name", "") or "").strip()
        if not raw_name:
            continue
        pretty_name = Path(raw_name).stem
        if pretty_name in seen_scoped_names:
            continue
        seen_scoped_names.add(pretty_name)
        scoped_names.append(pretty_name)

    agent_instructions = runtime.prompts.agent_system
    if allowed_doc_ids is not None and scoped_names:
        agent_instructions = (
            f"{runtime.prompts.agent_system}\n\n"
            "当前问答范围已经被用户限定，不要把任务表述成查看整个资料库。"
            " 如需确认可用文档，只需在当前限定范围内判断。"
            f" 当前可用文档：{', '.join(scoped_names)}。"
        )

    def ensure_doc_allowed(doc_id: str):
        """校验 doc_id 是否在本轮允许范围内。"""
        if runtime_allowed_doc_ids is not None and doc_id not in runtime_allowed_doc_ids:
            raise ValueError(f"doc_id '{doc_id}' is not in restricted docs.")

    @function_tool
    def list_documents() -> str:
        """Return the indexed document catalog with doc_id, name, description, type, page count, and index mode."""
        return json.dumps(list_documents_tool(client, allowed_doc_ids=runtime_allowed_doc_ids), ensure_ascii=False)

    @function_tool
    def get_document(doc_id: str) -> str:
        """Get one document's metadata by doc_id, including status, page count, name, and description."""
        ensure_doc_allowed(doc_id)
        return get_document_tool(client, doc_id)

    @function_tool
    def get_document_structure(doc_id: str) -> str:
        """Get one document's tree structure by doc_id to identify relevant sections and page ranges."""
        ensure_doc_allowed(doc_id)
        structure_json = client.get_document_structure(doc_id)
        if os.getenv("DEBUG_BREAKPOINT_GET_DOCUMENT_STRUCTURE", "0") == "1":
            breakpoint()
        return structure_json

    @function_tool
    def get_page_content(doc_id: str, pages: str) -> str:
        """
        Get text content from one document by doc_id and a tight pages expression.
        Example pages values: '5-7', '12', '3,8'.
        """
        ensure_doc_allowed(doc_id)
        return json.dumps(get_page_content_tool(client, doc_id, pages), ensure_ascii=False)

    agent = Agent(
        name="PageIndexMultiDoc",
        instructions=agent_instructions,
        tools=[list_documents, get_document, get_document_structure, get_page_content],
        model=client.retrieve_model,
    )

    async def _run():
        """执行一次 Agent 流式会话并组装最终证据结果。"""
        streamed_run = Runner.run_streamed(agent, prompt)
        current_stream_kind = None
        stream_buffer: list[str] = []
        step_counter = 0
        last_reasoning_preview = ""
        last_tool_name = ""
        last_tool_args: dict[str, Any] = {}
        selected_doc_ids: list[str] = []
        selected_doc_id_set: set[str] = set()
        evidence_by_doc: dict[str, dict[str, Any]] = {}

        def flush_stream_buffer():
            """刷新流式缓冲区并按模式输出。"""
            nonlocal stream_buffer, current_stream_kind, last_reasoning_preview
            if not stream_output or not stream_buffer or current_stream_kind is None:
                stream_buffer = []
                return
            text = "".join(stream_buffer).strip()
            if text:
                # 非 verbose 模式下，推理文本采用短预览，避免刷屏。
                if not verbose and current_stream_kind == "reasoning":
                    compact = re.sub(r"\s+", " ", text).strip()
                    compact = _replace_doc_ids_with_names(compact, doc_name_by_id)
                    preview = _compact_reasoning_preview(compact)
                    if preview and preview != last_reasoning_preview:
                        _print_step("思考", preview)
                        last_reasoning_preview = preview
                elif verbose:
                    verbose_text = _replace_doc_ids_with_names(text, doc_name_by_id)
                    log_event("agent_stream", kind=current_stream_kind, text_preview=verbose_text[:1200])
            stream_buffer = []

        async for event in streamed_run.stream_events():
            if isinstance(event, RawResponsesStreamEvent):
                if isinstance(event.data, ResponseReasoningSummaryTextDeltaEvent):
                    if stream_output:
                        if current_stream_kind != "reasoning":
                            flush_stream_buffer()
                        stream_buffer.append(event.data.delta)
                    current_stream_kind = "reasoning"
                elif isinstance(event.data, ResponseTextDeltaEvent):
                    if stream_output:
                        if current_stream_kind != "text":
                            flush_stream_buffer()
                        stream_buffer.append(event.data.delta)
                    current_stream_kind = "text"
            elif isinstance(event, RunItemStreamEvent):
                item = event.item
                if item.type == "tool_call_item":
                    step_counter += 1
                    raw = item.raw_item
                    tool_name = str(getattr(raw, "name", "") or "")
                    tool_args = _parse_tool_arguments(getattr(raw, "arguments", {}))
                    last_tool_name = tool_name
                    last_tool_args = tool_args
                    doc_id = str(tool_args.get("doc_id", "") or "").strip()
                    if doc_id:
                        doc_meta = (getattr(client, "documents", {}) or {}).get(doc_id, {})
                        if doc_id not in selected_doc_id_set:
                            selected_doc_ids.append(doc_id)
                            selected_doc_id_set.add(doc_id)
                        slot = evidence_by_doc.setdefault(
                            doc_id,
                            {
                                "doc_id": doc_id,
                                "doc_name": doc_name_by_id.get(doc_id, doc_id),
                                "doc_type": str(doc_meta.get("type", "") or "").strip().lower(),
                                "selected_page_ranges": [],
                                "requested_pages": [],
                                "verified_pages": [],
                                "snippets": [],
                                "structure_candidates": [],
                            },
                        )
                        if tool_name == "get_page_content":
                            page_expr = str(tool_args.get("pages", "") or "").strip()
                            if page_expr:
                                slot["selected_page_ranges"].append(page_expr)
                                try:
                                    slot["requested_pages"].extend(parse_pages_expression(page_expr))
                                except Exception:
                                    pass
                    if stream_output:
                        flush_stream_buffer()
                        args = getattr(raw, "arguments", "{}")
                        args = _replace_doc_ids_with_names(str(args), doc_name_by_id)
                        args_payload = str(args)[:1200] if verbose else ""
                        if verbose:
                            log_event(
                                "agent_step",
                                step="tool_call",
                                index=step_counter,
                                tool_name=getattr(raw, "name", ""),
                                args=args_payload,
                            )
                        else:
                            _print_step("调用工具", f"{getattr(raw, 'name', '')}（步骤{step_counter}）")
                    current_stream_kind = None
                elif item.type == "tool_call_output_item":
                    if last_tool_name == "get_page_content":
                        doc_id = str(last_tool_args.get("doc_id", "") or "").strip()
                        output_text = str(item.output or "")
                        if doc_id and output_text:
                            try:
                                payload = json.loads(output_text)
                            except Exception:
                                payload = []
                            if isinstance(payload, list):
                                slot = evidence_by_doc.setdefault(
                                    doc_id,
                                    {
                                        "doc_id": doc_id,
                                        "doc_name": doc_name_by_id.get(doc_id, doc_id),
                                        "doc_type": str(((getattr(client, "documents", {}) or {}).get(doc_id, {}) or {}).get("type", "") or "").strip().lower(),
                                        "selected_page_ranges": [],
                                        "requested_pages": [],
                                        "verified_pages": [],
                                        "snippets": [],
                                        "structure_candidates": [],
                                    },
                                )
                                for row in payload:
                                    if not isinstance(row, dict):
                                        continue
                                    page_value = row.get("page")
                                    try:
                                        page_int = int(page_value)
                                    except Exception:
                                        continue
                                    if page_int > 0:
                                        slot["verified_pages"].append(page_int)
                                    content_text = str(row.get("content", "") or "")
                                    if _is_toc_heavy_content(content_text):
                                        continue
                                    snippet = {
                                        "page": page_int,
                                        # Keep enough source text for traceability; avoid early head truncation.
                                        "content": content_text[:12000],
                                        "quote": _build_quote_from_content(
                                            content_text,
                                            max_len=600,
                                            query=prompt,
                                        ),
                                        "source_tool": "get_page_content",
                                    }
                                    slot["snippets"].append(snippet)
                    elif last_tool_name == "get_document_structure":
                        doc_id = str(last_tool_args.get("doc_id", "") or "").strip()
                        output_text = str(item.output or "")
                        if doc_id and output_text:
                            try:
                                structure_payload = json.loads(output_text)
                            except Exception:
                                structure_payload = []
                            if isinstance(structure_payload, list):
                                slot = evidence_by_doc.setdefault(
                                    doc_id,
                                    {
                                        "doc_id": doc_id,
                                        "doc_name": doc_name_by_id.get(doc_id, doc_id),
                                        "doc_type": str(((getattr(client, "documents", {}) or {}).get(doc_id, {}) or {}).get("type", "") or "").strip().lower(),
                                        "selected_page_ranges": [],
                                        "requested_pages": [],
                                        "verified_pages": [],
                                        "snippets": [],
                                        "structure_candidates": [],
                                    },
                                )
                                slot["structure_candidates"].extend(_extract_structure_candidates(structure_payload))
                    if verbose and stream_output:
                        flush_stream_buffer()
                        output = str(item.output)
                        output = _replace_doc_ids_with_names(output, doc_name_by_id)
                        preview = output[:400] + "..." if len(output) > 400 else output
                        log_event("agent_step", step="tool_output", output_preview=preview)
                    current_stream_kind = None

        flush_stream_buffer()
        final_output = "" if not streamed_run.final_output else str(streamed_run.final_output)
        final_output = _normalize_no_info_surface_text(final_output)
        should_print_final_answer = bool(stream_output and not verbose)

        selected_documents: list[dict] = []
        for doc_id in selected_doc_ids:
            doc_meta = (getattr(client, "documents", {}) or {}).get(doc_id, {})
            selected_documents.append(
                {
                    "doc_id": doc_id,
                    "doc_name": str(doc_meta.get("doc_name", "") or doc_name_by_id.get(doc_id, doc_id)),
                    "doc_description": str(doc_meta.get("doc_description", "") or ""),
                    "index_mode": str(doc_meta.get("index_mode", "standard") or "standard"),
                }
            )

        evidence: list[dict] = []
        for doc_id in selected_doc_ids:
            slot = evidence_by_doc.get(doc_id)
            if slot is None:
                continue
            selected_ranges: list[str] = []
            seen_ranges = set()
            for value in slot.get("selected_page_ranges", []):
                text = str(value or "").strip()
                if not text or text in seen_ranges:
                    continue
                seen_ranges.add(text)
                selected_ranges.append(text)
            verified_pages = sorted({int(page) for page in slot.get("verified_pages", []) if isinstance(page, int) and int(page) > 0})
            requested_pages = sorted({int(page) for page in slot.get("requested_pages", []) if isinstance(page, int) and int(page) > 0})
            fetched_pages = _compact_pages(verified_pages or requested_pages)
            snippets = []
            snippet_unit = _citation_unit_by_doc_type(str(slot.get("doc_type", "") or "").strip().lower())
            seen_snippet_pages = set()
            for snippet in slot.get("snippets", []):
                if not isinstance(snippet, dict):
                    continue
                page_val = snippet.get("page")
                try:
                    page_int = int(page_val)
                except Exception:
                    continue
                if page_int <= 0 or page_int in seen_snippet_pages:
                    continue
                seen_snippet_pages.add(page_int)
                snippets.append(
                    {
                        "page": page_int,
                        "page_range": str(snippet.get("page_range", "") or str(page_int)),
                        "content": str(snippet.get("content", "") or ""),
                        # Always re-generate quote with both query+answer for best alignment.
                        "quote": _build_quote_from_content(
                            str(snippet.get("content", "") or ""),
                            max_len=600,
                            query=prompt,
                            answer=final_output,
                        ),
                        "source_tool": str(snippet.get("source_tool", "") or "get_page_content"),
                        "unit": snippet_unit,
                    }
                )
            snippets.sort(key=lambda x: x["page"])
            # Keep only the most relevant snippets for final evidence display.
            if snippets:
                answer_num_terms = _extract_numeric_terms(final_output)
                query_num_terms = _extract_numeric_terms(prompt)
                answer_terms = _extract_terms(final_output)
                query_terms = _extract_terms(prompt)
                key_terms = {t for t in (answer_terms | query_terms) if len(t) >= 2 and not t.isdigit()}
                focus_terms = _extract_focus_terms(prompt, final_output)
                query_focus_terms = _extract_query_anchor_terms(prompt)
                match_terms = query_focus_terms or focus_terms
                direct_page_snippets = [
                    s for s in snippets
                    if str(s.get("source_tool", "") or "").strip() == "get_page_content"
                ]
                has_direct_page_snippets = bool(direct_page_snippets)
                usable_direct_snippets = [
                    s for s in direct_page_snippets
                    if _snippet_hits_focus_terms(s, match_terms)
                ]

                def _score_snippet(s: dict) -> int:
                    body = f"{str(s.get('quote', '') or '')} {str(s.get('content', '') or '')}"
                    base = _snippet_relevance_score(
                        quote=body,
                        query=prompt,
                        answer=final_output,
                    )
                    body_terms = _extract_terms(body)
                    body_nums = _extract_numeric_terms(body)
                    num_score = len(body_nums & answer_num_terms) * 8 + len(body_nums & query_num_terms) * 5
                    key_score = len(body_terms & key_terms) * 2
                    focus_score = _snippet_focus_hit_count(s, match_terms) * 10
                    # If answer has numeric facts, prefer snippets that carry at least one.
                    if answer_num_terms and not (body_nums & answer_num_terms):
                        num_score -= 4
                    return base + num_score + key_score + focus_score

                ranked_base = usable_direct_snippets if usable_direct_snippets else snippets
                ranked = sorted(
                    ranked_base,
                    key=_score_snippet,
                    reverse=True,
                )
                focus_ranked = [item for item in ranked if _snippet_hits_focus_terms(item, match_terms)]
                if focus_ranked:
                    ranked = focus_ranked

                # If direct get_page_content snippets are not query/answer-aligned,
                # probe around structure candidate ranges and refill evidence from page content.
                top_score = _score_snippet(ranked[0]) if ranked else 0
                has_focus_hit = any(_snippet_hits_focus_terms(item, match_terms) for item in ranked)
                need_structure_probe = (top_score <= 0 or not has_focus_hit) and slot.get("structure_candidates")
                # If we already have usable direct get_page_content snippets, never override with structure probing.
                if has_direct_page_snippets and usable_direct_snippets:
                    need_structure_probe = False
                if need_structure_probe:
                    structure_candidates = []
                    for candidate in slot.get("structure_candidates", []):
                        if not isinstance(candidate, dict):
                            continue
                        start_page = candidate.get("start_page")
                        end_page = candidate.get("end_page")
                        if not isinstance(start_page, int) or start_page <= 0:
                            continue
                        if not isinstance(end_page, int) or end_page < start_page:
                            end_page = start_page
                        structure_candidates.append(
                            {
                                "start_page": start_page,
                                "end_page": end_page,
                                "quote": str(candidate.get("quote", "") or ""),
                                "content": str(candidate.get("content", "") or ""),
                            }
                        )
                    if structure_candidates:
                        structure_candidates = sorted(
                            structure_candidates,
                            key=lambda s: _snippet_relevance_score(
                                quote=s.get("quote", "") or s.get("content", ""),
                                query=prompt,
                                answer=final_output,
                            ),
                            reverse=True,
                        )
                        page_count_cap = int(((getattr(client, "documents", {}) or {}).get(doc_id, {}) or {}).get("page_count", 0) or 0)
                        probe_pages: set[int] = set()
                        for picked in structure_candidates[:2]:
                            for base in range(int(picked["start_page"]), int(picked["end_page"]) + 1):
                                for shift in (-2, -1, 0, 1, 2):
                                    p = base + shift
                                    if p <= 0:
                                        continue
                                    if page_count_cap > 0 and p > page_count_cap:
                                        continue
                                    probe_pages.add(p)
                        probe_expr = _compact_pages(sorted(probe_pages))
                        if probe_expr:
                            try:
                                payload_probe = get_page_content_tool(client, doc_id, probe_expr)
                            except Exception:
                                payload_probe = []
                            if isinstance(payload_probe, list):
                                probe_snippets: list[dict] = []
                                seen_probe_pages: set[int] = set()
                                for row in payload_probe:
                                    if not isinstance(row, dict):
                                        continue
                                    try:
                                        page_int = int(row.get("page"))
                                    except Exception:
                                        continue
                                    if page_int <= 0 or page_int in seen_probe_pages:
                                        continue
                                    seen_probe_pages.add(page_int)
                                    content_text = str(row.get("content", "") or "")
                                    if _is_toc_heavy_content(content_text):
                                        continue
                                    probe_snippets.append(
                                        {
                                            "page": page_int,
                                            "page_range": str(page_int),
                                            "content": content_text[:12000],
                                            "quote": _build_quote_from_content(
                                                content_text,
                                                max_len=600,
                                                query=prompt,
                                                answer=final_output,
                                            ),
                                            "source_tool": "get_page_content",
                                            "unit": snippet_unit,
                                        }
                                    )
                                if probe_snippets:
                                    probe_focus_ranked = [
                                        item for item in probe_snippets
                                        if _snippet_hits_focus_terms(item, match_terms)
                                    ]
                                    if probe_focus_ranked:
                                        probe_snippets = probe_focus_ranked
                                    snippets = probe_snippets
                                    ranked = sorted(
                                        snippets,
                                        key=_score_snippet,
                                        reverse=True,
                                    )

                # Rescue path: when direct page snippets exist but none are query-aligned,
                # re-scan loaded pages by query anchors and refill snippets.
                if (not any(_snippet_hits_focus_terms(item, match_terms) for item in ranked)) and match_terms:
                    keyword_ranked_pages: list[tuple[int, int]] = []
                    try:
                        doc_meta = ((getattr(client, "documents", {}) or {}).get(doc_id, {}) or {})
                        page_entries = doc_meta.get("pages")
                        if isinstance(page_entries, list):
                            for row in page_entries:
                                if not isinstance(row, dict):
                                    continue
                                try:
                                    page_int = int(row.get("page"))
                                except Exception:
                                    continue
                                if page_int <= 0:
                                    continue
                                content_text = str(row.get("content", "") or "")
                                if _is_toc_heavy_content(content_text):
                                    continue
                                probe_item = {"quote": "", "content": content_text}
                                hit_count = _snippet_focus_hit_count(probe_item, match_terms)
                                if hit_count <= 0:
                                    continue
                                keyword_ranked_pages.append((hit_count, page_int))
                    except Exception:
                        keyword_ranked_pages = []

                    if keyword_ranked_pages:
                        keyword_ranked_pages = sorted(
                            keyword_ranked_pages,
                            key=lambda item: (-int(item[0]), int(item[1])),
                        )
                        keyword_pages = [page for _, page in keyword_ranked_pages[:3]]
                        keyword_expr = _compact_pages(keyword_pages)
                        try:
                            keyword_payload = get_page_content_tool(client, doc_id, keyword_expr)
                        except Exception:
                            keyword_payload = []
                        recovered_snippets: list[dict] = []
                        if isinstance(keyword_payload, list):
                            for row in keyword_payload:
                                if not isinstance(row, dict):
                                    continue
                                try:
                                    page_int = int(row.get("page"))
                                except Exception:
                                    continue
                                if page_int <= 0:
                                    continue
                                content_text = str(row.get("content", "") or "")
                                if _is_toc_heavy_content(content_text):
                                    continue
                                probe_item = {"quote": "", "content": content_text}
                                if _snippet_focus_hit_count(probe_item, match_terms) <= 0:
                                    continue
                                recovered_snippets.append(
                                    {
                                        "page": page_int,
                                        "page_range": str(page_int),
                                        "content": content_text[:12000],
                                        "quote": _build_quote_from_content(
                                            content_text,
                                            max_len=600,
                                            query=prompt,
                                            answer=final_output,
                                        ),
                                        "source_tool": "get_page_content",
                                        "unit": snippet_unit,
                                    }
                                )
                        if recovered_snippets:
                            snippets = recovered_snippets
                            ranked = sorted(
                                snippets,
                                key=_score_snippet,
                                reverse=True,
                            )

                selected_ranked: list[dict] = []
                selected_pages: set[int] = set()
                parsed_ranges: list[set[int]] = []
                requested_page_pool: set[int] = set()
                for expr in selected_ranges:
                    try:
                        expanded_pages = [int(p) for p in parse_pages_expression(expr) if int(p) > 0]
                    except Exception:
                        expanded_pages = []
                    if expanded_pages:
                        requested_page_pool.update(expanded_pages)
                        parsed_ranges.extend(_split_into_contiguous_page_sets(expanded_pages))

                # Keep one representative snippet per requested page range.
                for range_pages in parsed_ranges:
                    fact_candidates = [
                        item
                        for item in ranked
                        if int(item.get("page", 0)) in range_pages
                        and _score_snippet(item) > 0
                        and _snippet_focus_hit_count(item, match_terms) > 0
                        and int(item.get("page", 0)) not in selected_pages
                    ]
                    candidate = fact_candidates[0] if fact_candidates else None
                    if candidate is None:
                        continue
                    page_no = int(candidate.get("page", 0))
                    if page_no <= 0:
                        continue
                    selected_ranked.append(candidate)
                    selected_pages.add(page_no)

                # Ensure multi-fact answers are backed by multiple numeric anchors when possible.
                if answer_num_terms:
                    snippet_num_terms: list[set[str]] = []
                    num_support_count: dict[str, int] = {}
                    for item in ranked:
                        body = f"{str(item.get('quote', '') or '')} {str(item.get('content', '') or '')}"
                        nums = _extract_numeric_terms(body)
                        snippet_num_terms.append(nums)
                        for num in nums:
                            if num in answer_num_terms:
                                num_support_count[num] = num_support_count.get(num, 0) + 1
                    anchor_nums = [
                        num
                        for num in sorted(
                            num_support_count.keys(),
                            key=lambda n: (num_support_count.get(n, 0), len(n), n),
                        )
                    ][:3]
                    covered_nums: set[str] = set()
                    for chosen in selected_ranked:
                        body = f"{str(chosen.get('quote', '') or '')} {str(chosen.get('content', '') or '')}"
                        covered_nums.update(_extract_numeric_terms(body) & set(anchor_nums))
                    for anchor in anchor_nums:
                        if anchor in covered_nums:
                            continue
                        candidate = None
                        for idx, item in enumerate(ranked):
                            page_no = int(item.get("page", 0))
                            if page_no <= 0 or page_no in selected_pages:
                                continue
                            if anchor in snippet_num_terms[idx]:
                                candidate = item
                                break
                        if candidate is None:
                            continue
                        page_no = int(candidate.get("page", 0))
                        selected_ranked.append(candidate)
                        selected_pages.add(page_no)
                        covered_nums.add(anchor)

                # Fallback for single-range/no-range cases.
                if not selected_ranked:
                    fallback = None
                    if requested_page_pool:
                        # If user/agent requested specific pages, never fallback to unrelated pages.
                        fallback = next(
                            (
                                item for item in ranked
                                if int(item.get("page", 0)) in requested_page_pool
                                and _snippet_hits_focus_terms(item, match_terms)
                            ),
                            None,
                        )
                        if fallback is None:
                            fallback = next(
                                (
                                    item for item in ranked
                                    if int(item.get("page", 0)) in requested_page_pool
                                    and _score_snippet(item) > 0
                                ),
                                None,
                            )
                    else:
                        fallback = next((item for item in ranked if _snippet_hits_focus_terms(item, match_terms)), None)
                        if fallback is None and ranked:
                            best_score = _score_snippet(ranked[0])
                            fallback = ranked[0] if best_score > 0 else None
                        if fallback is None and snippets:
                            fallback = next((item for item in snippets if _snippet_hits_focus_terms(item, match_terms)), None)
                        if fallback is None and snippets:
                            fallback = snippets[0]
                    if fallback is not None:
                        selected_ranked = [fallback]

                # Cap evidence volume while preserving multi-range coverage.
                recommendation_item_count = _estimate_recommendation_item_count(final_output)
                recommendation_focus_terms = _extract_recommendation_focus_terms(final_output)
                if (
                    recommendation_item_count >= 2
                    and len(selected_ranked) < recommendation_item_count
                    and (not requested_page_pool or bool(selected_ranked))
                ):
                    def _snippet_body(s: dict) -> str:
                        return f"{str(s.get('quote', '') or '')} {str(s.get('content', '') or '')}".lower()

                    # Prefer snippets that explicitly mention each recommendation item label.
                    covered_terms: set[str] = set()
                    for chosen in selected_ranked:
                        body = _snippet_body(chosen)
                        for term in recommendation_focus_terms:
                            if term.lower() in body:
                                covered_terms.add(term.lower())
                    for term in recommendation_focus_terms:
                        if len(selected_ranked) >= recommendation_item_count:
                            break
                        key = term.lower()
                        if key in covered_terms:
                            continue
                        candidate = None
                        for item in ranked:
                            page_no = int(item.get("page", 0))
                            if page_no <= 0 or page_no in selected_pages:
                                continue
                            if key in _snippet_body(item):
                                candidate = item
                                break
                        if candidate is None:
                            continue
                        page_no = int(candidate.get("page", 0))
                        selected_ranked.append(candidate)
                        selected_pages.add(page_no)
                        covered_terms.add(key)

                    # If still short, top up with highest-ranked remaining snippets.
                    if len(selected_ranked) < recommendation_item_count:
                        for item in ranked:
                            page_no = int(item.get("page", 0))
                            if page_no <= 0 or page_no in selected_pages:
                                continue
                            selected_ranked.append(item)
                            selected_pages.add(page_no)
                            if len(selected_ranked) >= recommendation_item_count:
                                break

                evidence_cap = 3 if recommendation_item_count <= 0 else max(3, min(6, recommendation_item_count))
                if len(selected_ranked) > evidence_cap:
                    selected_ranked = sorted(selected_ranked, key=_score_snippet, reverse=True)[:evidence_cap]

                snippets = sorted(selected_ranked, key=lambda x: x["page"])
                verified_pages = sorted({int(s["page"]) for s in snippets if isinstance(s.get("page"), int) and s["page"] > 0})
                fetched_pages = _compact_pages(verified_pages)
            elif slot.get("structure_candidates"):
                structure_candidates = []
                for candidate in slot.get("structure_candidates", []):
                    if not isinstance(candidate, dict):
                        continue
                    start_page = candidate.get("start_page")
                    end_page = candidate.get("end_page")
                    if not isinstance(start_page, int) or start_page <= 0:
                        continue
                    if not isinstance(end_page, int) or end_page < start_page:
                        end_page = start_page
                    structure_candidates.append(
                        {
                            "start_page": start_page,
                            "end_page": end_page,
                            "page_range": _compact_page_range(start_page, end_page),
                            "content": str(candidate.get("content", "") or ""),
                            "quote": str(candidate.get("quote", "") or ""),
                        }
                    )
                if structure_candidates:
                    focus_terms = _extract_focus_terms(prompt, final_output)
                    query_focus_terms = _extract_query_anchor_terms(prompt)
                    match_terms = query_focus_terms or focus_terms
                    structure_anchor_terms: set[str] = set()
                    ranked = sorted(
                        structure_candidates,
                        key=lambda s: _snippet_relevance_score(
                            quote=s.get("quote", "") or s.get("content", ""),
                            query=prompt,
                            answer=final_output,
                        ),
                        reverse=True,
                    )
                    ranked_query_aligned = [
                        item for item in ranked
                        if _snippet_hits_focus_terms(
                            {"quote": item.get("quote", ""), "content": item.get("content", "")},
                            query_focus_terms or match_terms,
                        )
                    ]
                    candidate_pool = ranked_query_aligned if ranked_query_aligned else ranked
                    multi_source_needed = _answer_implies_multi_source_support(final_output) and len(candidate_pool) >= 2
                    selected_candidates = candidate_pool[:2] if multi_source_needed else candidate_pool[:1]
                    for picked in selected_candidates:
                        picked_score = _snippet_relevance_score(
                            quote=str(picked.get("quote", "") or picked.get("content", "")),
                            query=prompt,
                            answer=final_output,
                        )
                        if picked_score <= 0 and not _snippet_hits_focus_terms(
                            {"quote": picked.get("quote", ""), "content": picked.get("content", "")},
                            query_focus_terms or match_terms,
                        ):
                            continue
                        structure_anchor_terms.update(
                            _extract_anchor_terms_from_structure_text(
                                f"{str(picked.get('quote', '') or '')} {str(picked.get('content', '') or '')}"
                            )
                        )
                    if structure_anchor_terms:
                        match_terms = set(match_terms) | structure_anchor_terms
                    # Force one extra正文核验: structure-only evidence must be validated by get_page_content.
                    verified_snippets: list[dict] = []
                    verified_pages_set: set[int] = set()
                    doc_type_value = str(slot.get("doc_type", "") or "").strip().lower()
                    # For non-PDF docs, structure start/end indexes are often not real page numbers.
                    # Prefer global keyword-hit scan over index-window probing.
                    if doc_type_value != "pdf" and match_terms:
                        keyword_ranked_pages: list[tuple[int, int]] = []
                        try:
                            doc_meta = ((getattr(client, "documents", {}) or {}).get(doc_id, {}) or {})
                            page_entries = doc_meta.get("pages")
                            if isinstance(page_entries, list):
                                for row in page_entries:
                                    if not isinstance(row, dict):
                                        continue
                                    try:
                                        page_int = int(row.get("page"))
                                    except Exception:
                                        continue
                                    if page_int <= 0:
                                        continue
                                    content_text = str(row.get("content", "") or "")
                                    if _is_toc_heavy_content(content_text):
                                        continue
                                    probe_item = {"quote": "", "content": content_text}
                                    hit_count = _snippet_focus_hit_count(probe_item, match_terms)
                                    if hit_count <= 0:
                                        continue
                                    keyword_ranked_pages.append((hit_count, page_int))
                        except Exception:
                            keyword_ranked_pages = []

                        if keyword_ranked_pages:
                            keyword_ranked_pages = sorted(
                                keyword_ranked_pages,
                                key=lambda item: (-int(item[0]), int(item[1])),
                            )
                            keyword_pages = [page for _, page in keyword_ranked_pages[:3]]
                            keyword_expr = _compact_pages(keyword_pages)
                            try:
                                keyword_payload = get_page_content_tool(client, doc_id, keyword_expr)
                            except Exception:
                                keyword_payload = []
                            if isinstance(keyword_payload, list):
                                for row in keyword_payload:
                                    if not isinstance(row, dict):
                                        continue
                                    try:
                                        page_int = int(row.get("page"))
                                    except Exception:
                                        continue
                                    if page_int <= 0:
                                        continue
                                    content_text = str(row.get("content", "") or "")
                                    if _is_toc_heavy_content(content_text):
                                        continue
                                    probe_item = {"quote": "", "content": content_text}
                                    if _snippet_focus_hit_count(probe_item, match_terms) <= 0:
                                        continue
                                    verified_pages_set.add(page_int)
                                    verified_snippets.append(
                                        {
                                            "page": page_int,
                                            "page_range": str(page_int),
                                            "content": content_text[:12000],
                                            "quote": _build_quote_from_content(
                                                content_text,
                                                max_len=600,
                                                query=prompt,
                                                answer=final_output,
                                            ),
                                            "source_tool": "get_page_content",
                                            "unit": _citation_unit_by_doc_type(str(slot.get("doc_type", "") or "").strip().lower()),
                                        }
                                    )
                    for picked in selected_candidates:
                        if verified_snippets:
                            break
                        page_expr = str(picked.get("page_range", "") or "").strip()
                        if not page_expr:
                            page_expr = _compact_page_range(int(picked["start_page"]), int(picked["end_page"]))
                        # Probe nearby page/chunk windows to tolerate minor index-range drift.
                        try:
                            base_pages = [int(p) for p in parse_pages_expression(page_expr) if int(p) > 0]
                        except Exception:
                            base_pages = []
                        if not base_pages:
                            base_pages = [int(picked["start_page"])]
                        page_count_cap = int(((getattr(client, "documents", {}) or {}).get(doc_id, {}) or {}).get("page_count", 0) or 0)
                        probe_pages: set[int] = set()
                        for base in base_pages:
                            for shift in (-2, -1, 0, 1, 2):
                                candidate = int(base) + shift
                                if candidate <= 0:
                                    continue
                                if page_count_cap > 0 and candidate > page_count_cap:
                                    continue
                                probe_pages.add(candidate)
                        probe_expr = _compact_pages(sorted(probe_pages)) or page_expr
                        try:
                            page_payload = get_page_content_tool(client, doc_id, probe_expr)
                        except Exception:
                            page_payload = []
                        if not isinstance(page_payload, list):
                            page_payload = []
                        for row in page_payload:
                            if not isinstance(row, dict):
                                continue
                            try:
                                page_int = int(row.get("page"))
                            except Exception:
                                continue
                            if page_int <= 0:
                                continue
                            content_text = str(row.get("content", "") or "")
                            if _is_toc_heavy_content(content_text):
                                continue
                            if structure_anchor_terms:
                                probe_item = {"quote": "", "content": content_text}
                                if not _snippet_hits_focus_terms(probe_item, structure_anchor_terms):
                                    continue
                            verified_pages_set.add(page_int)
                            verified_snippets.append(
                                {
                                    "page": page_int,
                                    "page_range": str(page_int),
                                    "content": content_text[:12000],
                                    "quote": _build_quote_from_content(
                                        content_text,
                                        max_len=600,
                                        query=prompt,
                                        answer=final_output,
                                    ),
                                    "source_tool": "get_page_content",
                                    "unit": _citation_unit_by_doc_type(str(slot.get("doc_type", "") or "").strip().lower()),
                                }
                            )

                    if verified_snippets:
                        dedup_by_page: dict[int, dict] = {}
                        for item in verified_snippets:
                            page_int = int(item.get("page", 0) or 0)
                            if page_int <= 0:
                                continue
                            # Prefer snippet with stronger lexical match.
                            existing = dedup_by_page.get(page_int)
                            if existing is None:
                                dedup_by_page[page_int] = item
                                continue
                            new_score = _snippet_relevance_score(
                                quote=str(item.get("quote", "") or item.get("content", "")),
                                query=prompt,
                                answer=final_output,
                            )
                            old_score = _snippet_relevance_score(
                                quote=str(existing.get("quote", "") or existing.get("content", "")),
                                query=prompt,
                                answer=final_output,
                            )
                            if new_score > old_score:
                                dedup_by_page[page_int] = item
                        ranked_page_snippets = list(dedup_by_page.values())
                        focus_ranked_page_snippets = [
                            item for item in ranked_page_snippets
                            if _snippet_hits_focus_terms(item, match_terms)
                        ]
                        if focus_ranked_page_snippets:
                            ranked_page_snippets = sorted(
                                focus_ranked_page_snippets,
                                key=lambda s: (
                                    _snippet_relevance_score(
                                        quote=str(s.get("quote", "") or s.get("content", "")),
                                        query=prompt,
                                        answer=final_output,
                                    )
                                    + _snippet_focus_hit_count(s, match_terms) * 10
                                ),
                                reverse=True,
                            )[:3]
                            snippets = sorted(ranked_page_snippets, key=lambda x: int(x.get("page", 0) or 0))
                            verified_pages = sorted({
                                int(s.get("page", 0) or 0)
                                for s in snippets
                                if int(s.get("page", 0) or 0) > 0
                            })
                            fetched_pages = _compact_pages(verified_pages)
                        else:
                            verified_snippets = []

                    if not verified_snippets:
                        # Last-resort正文核验：scan loaded pages by focus-term hits.
                        keyword_pages: list[int] = []
                        try:
                            doc_meta = ((getattr(client, "documents", {}) or {}).get(doc_id, {}) or {})
                            page_entries = doc_meta.get("pages")
                            if isinstance(page_entries, list) and match_terms:
                                for row in page_entries:
                                    if not isinstance(row, dict):
                                        continue
                                    try:
                                        page_int = int(row.get("page"))
                                    except Exception:
                                        continue
                                    if page_int <= 0:
                                        continue
                                    content_text = str(row.get("content", "") or "")
                                    test_item = {"quote": "", "content": content_text}
                                    if _is_toc_heavy_content(content_text):
                                        continue
                                    if structure_anchor_terms and not _snippet_hits_focus_terms(test_item, structure_anchor_terms):
                                        continue
                                    if _snippet_hits_focus_terms(test_item, match_terms):
                                        keyword_pages.append(page_int)
                            keyword_pages = sorted(dict.fromkeys(keyword_pages))[:3]
                        except Exception:
                            keyword_pages = []

                        if keyword_pages:
                            keyword_expr = _compact_pages(keyword_pages)
                            try:
                                keyword_payload = get_page_content_tool(client, doc_id, keyword_expr)
                            except Exception:
                                keyword_payload = []
                            if isinstance(keyword_payload, list):
                                for row in keyword_payload:
                                    if not isinstance(row, dict):
                                        continue
                                    try:
                                        page_int = int(row.get("page"))
                                    except Exception:
                                        continue
                                    if page_int <= 0:
                                        continue
                                    content_text = str(row.get("content", "") or "")
                                    if _is_toc_heavy_content(content_text):
                                        continue
                                    if structure_anchor_terms:
                                        probe_item = {"quote": "", "content": content_text}
                                        if not _snippet_hits_focus_terms(probe_item, structure_anchor_terms):
                                            continue
                                    verified_pages_set.add(page_int)
                                    verified_snippets.append(
                                        {
                                            "page": page_int,
                                            "page_range": str(page_int),
                                            "content": content_text[:12000],
                                            "quote": _build_quote_from_content(
                                                content_text,
                                                max_len=600,
                                                query=prompt,
                                                answer=final_output,
                                            ),
                                            "source_tool": "get_page_content",
                                            "unit": _citation_unit_by_doc_type(str(slot.get("doc_type", "") or "").strip().lower()),
                                        }
                                    )

                        if verified_snippets:
                            dedup_by_page: dict[int, dict] = {}
                            for item in verified_snippets:
                                page_int = int(item.get("page", 0) or 0)
                                if page_int <= 0:
                                    continue
                                existing = dedup_by_page.get(page_int)
                                if existing is None:
                                    dedup_by_page[page_int] = item
                                    continue
                                new_score = _snippet_relevance_score(
                                    quote=str(item.get("quote", "") or item.get("content", "")),
                                    query=prompt,
                                    answer=final_output,
                                )
                                old_score = _snippet_relevance_score(
                                    quote=str(existing.get("quote", "") or existing.get("content", "")),
                                    query=prompt,
                                    answer=final_output,
                                )
                                if new_score > old_score:
                                    dedup_by_page[page_int] = item
                            snippets = sorted(dedup_by_page.values(), key=lambda x: int(x.get("page", 0) or 0))
                            verified_pages = sorted({
                                int(s.get("page", 0) or 0)
                                for s in snippets
                                if int(s.get("page", 0) or 0) > 0
                            })
                            fetched_pages = _compact_pages(verified_pages)

                    if not verified_snippets:
                        # Fallback to structure snippet when page fetching fails.
                        snippets = []
                        for picked in selected_candidates:
                            snippets.append(
                                {
                                    "page": picked["start_page"],
                                    "page_range": picked["page_range"],
                                    "content": picked["content"],
                                    "quote": picked["quote"],
                                    "source_tool": "get_document_structure",
                                    "unit": _citation_unit_by_doc_type(str(slot.get("doc_type", "") or "").strip().lower()),
                                }
                            )
                            if picked["end_page"] >= picked["start_page"]:
                                verified_pages_set.update(range(picked["start_page"], picked["end_page"] + 1))
                            else:
                                verified_pages_set.add(picked["start_page"])
                        verified_pages = sorted(verified_pages_set)
                        fetched_pages = _compact_pages(verified_pages)

            # Prefer正文证据: if page-content snippets exist, drop structure-only snippets.
            query_focus_terms = _extract_query_anchor_terms(prompt)
            answer_focus_terms = _extract_focus_terms("", final_output)
            strict_query_phrases = _extract_strict_query_anchor_phrases(prompt)
            page_content_snippets = [
                s for s in (snippets or [])
                if str((s or {}).get("source_tool", "") or "").strip() == "get_page_content"
            ]
            if strict_query_phrases:
                strict_phrase_hits = [
                    s for s in page_content_snippets
                    if _snippet_hits_focus_terms(s, strict_query_phrases)
                ]
                if strict_phrase_hits:
                    page_content_snippets = strict_phrase_hits
            strict_query_focus_terms = {t for t in query_focus_terms if len(str(t or "")) >= 3}
            if strict_query_focus_terms:
                page_content_snippets = [
                    s for s in page_content_snippets
                    if _snippet_hits_focus_terms(s, strict_query_focus_terms)
                ]
            if query_focus_terms:
                page_content_snippets = [
                    s for s in page_content_snippets
                    if _snippet_hits_focus_terms(s, query_focus_terms)
                ]
            strict_answer_focus_terms = {
                t for t in answer_focus_terms
                if len(str(t or "")) >= 2 and t not in query_focus_terms
            }
            if strict_answer_focus_terms:
                page_content_snippets = [
                    s for s in page_content_snippets
                    if _snippet_hits_focus_terms(s, strict_answer_focus_terms)
                ]
            if page_content_snippets:
                snippets = sorted(page_content_snippets, key=lambda x: int(x.get("page", 0) or 0))
                verified_pages = sorted({
                    int(s.get("page", 0) or 0)
                    for s in snippets
                    if int(s.get("page", 0) or 0) > 0
                })
                fetched_pages = _compact_pages(verified_pages)
            if strict_query_phrases and snippets and not any(
                _snippet_hits_focus_terms(s, strict_query_phrases) for s in snippets
            ):
                snippets = []
                verified_pages = []
                fetched_pages = ""
            if query_focus_terms and snippets and not any(
                _snippet_hits_focus_terms(s, query_focus_terms) for s in snippets
            ):
                snippets = []
                verified_pages = []
                fetched_pages = ""
            evidence.append(
                {
                    "doc_id": doc_id,
                    "doc_name": str(slot.get("doc_name", "") or doc_name_by_id.get(doc_id, doc_id)),
                    "doc_type": str(slot.get("doc_type", "") or "").strip().lower(),
                    "unit": _citation_unit_by_doc_type(str(slot.get("doc_type", "") or "").strip().lower()),
                    "selected_page_ranges": selected_ranges,
                    "fetched_pages": fetched_pages,
                    "verified_pages": verified_pages,
                    "snippets": snippets,
                }
            )

        normalized_answer, citations = _normalize_agent_answer_with_citations(final_output, evidence)
        if should_print_final_answer and normalized_answer:
            _print_step("回答", normalized_answer)
        return {
            "answer": normalized_answer or final_output,
            "selected_documents": selected_documents,
            "evidence": evidence,
            "citations": citations,
        }

    try:
        result = await _run()
        if return_details:
            if isinstance(result, dict):
                return result
            return {
                "answer": "" if result is None else str(result),
                "selected_documents": [],
                "evidence": [],
                "citations": [],
            }
        if isinstance(result, dict):
            return str(result.get("answer", "") or "")
        return "" if result is None else str(result)
    except Exception:
        logger.exception("Agent query failed")
        if return_details:
            return {
                "answer": DEFAULT_NO_INFO_ANSWER,
                "selected_documents": [],
                "evidence": [],
                "citations": [],
            }
        return DEFAULT_NO_INFO_ANSWER


