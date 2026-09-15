from __future__ import annotations

import asyncio
import random
from time import monotonic

import httpx

from app.config import AppSettings
from app.core.errors import AppError
from app.core.ids import prefixed_id
from app.core.resilience import CircuitBreaker
from app.debug.trace import current, record

_WEB_SEARCH_MODELS = {"doubao-seedream-5-0-260128"}


class SeedreamClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.seedream_max_concurrency)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.seedream_connect_timeout_seconds,
                read=settings.seedream_call_max_seconds,
                write=settings.seedream_call_max_seconds,
                pool=settings.seedream_connect_timeout_seconds,
            )
        )
        self._breaker = CircuitBreaker(
            enabled=settings.seedream_circuit_breaker_enabled,
            threshold=settings.seedream_circuit_failure_threshold,
            recovery_seconds=settings.seedream_circuit_recovery_seconds,
            error_code="SEEDREAM_CIRCUIT_OPEN",
            provider_name="Seedream",
        )

    async def generate(self, prompt: str) -> str:
        last_error: Exception | None = None
        started = monotonic()
        trace_id = prefixed_id("tool") if current() is not None else ""
        attempts = 0
        trace_finished = False
        web_search_enabled = self.settings.seedream_model in _WEB_SEARCH_MODELS

        def trace_call(status: str, *, result: object = None, error: BaseException | None = None) -> None:
            nonlocal trace_finished
            if not trace_id or trace_finished:
                return
            trace_finished = True
            value: dict[str, object] = {
                "id": trace_id,
                "kind": "tool",
                "tool": "seedream.generate",
                "provider": "volcengine_ark",
                "status": status,
                "arguments": {
                    "prompt": prompt,
                    "size": self.settings.seedream_size,
                    "web_search_enabled": web_search_enabled,
                },
                "attempts": attempts,
                "elapsed_ms": round((monotonic() - started) * 1000, 3),
            }
            if result is not None:
                value["result"] = result
            if error is not None:
                value["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "code": getattr(error, "code", None),
                }
            record("tool_calls", value)

        if not self.settings.ark_api_key:
            missing_key = AppError(503, "ARK_API_KEY_MISSING", "ARK_API_KEY is not configured.")
            trace_call("failed", error=missing_key)
            raise missing_key

        try:
            probe = await self._breaker.before_call()
        except AppError as exc:
            trace_call("failed", error=exc)
            raise
        for attempt in range(self.settings.seedream_max_retries + 1):
            attempts = attempt + 1
            try:
                async with self._semaphore:
                    payload = {
                        "model": self.settings.seedream_model,
                        "prompt": prompt,
                        "response_format": "url",
                        "size": self.settings.seedream_size,
                        "watermark": False,
                    }
                    if web_search_enabled:
                        payload["tools"] = [{"type": "web_search"}]
                    response = await self._client.post(
                        str(self.settings.seedream_responses_url),
                        headers={"Authorization": f"Bearer {self.settings.ark_api_key}"},
                        json=payload,
                    )
                if response.status_code < 300:
                    payload = response.json()
                    data = payload.get("data") or []
                    url = str(data[0].get("url", "")) if data else ""
                    if url.startswith(("https://", "http://")):
                        await self._breaker.success(probe)
                        trace_call("completed", result={"url": url})
                        return url
                    raise AppError(
                        502,
                        "SEEDREAM_INVALID_RESPONSE",
                        "Seedream response did not contain an image URL.",
                    )
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    raise AppError(
                        response.status_code,
                        "SEEDREAM_REQUEST_REJECTED",
                        f"Seedream returned HTTP {response.status_code}.",
                        details={"body": response.text[:2000]},
                    )
                last_error = AppError(
                    503,
                    "SEEDREAM_UNAVAILABLE",
                    f"Seedream returned HTTP {response.status_code}.",
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
            if attempt < self.settings.seedream_max_retries:
                await asyncio.sleep(
                    self.settings.seedream_retry_base_seconds * (2**attempt) * random.uniform(0.75, 1.25)
                )
        await self._breaker.failure(counts=True, recovery_probe=probe)
        unavailable = AppError(
            503,
            "SEEDREAM_UNAVAILABLE",
            "Seedream is unavailable after bounded retries.",
            True,
            details={"cause": type(last_error).__name__ if last_error else "unknown"},
        )
        trace_call("failed", error=last_error or unavailable)
        raise unavailable

    async def close(self) -> None:
        await self._client.aclose()
