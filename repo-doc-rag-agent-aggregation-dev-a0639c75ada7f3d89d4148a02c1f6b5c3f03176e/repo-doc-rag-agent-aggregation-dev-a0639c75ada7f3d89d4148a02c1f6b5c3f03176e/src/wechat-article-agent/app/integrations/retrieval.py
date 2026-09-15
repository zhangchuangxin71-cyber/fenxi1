from __future__ import annotations

import asyncio
import random
from time import monotonic
from typing import Any

import httpx

from app.config import AppSettings
from app.core.errors import AppError
from app.core.ids import prefixed_id
from app.core.resilience import CircuitBreaker
from app.debug.trace import current, record


class RetrievalClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.retrieval_max_concurrency)
        timeout = httpx.Timeout(
            connect=settings.retrieval_connect_timeout_seconds,
            read=settings.retrieval_timeout_seconds,
            write=settings.retrieval_timeout_seconds,
            pool=settings.retrieval_connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            base_url=str(settings.retrieval_base_url).rstrip("/"), timeout=timeout
        )
        self._breaker = CircuitBreaker(
            enabled=settings.retrieval_circuit_breaker_enabled,
            threshold=settings.retrieval_circuit_failure_threshold,
            recovery_seconds=settings.retrieval_circuit_recovery_seconds,
            error_code="RETRIEVAL_CIRCUIT_OPEN",
            provider_name="Retrieval service",
        )

    async def meta(
        self,
        *,
        user_id: str,
        kb_id: str,
        session_id: str,
        doc_ids: list[str],
        temp_doc_ids: list[str],
    ) -> dict[str, Any]:
        result = await self._request(
            "POST",
            "/rag/v1/documents/meta",
            json={
                "user_id": user_id,
                "kb_id": kb_id,
                "session_id": session_id,
                "doc_ids": doc_ids,
                "temp_doc_ids": temp_doc_ids,
                "include_missing": True,
            },
        )
        missing = list(result.get("missing_doc_ids") or [])
        if missing:
            raise AppError(
                404,
                "DOCUMENT_NOT_FOUND",
                "One or more requested documents are missing or unavailable.",
                details={"missing_doc_ids": missing},
                stage="docs_research",
            )
        return result

    async def route(
        self,
        *,
        user_id: str,
        kb_id: str,
        session_id: str,
        doc_ids: list[str],
        temp_doc_ids: list[str],
        topic: str,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        try:
            result = await self._request(
                "POST",
                "/rag/v1/documents/route",
                json={
                    "user_id": user_id,
                    "kb_id": kb_id,
                    "session_id": session_id,
                    "doc_ids": doc_ids,
                    "temp_doc_ids": temp_doc_ids,
                    "criteria": [
                        {
                            "queries": [topic],
                            "target_docs_description": f"与文章主题直接相关的参考文档：{topic}",
                            "target_docs_keywords": [],
                        }
                    ],
                    "keyword_prefilter": False,
                },
            )
        except AppError as exc:
            if exc.retryable:
                return [*doc_ids, *temp_doc_ids], [
                    {"code": "DOCUMENT_ROUTE_FALLBACK", "message": exc.message}
                ]
            raise
        groups = result.get("groups") or []
        selected: list[str] = []
        warnings: list[dict[str, Any]] = []
        for group in groups:
            selected.extend(group.get("accept_doc_ids") or [])
            selected.extend(group.get("possible_doc_ids") or [])
            warnings.extend(group.get("warnings") or [])
            if group.get("degraded"):
                warnings.append(
                    {
                        "code": "DOCUMENT_ROUTE_DEGRADED",
                        "message": "Document routing used a degraded decision path.",
                    }
                )
        return list(dict.fromkeys(selected)), warnings

    async def raw(self, *, user_id: str, kb_id: str, doc_id: str) -> dict[str, Any]:
        started = monotonic()
        response = await self._request_response(
            "POST",
            "/rag/v1/documents/raw",
            json={"user_id": user_id, "kb_id": kb_id, "doc_id": doc_id},
            accepted_statuses={200, 202, 422},
        )
        if response.status_code == 422:
            raise AppError(
                422,
                "RAW_UNAVAILABLE",
                "Raw document content is unavailable.",
                stage="docs_research",
            )
        payload = response.json()
        while response.status_code == 202:
            if monotonic() - started >= self.settings.raw_repair_max_wait_seconds:
                raise AppError(
                    504,
                    "RAW_REPAIR_TIMEOUT",
                    "Raw document repair did not complete in time.",
                    True,
                    "docs_research",
                )
            await asyncio.sleep(max(1, int(payload.get("retry_after", 2))))
            response = await self._request_response(
                "GET", str(payload["status_url"]), accepted_statuses={200}
            )
            status = response.json()
            if status.get("status") == "completed":
                response = await self._request_response(
                    "POST",
                    "/rag/v1/documents/raw",
                    json={"user_id": user_id, "kb_id": kb_id, "doc_id": doc_id},
                    accepted_statuses={200},
                )
                break
            if status.get("status") == "failed":
                raise AppError(
                    422,
                    str(status.get("error_code") or "RAW_REPAIR_FAILED"),
                    str(status.get("message") or "Raw document repair failed."),
                    bool(status.get("retryable")),
                    "docs_research",
                )
            payload["retry_after"] = 2
        result = response.json()
        if not isinstance(result, dict):
            raise AppError(502, "RETRIEVAL_INVALID_RESPONSE", "Retrieval returned invalid JSON.")
        return result

    async def retrieve(
        self,
        *,
        user_id: str,
        kb_id: str,
        session_id: str,
        doc_ids: list[str],
        temp_doc_ids: list[str],
        queries: list[str],
        ensure_document_coverage: bool,
        debug: bool = False,
        max_return_tokens: int = 16_000,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/rag/v1/retrieve",
            json={
                "user_id": user_id,
                "kb_id": kb_id,
                "session_id": session_id,
                "doc_ids": doc_ids,
                "temp_doc_ids": temp_doc_ids,
                "query": queries,
                "top_k": 12,
                "max_return_tokens": max_return_tokens,
                "search_mode": "hybrid",
                "options": {
                    "include_document_meta": True,
                    "include_debug": debug,
                    "ensure_document_coverage": ensure_document_coverage,
                },
            },
        )

    async def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._request_response(method, path, json=json, accepted_statuses={200})
        payload = response.json()
        if not isinstance(payload, dict):
            raise AppError(502, "RETRIEVAL_INVALID_RESPONSE", "Retrieval returned invalid JSON.")
        return payload

    async def _request_response(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        accepted_statuses: set[int],
    ) -> httpx.Response:
        last_error: Exception | None = None
        started = monotonic()
        trace_id = prefixed_id("tool") if current() is not None else ""
        attempts = 0
        trace_finished = False

        def trace_call(
            status: str,
            *,
            response: httpx.Response | None = None,
            error: BaseException | None = None,
        ) -> None:
            nonlocal trace_finished
            if not trace_id or trace_finished:
                return
            trace_finished = True
            value: dict[str, Any] = {
                "id": trace_id,
                "kind": "tool",
                "tool": "retrieval_http",
                "provider": "retrieval_service",
                "status": status,
                "method": method,
                "path": path,
                "arguments": json,
                "attempts": attempts,
                "elapsed_ms": round((monotonic() - started) * 1000, 3),
            }
            if response is not None:
                value["status_code"] = response.status_code
                value["result"] = _response_debug_value(response)
            if error is not None:
                value["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "code": getattr(error, "code", None),
                }
            record("tool_calls", value)

        try:
            probe = await self._breaker.before_call()
        except AppError as exc:
            trace_call("failed", error=exc)
            raise
        for attempt in range(self.settings.retrieval_max_retries + 1):
            attempts = attempt + 1
            try:
                async with self._semaphore:
                    response = await self._client.request(method, path, json=json)
                if response.status_code in accepted_statuses:
                    await self._breaker.success(probe)
                    trace_call("completed", response=response)
                    return response
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    rejected = AppError(
                        response.status_code,
                        "RETRIEVAL_REQUEST_REJECTED",
                        f"Retrieval returned HTTP {response.status_code}.",
                        details={"body": response.text[:2000]},
                    )
                    trace_call("failed", response=response, error=rejected)
                    raise rejected
                last_error = AppError(
                    503,
                    "RETRIEVAL_UNAVAILABLE",
                    f"Retrieval returned HTTP {response.status_code}.",
                    True,
                )
            except asyncio.CancelledError:
                await self._breaker.failure(counts=False, recovery_probe=probe)
                trace_call("cancelled", error=asyncio.CancelledError())
                raise
            except AppError as exc:
                await self._breaker.failure(counts=False, recovery_probe=probe)
                trace_call("failed", error=exc)
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
            if attempt < self.settings.retrieval_max_retries:
                await asyncio.sleep(
                    self.settings.retrieval_retry_base_seconds * (2**attempt) * random.uniform(0.75, 1.25)
                )
        await self._breaker.failure(counts=True, recovery_probe=probe)
        unavailable = AppError(
            503,
            "RETRIEVAL_UNAVAILABLE",
            "Retrieval service is unavailable after bounded retries.",
            True,
            details={"cause": type(last_error).__name__ if last_error else "unknown"},
        )
        trace_call("failed", error=last_error or unavailable)
        raise unavailable

    async def close(self) -> None:
        await self._client.aclose()


def raw_text(payload: dict[str, Any]) -> str:
    raw_mineru = payload.get("raw_mineru") or {}
    markdown = raw_mineru.get("md_content")
    if isinstance(markdown, str) and markdown.strip():
        return markdown.strip()
    pages = payload.get("pages") or []
    return "\n\n".join(
        str(page.get("content", "")).strip()
        for page in sorted(pages, key=lambda item: item.get("page", 0))
        if str(page.get("content", "")).strip()
    )


def _response_debug_value(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
