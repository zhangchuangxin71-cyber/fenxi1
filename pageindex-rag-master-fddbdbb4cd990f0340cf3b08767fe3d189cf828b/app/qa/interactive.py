# 模块说明：交互式问答会话循环。
import logging
import re

from core.llm_ops import DEFAULT_NO_INFO_ANSWER, GENERAL_KNOWLEDGE_SOURCE_LABEL
from utils.runtime import get_client_runtime

logger = logging.getLogger(__name__)


def _estimate_recommendation_item_count(text: str) -> int:
    """估算回答中的条目数量（用于控制证据片段展示上限）。"""
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


def _print_evidence_details(result: dict):
    """在终端输出参考依据与证据片段。"""
    citations = result.get("citations", []) if isinstance(result, dict) else []
    evidence = result.get("evidence", []) if isinstance(result, dict) else []
    answer_text = str(result.get("answer", "") or "").strip() if isinstance(result, dict) else ""
    answer_has_citation_block = "参考依据：" in answer_text
    recommendation_item_count = _estimate_recommendation_item_count(answer_text)
    snippet_show_limit = 3 if recommendation_item_count <= 0 else max(2, min(6, recommendation_item_count))

    if citations and not answer_has_citation_block:
        print("\n参考依据：")
        for item in citations:
            doc_name = str(item.get("doc_name", "") or "").strip()
            pages = str(item.get("pages", "") or "").strip()
            unit = str(item.get("unit", "") or "页").strip() or "页"
            if doc_name and pages:
                print(f"- 《{doc_name}》 第{pages}{unit}")

    printed_snippet_header = False
    for item in evidence:
        if not isinstance(item, dict):
            continue
        doc_name = str(item.get("doc_name", "") or "").strip()
        snippets = item.get("snippets", [])
        if not isinstance(snippets, list) or not snippets:
            continue
        if not printed_snippet_header:
            print("\n证据片段：")
            printed_snippet_header = True
        shown = 0
        for snippet in snippets:
            if not isinstance(snippet, dict):
                continue
            page = snippet.get("page")
            page_range = str(snippet.get("page_range", "") or "").strip()
            content = str(snippet.get("content", "") or "").strip()
            quote = str(snippet.get("quote", "") or "").strip()
            source_tool = str(snippet.get("source_tool", "") or "").strip()
            unit = str(snippet.get("unit", "") or item.get("unit", "") or "页").strip() or "页"
            if page is None or not content:
                continue
            snippet_text = quote or (content[:800] + ("..." if len(content) > 800 else ""))
            page_label = page_range if page_range else str(page)
            if source_tool:
                print(f"- 《{doc_name}》 第{page_label}{unit}（来源: {source_tool}）：{snippet_text}")
            else:
                print(f"- 《{doc_name}》 第{page_label}{unit}：{snippet_text}")
            shown += 1
            if shown >= snippet_show_limit:
                break


async def interactive_chat(
    client,
    *,
    execute_qa_fn,
    verbose: bool,
    qa_mode: str,
    top_k_docs: int,
    allowed_doc_ids: set[str] | None = None,
    bm25_prefilter=None,
    bm25_prefilter_top_k: int = 8,
    session_id: str | None = None,
):
    """启动交互式问答循环。

    输入 `exit` / `quit` 结束会话；每轮调用统一 QA 执行器，
    并在终端渲染答案、引用和证据片段。
    """
    logger.info("Interactive mode started. Type 'exit' to quit.")
    while True:
        question = input("\nDocument question> ").strip()
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            break
        try:
            result = await execute_qa_fn(
                client,
                question,
                qa_mode=qa_mode,
                verbose=verbose,
                stream_output=True,
                top_k_docs=top_k_docs,
                allowed_doc_ids=allowed_doc_ids,
                bm25_prefilter=bm25_prefilter,
                bm25_prefilter_top_k=bm25_prefilter_top_k,
                session_id=session_id,
            )
            final_answer = str(result.get("answer", "") or "").strip()
            if final_answer.startswith(GENERAL_KNOWLEDGE_SOURCE_LABEL):
                print(f"\n最终回答：{final_answer}")
            _print_evidence_details(result)
            logger.debug("answer=%s", result["answer"])
        except Exception:
            runtime = get_client_runtime(client)
            runtime.stats["qa_failures"] = int(runtime.stats.get("qa_failures", 0)) + 1
            logger.exception("Interactive query failed")
            logger.error(DEFAULT_NO_INFO_ANSWER)

