from __future__ import annotations

import logging

import httpx

from app.rag.types import ProvidedChunkInput
from app.utils.errors import FrontendServiceError


logger = logging.getLogger(__name__)

MIN_RETURN_TOKENS = 4096
DEFAULT_RETURN_TOKENS = 8192
MEDIUM_RETURN_TOKENS = 16384
MAX_RETURN_TOKENS = 32768
_BROAD_QUERY_MARKERS = ("总结", "概括", "对比", "比较", "解释", "分析", "全文", "详细")


def calculate_max_return_tokens(query: str, *, document_count: int) -> int:
    """Use the same bounded retrieval budget policy as rag-knowledge-chat."""
    text = str(query or "").strip()
    budget = DEFAULT_RETURN_TOKENS
    if len(text) >= 200 or document_count >= 10:
        budget = MEDIUM_RETURN_TOKENS
    if len(text) >= 800 or document_count >= 40 or any(
        marker in text for marker in _BROAD_QUERY_MARKERS
    ):
        budget = MAX_RETURN_TOKENS
    return max(MIN_RETURN_TOKENS, min(budget, MAX_RETURN_TOKENS))


def _retrieve_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/rag/v1/retrieve"


def _remote_error_from_response(response: httpx.Response) -> FrontendServiceError:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    upstream_code = str(error.get("code") or response.status_code)
    upstream_message = str(error.get("message") or response.text or "")[:500]
    upstream_request_id = str(error.get("request_id") or "")
    internal = (
        f"rag retrieval service returned status={response.status_code} "
        f"code={upstream_code} request_id={upstream_request_id} message={upstream_message}"
    )
    if response.status_code == 429:
        return FrontendServiceError(
            status_code=429,
            error_type="rag_retrieval_rate_limited",
            public_message="知识库检索服务繁忙，请稍后再试。",
            internal_message=internal,
        )
    return FrontendServiceError(
        status_code=503,
        error_type="rag_retrieval_unavailable",
        public_message="知识库检索服务暂时不可用，请稍后再试。",
        internal_message=internal,
    )


async def retrieve(
    *,
    base_url: str,
    kb_id: str,
    user_id: str,
    query: str,
    session_id: str,
    doc_ids: list[str] | None = None,
    temp_doc_ids: list[str] | None = None,
    top_k: int = 5,
    search_mode: str = "hybrid",
    ensure_document_coverage: bool = False,
    timeout_seconds: int | float = 8,
) -> list[ProvidedChunkInput]:
    """Retrieve chunks from the standalone RAG retrieval service."""
    url = _retrieve_url(base_url)
    document_count = len(doc_ids or []) + len(temp_doc_ids or [])
    payload = {
        "user_id": user_id,
        "kb_id": kb_id,
        "query": query,
        "session_id": session_id,
        "doc_ids": doc_ids or [],
        "temp_doc_ids": temp_doc_ids or [],
        "top_k": top_k,
        "max_return_tokens": calculate_max_return_tokens(
            query,
            document_count=document_count,
        ),
        "search_mode": search_mode,
        "options": {
            "include_document_meta": False,
            "include_debug": False,
            "ensure_document_coverage": ensure_document_coverage,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
            response = await client.post(url, json=payload, headers={})
    except httpx.TimeoutException as exc:
        raise FrontendServiceError(
            status_code=503,
            error_type="rag_retrieval_unavailable",
            public_message="知识库检索服务响应超时，请稍后再试。",
            internal_message=f"rag retrieval service timeout: {exc}",
        ) from exc
    except httpx.RequestError as exc:
        raise FrontendServiceError(
            status_code=503,
            error_type="rag_retrieval_unavailable",
            public_message="知识库检索服务暂时不可用，请稍后再试。",
            internal_message=f"rag retrieval service request failed: {exc}",
        ) from exc
    if response.status_code >= 400:
        raise _remote_error_from_response(response)
    body = response.json()
    chunks = body.get("chunks") or []
    return [
        ProvidedChunkInput(
            chunk_id=str(item.get("chunk_id") or ""),
            document_id=str(item.get("document_id") or ""),
            document_name=str(
                item.get("document_name")
                or ("请求范围" if not item.get("document_id") else "未命名文档")
            ),
            path=str(item.get("path") or ""),
            content=str(item.get("content") or ""),
        )
        for item in chunks
        if item.get("chunk_id") and item.get("content")
    ]
