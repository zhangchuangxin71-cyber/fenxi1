from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

MAX_SUPPORTED_CONCURRENCY = 30


@dataclass(frozen=True, slots=True)
class LoadConfig:
    base_url: str
    api_key: str
    request_count: int
    concurrency: int
    timeout_seconds: float
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if self.request_count < 1:
            raise ValueError("request_count must be positive")
        if not 1 <= self.concurrency <= MAX_SUPPORTED_CONCURRENCY:
            raise ValueError(f"concurrency must be between 1 and {MAX_SUPPORTED_CONCURRENCY}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class RequestResult:
    index: int
    status_code: int
    total_ms: float
    first_event_ms: float | None = None
    first_content_ms: float | None = None
    done: bool = False
    error_code: str | None = None
    request_id: str | None = None
    answer_basis: str | None = None


@dataclass(frozen=True, slots=True)
class LoadRun:
    results: tuple[RequestResult, ...]
    max_in_flight: int
    wall_time_ms: float


@dataclass(slots=True)
class _InFlight:
    active: int = 0
    maximum: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def enter(self) -> None:
        async with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)

    async def leave(self) -> None:
        async with self.lock:
            self.active -= 1


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(min(1.0, max(0.0, quantile)) * len(ordered)))
    return ordered[rank - 1]


def _error_code_from_http(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "http_error"
    if not isinstance(payload, dict):
        return "http_error"
    error = payload.get("error")
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    return "http_error"


async def _send_one(
    *,
    index: int,
    config: LoadConfig,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    in_flight: _InFlight,
) -> RequestResult:
    async with semaphore:
        await in_flight.enter()
        started = time.perf_counter()
        first_event_ms: float | None = None
        first_content_ms: float | None = None
        status_code = 0
        done = False
        error_code: str | None = None
        request_id: str | None = None
        answer_basis: str | None = None
        headers = {"X-Request-Id": f"load-{index:04d}"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        try:
            async with client.stream(
                "POST",
                f"{config.base_url.rstrip('/')}/v1/chat/completions",
                headers=headers,
                json=config.payload,
                timeout=config.timeout_seconds,
            ) as response:
                status_code = response.status_code
                request_id = response.headers.get("X-Request-Id")
                if status_code != 200:
                    error_code = _error_code_from_http(await response.aread())
                else:
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        elapsed_ms = (time.perf_counter() - started) * 1000
                        if first_event_ms is None:
                            first_event_ms = elapsed_ms
                        data = line.removeprefix("data: ")
                        if data == "[DONE]":
                            done = True
                            continue
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            error_code = error_code or "invalid_sse_json"
                            continue
                        if not isinstance(event, dict):
                            error_code = error_code or "invalid_sse_event"
                            continue
                        rag = event.get("rag")
                        if isinstance(rag, dict):
                            if isinstance(rag.get("error"), dict):
                                error_code = str(rag["error"].get("code") or "sse_error")
                            if rag.get("answer_basis"):
                                answer_basis = str(rag["answer_basis"])
                        choices = event.get("choices")
                        if isinstance(choices, list) and choices:
                            delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                            content = delta.get("content") if isinstance(delta, dict) else None
                            if content and first_content_ms is None:
                                first_content_ms = elapsed_ms
        except httpx.TimeoutException:
            error_code = "client_timeout"
        except httpx.HTTPError:
            error_code = "client_http_error"
        finally:
            total_ms = (time.perf_counter() - started) * 1000
            await in_flight.leave()
        return RequestResult(
            index=index,
            status_code=status_code,
            total_ms=total_ms,
            first_event_ms=first_event_ms,
            first_content_ms=first_content_ms,
            done=done,
            error_code=error_code,
            request_id=request_id,
            answer_basis=answer_basis,
        )


async def run_load(
    config: LoadConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> LoadRun:
    semaphore = asyncio.Semaphore(config.concurrency)
    in_flight = _InFlight()
    started = time.perf_counter()

    async def execute(active_client: httpx.AsyncClient) -> tuple[RequestResult, ...]:
        results = await asyncio.gather(
            *(
                _send_one(
                    index=index,
                    config=config,
                    client=active_client,
                    semaphore=semaphore,
                    in_flight=in_flight,
                )
                for index in range(config.request_count)
            )
        )
        return tuple(results)

    if client is None:
        limits = httpx.Limits(
            max_connections=config.concurrency,
            max_keepalive_connections=config.concurrency,
        )
        async with httpx.AsyncClient(limits=limits) as active_client:
            results = await execute(active_client)
    else:
        results = await execute(client)

    return LoadRun(
        results=results,
        max_in_flight=in_flight.maximum,
        wall_time_ms=(time.perf_counter() - started) * 1000,
    )


def _latency_summary(values: list[float]) -> dict[str, float | None]:
    return {
        "avg_ms": round(sum(values) / len(values), 2) if values else None,
        "p50_ms": round(percentile(values, 0.50) or 0.0, 2) if values else None,
        "p95_ms": round(percentile(values, 0.95) or 0.0, 2) if values else None,
        "max_ms": round(max(values), 2) if values else None,
    }


def summarize(run: LoadRun) -> dict[str, Any]:
    results = list(run.results)
    errors = Counter(item.error_code for item in results if item.error_code)
    app_rate_limited = sum(
        item.status_code == 429 or item.error_code == "rate_limit_exceeded" for item in results
    )
    upstream_errors = sum(item.error_code == "ark_upstream_error" for item in results)
    successful = sum(item.status_code == 200 and item.done and item.error_code is None for item in results)
    total_values = [item.total_ms for item in results]
    first_event_values = [item.first_event_ms for item in results if item.first_event_ms is not None]
    first_content_values = [item.first_content_ms for item in results if item.first_content_ms is not None]
    assessment = "passed"
    if app_rate_limited:
        assessment = "application_rate_limit_reached"
    elif upstream_errors:
        assessment = "ark_or_upstream_limit_reached"
    elif successful != len(results):
        assessment = "application_or_network_errors"

    return {
        "assessment": assessment,
        "requests": len(results),
        "successful": successful,
        "failed": len(results) - successful,
        "app_rate_limited": app_rate_limited,
        "upstream_errors": upstream_errors,
        "done_received": sum(item.done for item in results),
        "max_in_flight": run.max_in_flight,
        "wall_time_ms": round(run.wall_time_ms, 2),
        "throughput_rps": (
            round(len(results) / (run.wall_time_ms / 1000), 2) if run.wall_time_ms > 0 else None
        ),
        "total_latency": _latency_summary(total_values),
        "first_event_latency": _latency_summary(first_event_values),
        "first_content_latency": _latency_summary(first_content_values),
        "http_statuses": dict(sorted(Counter(item.status_code for item in results).items())),
        "error_codes": dict(sorted(errors.items())),
        "answer_bases": dict(
            sorted(Counter(item.answer_basis for item in results if item.answer_basis).items())
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded concurrent SSE load test for POST /v1/chat/completions."
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("LOAD_CHAT_URL", "http://127.0.0.1:8130"),
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("LOAD_CHAT_API_KEY", ""),
        help="Prefer LOAD_CHAT_API_KEY to avoid putting credentials in shell history.",
    )
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--user-id", default="load-test-user")
    parser.add_argument("--kb-id", default="load-test-kb")
    parser.add_argument("--doc-id", action="append", default=[])
    parser.add_argument(
        "--query",
        default="请用一句话说明你能提供什么帮助。",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=96)
    parser.add_argument("--include-debug", action="store_true")
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    payload = {
        "model": "rag-knowledge-chat",
        "messages": [{"role": "user", "content": args.query}],
        "stream": True,
        "temperature": 0,
        "max_tokens": args.max_tokens,
        "rag": {
            "user_id": args.user_id,
            "kb_id": args.kb_id,
            "doc_ids": args.doc_id,
            "temp_doc_ids": [],
            "top_k": args.top_k,
            "include_debug": args.include_debug,
        },
    }
    try:
        config = LoadConfig(
            base_url=args.base_url,
            api_key=args.api_key,
            request_count=args.requests,
            concurrency=args.concurrency,
            timeout_seconds=args.timeout_seconds,
            payload=payload,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    summary = summarize(await run_load(config))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["assessment"] == "passed" else 1


def main() -> None:
    raise SystemExit(asyncio.run(_main_async(_parser().parse_args())))


if __name__ == "__main__":
    main()
