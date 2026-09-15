from __future__ import annotations

from typing import Any

import httpx

from app.api.contracts import ChatCompletionRequest
from app.chat.models import RetrievalResult, RetrievalStatus
from app.platform.errors import ApiError

MIN_RETURN_TOKENS = 4096
DEFAULT_RETURN_TOKENS = 8192
MEDIUM_RETURN_TOKENS = 16384
DEFAULT_MAX_RETURN_TOKENS = 180_000
_BROAD_QUERY_MARKERS = ("总结", "概括", "对比", "比较", "解释", "分析", "全文", "详细")
MIN_TOP_K = 4
MAX_TOP_K = 20


def _query_text(query: str | list[str]) -> str:
    return query if isinstance(query, str) else "；".join(query)


def calculate_top_k(
    query: str | list[str],
    *,
    document_count: int,
    requested_max: int | None = None,
) -> int:
    text = _query_text(query).strip()
    top_k = MIN_TOP_K
    if len(text) >= 80 or document_count >= 4:
        top_k = 8
    if len(text) >= 200 or document_count >= 10:
        top_k = 12
    if len(text) >= 500 or document_count >= 20 or any(marker in text for marker in _BROAD_QUERY_MARKERS):
        top_k = MAX_TOP_K
    caller_cap = MAX_TOP_K if requested_max is None else int(requested_max)
    caller_cap = max(MIN_TOP_K, min(caller_cap, MAX_TOP_K))
    return max(MIN_TOP_K, min(top_k, caller_cap))


def calculate_max_return_tokens(
    query: str | list[str],
    *,
    document_count: int,
    requested_max: int | None = None,
    configured_max: int = DEFAULT_MAX_RETURN_TOKENS,
) -> int:
    if requested_max is not None:
        return int(requested_max)
    text = _query_text(query).strip()
    budget = DEFAULT_RETURN_TOKENS
    if len(text) >= 200 or document_count >= 10:
        budget = MEDIUM_RETURN_TOKENS
    if len(text) >= 800 or document_count >= 40 or any(marker in text for marker in _BROAD_QUERY_MARKERS):
        budget = configured_max
    return max(MIN_RETURN_TOKENS, min(budget, configured_max))


class RetrievalClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 35.0,
        max_return_tokens: int = DEFAULT_MAX_RETURN_TOKENS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._max_return_tokens = max_return_tokens

    async def get_document_names(
        self,
        *,
        user_id: str,
        kb_id: str,
        requested_doc_ids: list[str],
        permanent_scope_ids: list[str],
        temporary_scope_ids: list[str],
        session_id: str | None,
    ) -> list[str]:
        permanent_scope = set(permanent_scope_ids)
        temporary_scope = set(temporary_scope_ids) - permanent_scope
        doc_ids = [doc_id for doc_id in requested_doc_ids if doc_id in permanent_scope]
        temp_doc_ids = [
            doc_id
            for doc_id in requested_doc_ids
            if doc_id not in permanent_scope and doc_id in temporary_scope
        ]
        try:
            response = await self._http.post(
                f"{self._base_url}/rag/v1/documents/meta",
                json={
                    "user_id": user_id,
                    "kb_id": kb_id,
                    "doc_ids": doc_ids,
                    "temp_doc_ids": temp_doc_ids,
                    "session_id": session_id,
                    "include_missing": True,
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as exc:
            raise ApiError(
                503,
                "incremental_document_metadata_unavailable",
                "incremental document metadata is temporarily unavailable",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise ApiError(
                503,
                "incremental_document_metadata_unavailable",
                "incremental document metadata is temporarily unavailable",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        try:
            payload = response.json()
            documents = payload["documents"]
            missing = payload.get("missing_doc_ids", [])
            names_by_id = {str(document["doc_id"]): str(document["doc_name"]) for document in documents}
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError(
                502,
                "invalid_incremental_document_metadata",
                "incremental document metadata response is invalid",
                retryable=True,
            ) from exc
        unresolved = [doc_id for doc_id in requested_doc_ids if doc_id not in names_by_id]
        if missing or unresolved:
            raise ApiError(
                400,
                "incremental_document_metadata_not_found",
                "incremental document metadata is incomplete",
                details={"missing_doc_ids": list(dict.fromkeys([*missing, *unresolved]))},
            )
        return [names_by_id[doc_id] for doc_id in requested_doc_ids]

    async def retrieve(
        self,
        *,
        request: ChatCompletionRequest,
        query: str | list[str],
        include_debug: bool,
    ) -> RetrievalResult:
        document_count = len(request.rag.doc_ids) + len(request.rag.temp_doc_ids)
        payload = {
            "user_id": request.rag.user_id,
            "kb_id": request.rag.kb_id,
            "session_id": request.rag.session_id,
            "query": query,
            "doc_ids": request.rag.doc_ids,
            "temp_doc_ids": request.rag.temp_doc_ids,
            "top_k": calculate_top_k(
                query,
                document_count=document_count,
                requested_max=request.rag.top_k,
            ),
            "max_return_tokens": calculate_max_return_tokens(
                query,
                document_count=document_count,
                requested_max=request.rag.max_return_tokens,
                configured_max=self._max_return_tokens,
            ),
            "search_mode": "hybrid",
            "options": {
                "include_document_meta": True,
                "include_debug": include_debug,
                "ensure_document_coverage": False,
            },
        }
        try:
            response = await self._http.post(
                f"{self._base_url}/rag/v1/retrieve",
                json=payload,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as exc:
            return RetrievalResult(
                status=RetrievalStatus.ERROR,
                error={
                    "code": "retrieval_unavailable",
                    "type": type(exc).__name__,
                    "retryable": True,
                },
            )

        request_id = response.headers.get("X-Request-Id")
        if response.status_code == 404:
            error_code = "retrieval_not_found"
            try:
                body = response.json()
                response_error = body.get("error", {}) if isinstance(body, dict) else {}
                if isinstance(response_error, dict) and response_error.get("code"):
                    error_code = str(response_error["code"])
            except ValueError:
                pass
            return RetrievalResult(
                status=RetrievalStatus.NO_RESULT,
                request_id=request_id,
                error={
                    "code": error_code,
                    "status_code": 404,
                    "retryable": False,
                },
            )
        if response.status_code >= 400:
            return RetrievalResult(
                status=RetrievalStatus.ERROR,
                request_id=request_id,
                error={
                    "code": "retrieval_http_error",
                    "status_code": response.status_code,
                    "retryable": response.status_code in {429, 500, 502, 503, 504},
                },
            )
        try:
            data: dict[str, Any] = response.json()
            chunks = data["chunks"]
            usage = data["usage"]
            if not isinstance(chunks, list) or not isinstance(usage, dict):
                raise TypeError("invalid retrieval response")
        except (ValueError, KeyError, TypeError):
            return RetrievalResult(
                status=RetrievalStatus.ERROR,
                request_id=request_id,
                error={"code": "invalid_retrieval_response", "retryable": True},
            )
        status = RetrievalStatus.SUCCESS if chunks else RetrievalStatus.NO_RESULT
        return RetrievalResult(
            status=status,
            chunks=chunks,
            usage=usage,
            debug=data.get("debug"),
            request_id=request_id,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()
