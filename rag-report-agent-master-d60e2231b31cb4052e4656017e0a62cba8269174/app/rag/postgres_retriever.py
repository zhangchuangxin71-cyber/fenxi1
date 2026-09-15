import asyncio
import re
from dataclasses import dataclass

import psycopg

from app.config import settings
from app.rag.types import ProvidedChunkInput


@dataclass(frozen=True)
class CandidateChunk:
    chunk_id: str
    document_id: str
    document_name: str
    path: str
    content: str
    metadata_text: str = ""


_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize_query(query: str) -> list[str]:
    text = (query or "").strip().lower()
    tokens: set[str] = set(_WORD_RE.findall(text))
    for seq in _CJK_RE.findall(text):
        if len(seq) <= 8:
            tokens.add(seq)
        for size in (2, 3, 4):
            for idx in range(0, max(0, len(seq) - size + 1)):
                tokens.add(seq[idx : idx + size])
    stopwords = {"是谁", "什么", "怎么", "如何", "以及", "一个", "这个", "那个", "请问"}
    return sorted(token for token in tokens if token and token not in stopwords)


def _score_candidate(query: str, terms: list[str], chunk: CandidateChunk) -> float:
    content = chunk.content.lower()
    metadata = chunk.metadata_text.lower()
    query_text = (query or "").strip().lower()
    score = 0.0
    if query_text and query_text in content:
        score += 40.0
    if query_text and query_text in metadata:
        score += 20.0
    for term in terms:
        if term in content:
            score += 10.0 + min(content.count(term), 5)
        if term in metadata:
            score += 5.0 + min(metadata.count(term), 3)
    if chunk.path.startswith("node:"):
        score += 1.0
    if "谁" in (query or ""):
        profile_terms = ["负责人", "研究员", "博士", "工作经历", "教育背景", "研究方向", "任职"]
        score += sum(4.0 for term in profile_terms if term in chunk.content)
    if len(chunk.content) > 3000:
        score -= 0.5
    return score


def rank_candidates(
    query: str,
    chunks: list[CandidateChunk],
    top_k: int,
    *,
    ensure_document_coverage: bool = False,
) -> list[CandidateChunk]:
    terms = tokenize_query(query)
    scored = [(_score_candidate(query, terms, chunk), idx, chunk) for idx, chunk in enumerate(chunks)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    positive = [chunk for score, _, chunk in scored if score > 0]
    ranked = positive or [chunk for _, _, chunk in scored]
    limit = max(1, top_k)
    if not ensure_document_coverage:
        return ranked[:limit]

    covered_docs: set[str] = set()
    selected: list[CandidateChunk] = []
    selected_ids: set[str] = set()
    for chunk in ranked:
        if chunk.document_id in covered_docs:
            continue
        selected.append(chunk)
        selected_ids.add(chunk.chunk_id)
        covered_docs.add(chunk.document_id)
        if len(selected) >= limit:
            return selected

    for chunk in ranked:
        if len(selected) >= limit:
            break
        if chunk.chunk_id in selected_ids:
            continue
        selected.append(chunk)
        selected_ids.add(chunk.chunk_id)
    return selected


def _chunk_content(*, title: str | None, summary: str | None, text: str | None) -> str:
    parts = []
    if title:
        parts.append(str(title).strip())
    body = (summary or text or "").strip()
    if body:
        parts.append(body)
    return "\n".join(part for part in parts if part)


def _fetch_candidate_chunks_sync(
    *,
    kb_id: str,
    user_id: str,
    query: str,
    session_id: str,
    doc_ids: list[str] | None,
    temp_doc_ids: list[str] | None,
    max_candidates: int,
) -> list[CandidateChunk]:
    allowed_doc_ids = [str(item) for item in [*(doc_ids or []), *(temp_doc_ids or [])] if item]
    where = ["d.status = 'ready'", "b.kb_id = %s"]
    params: list[object] = [kb_id]
    if allowed_doc_ids:
        where.append("d.doc_id::text = ANY(%s)")
        params.append(allowed_doc_ids)
    where_sql = " AND ".join(where)

    node_sql = f"""
        SELECT
          d.doc_id::text,
          d.doc_name,
          COALESCE(d.doc_description, ''),
          n.node_id,
          n.title,
          n.summary,
          n.text,
          n.start_index,
          n.end_index
        FROM doc_nodes n
        JOIN documents d ON d.doc_id = n.doc_id
        JOIN document_bindings b ON b.doc_id = d.doc_id
        WHERE {where_sql}
        ORDER BY d.doc_name, n.id
        LIMIT %s
    """
    page_sql = f"""
        SELECT
          d.doc_id::text,
          d.doc_name,
          COALESCE(d.doc_description, ''),
          p.page_number,
          p.content
        FROM doc_pages p
        JOIN documents d ON d.doc_id = p.doc_id
        JOIN document_bindings b ON b.doc_id = d.doc_id
        WHERE {where_sql}
        ORDER BY d.doc_name, p.page_number
        LIMIT %s
    """

    chunks: list[CandidateChunk] = []
    seen: set[str] = set()
    with psycopg.connect(settings.pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(node_sql, tuple([*params, max_candidates]))
            for row in cur.fetchall():
                (
                    doc_id,
                    doc_name,
                    doc_description,
                    node_id,
                    title,
                    summary,
                    text,
                    start_index,
                    end_index,
                ) = row
                content = _chunk_content(title=title, summary=summary, text=text)
                if not content:
                    continue
                chunk_id = f"{doc_id}:node:{node_id}"
                seen.add(chunk_id)
                path_bits = [f"node:{node_id}"]
                if start_index is not None:
                    page_span = str(start_index)
                    if end_index is not None and end_index != start_index:
                        page_span = f"{start_index}-{end_index}"
                    path_bits.append(f"page:{page_span}")
                chunks.append(
                    CandidateChunk(
                        chunk_id=chunk_id,
                        document_id=doc_id,
                        document_name=doc_name or doc_id,
                        path=" ".join(path_bits),
                        content=content,
                        metadata_text=f"{doc_name or ''}\n{doc_description or ''}",
                    )
                )

            cur.execute(page_sql, tuple([*params, max_candidates]))
            for doc_id, doc_name, doc_description, page_number, content in cur.fetchall():
                if not content:
                    continue
                chunk_id = f"{doc_id}:page:{page_number}"
                if chunk_id in seen:
                    continue
                chunks.append(
                    CandidateChunk(
                        chunk_id=chunk_id,
                        document_id=doc_id,
                        document_name=doc_name or doc_id,
                        path=f"page:{page_number}",
                        content=str(content).strip(),
                        metadata_text=f"{doc_name or ''}\n{doc_description or ''}",
                    )
                )
    return chunks


async def fetch_candidate_chunks(
    *,
    kb_id: str,
    user_id: str,
    query: str,
    session_id: str,
    doc_ids: list[str] | None,
    temp_doc_ids: list[str] | None,
    max_candidates: int,
) -> list[CandidateChunk]:
    return await asyncio.to_thread(
        _fetch_candidate_chunks_sync,
        kb_id=kb_id,
        user_id=user_id,
        query=query,
        session_id=session_id,
        doc_ids=doc_ids,
        temp_doc_ids=temp_doc_ids,
        max_candidates=max_candidates,
    )


async def retrieve(
    *,
    kb_id: str,
    user_id: str,
    query: str,
    session_id: str,
    doc_ids: list[str] | None = None,
    temp_doc_ids: list[str] | None = None,
    top_k: int = 5,
    search_mode: str = "hybrid",
    ensure_document_coverage: bool = False,
) -> list[ProvidedChunkInput]:
    candidates = await fetch_candidate_chunks(
        kb_id=kb_id,
        user_id=user_id,
        query=query,
        session_id=session_id,
        doc_ids=doc_ids,
        temp_doc_ids=temp_doc_ids,
        max_candidates=settings.rag_max_candidates,
    )
    ranked = rank_candidates(
        query,
        candidates,
        top_k=top_k,
        ensure_document_coverage=ensure_document_coverage,
    )
    return [
        ProvidedChunkInput(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_name=chunk.document_name,
            path=chunk.path,
            content=chunk.content,
        )
        for chunk in ranked
    ]
