#!/usr/bin/env python3
"""Probe Ark Responses caching and service-tier capabilities without touching app code."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
DEFAULT_MODEL = "doubao-seed-2-1-pro-260628"


@dataclass
class Observation:
    scenario: str
    attempt: int
    requested_service_tier: str | None
    actual_service_tier: str | None
    status: str
    http_status: int
    first_event_ms: float | None
    total_ms: float
    input_tokens: int | None
    cached_tokens: int | None
    output_tokens: int | None
    response_id_suffix: str | None
    error_code: str | None = None
    error_message: str | None = None


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


def _representative_prefix() -> str:
    sys.path.insert(0, str(PROJECT_ROOT))
    from app.prompts.system import (  # noqa: PLC0415
        ARTICLE_SYSTEM_PROMPT,
        OUTLINE_SYSTEM_PROMPT,
        TASK_SPEC_SYSTEM_PROMPT,
    )

    return (
        ARTICLE_SYSTEM_PROMPT
        + "\n\n# 固定写作契约参考\n"
        + TASK_SPEC_SYSTEM_PROMPT
        + "\n\n"
        + OUTLINE_SYSTEM_PROMPT
        + "\n\n# 能力探测覆盖规则\n"
        + "以上内容用于模拟文章节点中稳定的系统规则、字段契约和已批准写作约束。"
        + "本请求只测试公共前缀缓存与服务等级，最终仅按用户要求返回简短标记。"
    )


def _usage(response: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    details = usage.get("input_tokens_details")
    if not isinstance(details, dict):
        details = {}
    return (
        _optional_int(usage.get("input_tokens")),
        _optional_int(details.get("cached_tokens")),
        _optional_int(usage.get("output_tokens")),
    )


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _id_suffix(value: Any) -> str | None:
    text = str(value or "")
    return text[-8:] if text else None


def _redact_error_message(message: str) -> str:
    redacted = re.sub(r"(?i)(request\s+id\s*:\s*)[A-Za-z0-9_-]+", r"\1[redacted]", message)
    redacted = re.sub(
        r"(?i)(account(?:\s+id)?\s*)(?:\[)?\d{6,}(?:\])?",
        r"\1[redacted]",
        redacted,
    )
    return redacted[:500]


class ArkProbe:
    def __init__(self, *, api_key: str, url: str, model: str, timeout: float) -> None:
        self.url = url
        self.model = model
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout, connect=15),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def stream(
        self,
        *,
        scenario: str,
        attempt: int,
        payload: dict[str, Any],
    ) -> tuple[Observation, dict[str, Any]]:
        started = time.perf_counter()
        first_event: float | None = None
        completed: dict[str, Any] = {}
        try:
            async with self.client.stream("POST", self.url, json=payload) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    error = _json_object(body)
                    return (
                        _error_observation(
                            scenario=scenario,
                            attempt=attempt,
                            requested_service_tier=payload.get("service_tier"),
                            status_code=response.status_code,
                            started=started,
                            error=error,
                        ),
                        error,
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:") or line == "data: [DONE]":
                        continue
                    event = json.loads(line[5:].strip())
                    event_type = str(event.get("type") or "")
                    if first_event is None and event_type in {
                        "response.output_text.delta",
                        "response.reasoning_summary_text.delta",
                    }:
                        first_event = time.perf_counter()
                    if event_type == "response.completed":
                        completed = event.get("response") or {}
                    elif event_type in {"response.failed", "error"}:
                        completed = event.get("response") or event
            elapsed = time.perf_counter() - started
            input_tokens, cached_tokens, output_tokens = _usage(completed)
            error = completed.get("error") if isinstance(completed.get("error"), dict) else {}
            return (
                Observation(
                    scenario=scenario,
                    attempt=attempt,
                    requested_service_tier=payload.get("service_tier"),
                    actual_service_tier=completed.get("service_tier"),
                    status=str(completed.get("status") or "completed"),
                    http_status=200,
                    first_event_ms=(first_event - started) * 1000 if first_event else None,
                    total_ms=elapsed * 1000,
                    input_tokens=input_tokens,
                    cached_tokens=cached_tokens,
                    output_tokens=output_tokens,
                    response_id_suffix=_id_suffix(completed.get("id")),
                    error_code=str(error.get("code") or "") or None,
                    error_message=_redact_error_message(str(error.get("message") or "")) or None,
                ),
                completed,
            )
        except Exception as exc:
            return (
                Observation(
                    scenario=scenario,
                    attempt=attempt,
                    requested_service_tier=payload.get("service_tier"),
                    actual_service_tier=None,
                    status="client_error",
                    http_status=0,
                    first_event_ms=None,
                    total_ms=(time.perf_counter() - started) * 1000,
                    input_tokens=None,
                    cached_tokens=None,
                    output_tokens=None,
                    response_id_suffix=None,
                    error_code=type(exc).__name__,
                    error_message=_redact_error_message(str(exc)),
                ),
                {},
            )

    async def request(
        self,
        *,
        scenario: str,
        attempt: int,
        payload: dict[str, Any],
    ) -> tuple[Observation, dict[str, Any]]:
        started = time.perf_counter()
        response = await self.client.post(self.url, json=payload)
        elapsed = time.perf_counter() - started
        data = _json_object(response.content)
        if response.status_code >= 400:
            return (
                _error_observation(
                    scenario=scenario,
                    attempt=attempt,
                    requested_service_tier=payload.get("service_tier"),
                    status_code=response.status_code,
                    started=started,
                    elapsed=elapsed,
                    error=data,
                ),
                data,
            )
        input_tokens, cached_tokens, output_tokens = _usage(data)
        return (
            Observation(
                scenario=scenario,
                attempt=attempt,
                requested_service_tier=payload.get("service_tier"),
                actual_service_tier=data.get("service_tier"),
                status=str(data.get("status") or "completed"),
                http_status=response.status_code,
                first_event_ms=None,
                total_ms=elapsed * 1000,
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
                output_tokens=output_tokens,
                response_id_suffix=_id_suffix(data.get("id")),
            ),
            data,
        )

    async def delete_response(self, response_id: str) -> bool:
        base = self.url.removesuffix("/responses")
        response = await self.client.delete(f"{base}/responses/{response_id}")
        return response.status_code < 300


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw_body": raw.decode(errors="replace")[:1000]}
    return value if isinstance(value, dict) else {"value": value}


def _error_observation(
    *,
    scenario: str,
    attempt: int,
    requested_service_tier: str | None,
    status_code: int,
    started: float,
    error: dict[str, Any],
    elapsed: float | None = None,
) -> Observation:
    nested = error.get("error") if isinstance(error.get("error"), dict) else error
    return Observation(
        scenario=scenario,
        attempt=attempt,
        requested_service_tier=requested_service_tier,
        actual_service_tier=None,
        status="http_error",
        http_status=status_code,
        first_event_ms=None,
        total_ms=(elapsed if elapsed is not None else time.perf_counter() - started) * 1000,
        input_tokens=None,
        cached_tokens=None,
        output_tokens=None,
        response_id_suffix=None,
        error_code=str(nested.get("code") or "") or None,
        error_message=_redact_error_message(str(nested.get("message") or error)),
    )


def _base_payload(prefix: str, marker: str) -> dict[str, Any]:
    return {
        "model": "",
        "input": [
            {"role": "system", "content": prefix},
            {"role": "user", "content": f"只返回标记 `{marker}`，不要输出其他内容。"},
        ],
        "stream": True,
        "store": False,
        "max_output_tokens": 64,
        "thinking": {"type": "disabled"},
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    _load_env_file(Path(args.env_file))
    api_key = os.environ.get("ARK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("ARK_API_KEY is missing; set it in the environment or --env-file")
    model = args.model or os.environ.get("ARK_MODEL_MAIN") or DEFAULT_MODEL
    url = args.url or os.environ.get("ARK_RESPONSES_URL") or DEFAULT_URL
    prefix = _representative_prefix()
    probe = ArkProbe(api_key=api_key, url=url, model=model, timeout=args.timeout)
    observations: list[Observation] = []
    cleanup: list[dict[str, Any]] = []
    run_nonce = uuid4().hex[:10]

    try:
        if args.mode in {"all", "implicit"}:
            for attempt in range(1, args.trials + 1):
                payload = _base_payload(prefix, f"implicit-{run_nonce}-{attempt}")
                payload["model"] = model
                observation, _ = await probe.stream(
                    scenario="implicit_cache_repeated_prefix",
                    attempt=attempt,
                    payload=payload,
                )
                observations.append(observation)

        if args.mode in {"all", "current"}:
            for attempt in range(1, 3):
                payload = _base_payload(prefix, f"current-{run_nonce}-{attempt}")
                payload.update({"model": model, "caching": {"type": "enabled"}})
                observation, response_data = await probe.stream(
                    scenario="current_gateway_shape_store_false",
                    attempt=attempt,
                    payload=payload,
                )
                observations.append(observation)
                response_id = str(response_data.get("id") or "")
                if response_id:
                    cleanup.append(
                        {
                            "response_id_suffix": _id_suffix(response_id),
                            "deleted": await probe.delete_response(response_id),
                        }
                    )

        if args.mode in {"all", "explicit"}:
            expire_at = int(time.time()) + 3600
            create_payload = {
                "model": model,
                "input": [{"role": "system", "content": prefix}],
                "stream": False,
                "store": True,
                "caching": {"type": "enabled", "prefix": True},
                "expire_at": expire_at,
                "thinking": {"type": "disabled"},
            }
            created, created_data = await probe.request(
                scenario="explicit_prefix_create",
                attempt=1,
                payload=create_payload,
            )
            observations.append(created)
            cache_id = str(created_data.get("id") or "")
            if cache_id:
                for attempt in range(1, args.trials + 1):
                    payload = {
                        "model": model,
                        "previous_response_id": cache_id,
                        "input": f"只返回标记 `explicit-{run_nonce}-{attempt}`，不要输出其他内容。",
                        "stream": True,
                        "store": False,
                        "max_output_tokens": 64,
                        "thinking": {"type": "disabled"},
                    }
                    observation, _ = await probe.stream(
                        scenario="explicit_prefix_hit",
                        attempt=attempt,
                        payload=payload,
                    )
                    observations.append(observation)
                cleanup.append(
                    {
                        "response_id_suffix": _id_suffix(cache_id),
                        "deleted": await probe.delete_response(cache_id),
                    }
                )

        if args.mode in {"all", "tier"}:
            for attempt in range(1, args.trials + 1):
                for tier in ("default", "fast"):
                    unique_prefix = f"探测批次 {uuid4().hex}。\n{prefix}"
                    payload = _base_payload(unique_prefix, f"tier-{tier}-{run_nonce}-{attempt}")
                    payload.update({"model": model, "service_tier": tier})
                    observation, _ = await probe.stream(
                        scenario=f"service_tier_{tier}",
                        attempt=attempt,
                        payload=payload,
                    )
                    observations.append(observation)
    finally:
        await probe.close()

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model,
        "url": url,
        "mode": args.mode,
        "trials": args.trials,
        "representative_prefix_chars": len(prefix),
        "observations": [asdict(item) for item in observations],
        "summary": _summarize(observations),
        "cleanup": cleanup,
        "notes": [
            "API key and full response IDs are intentionally omitted.",
            "Seed 2.0+ implicit caching is provider-managed and cannot be disabled.",
            "Explicit prefix cache creation is billed for at least one storage hour.",
            "The probe prefix intentionally exceeds the 1024-token implicit-cache threshold.",
        ],
    }


def _summarize(observations: list[Observation]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for scenario in sorted({item.scenario for item in observations}):
        items = [item for item in observations if item.scenario == scenario]
        successful = [item for item in items if item.http_status == 200 and item.status == "completed"]
        first_event = [item.first_event_ms for item in successful if item.first_event_ms is not None]
        totals = [item.total_ms for item in successful]
        cached = [item.cached_tokens or 0 for item in successful]
        result[scenario] = {
            "requests": len(items),
            "successful": len(successful),
            "cache_hits": sum(value > 0 for value in cached),
            "cached_tokens": cached,
            "median_first_event_ms": round(statistics.median(first_event), 2) if first_event else None,
            "median_total_ms": round(statistics.median(totals), 2) if totals else None,
            "actual_service_tiers": sorted(
                {item.actual_service_tier for item in successful if item.actual_service_tier}
            ),
            "errors": [
                {"code": item.error_code, "message": item.error_message}
                for item in items
                if item.http_status != 200 or item.status != "completed"
            ],
        }
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("all", "implicit", "current", "explicit", "tier"),
        default="all",
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--model", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.trials < 2 or args.trials > 10:
        parser.error("--trials must be between 2 and 10")
    return args


def main() -> None:
    args = _parse_args()
    report = asyncio.run(_run(args))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Probe report written to {output}")
    print(rendered)


if __name__ == "__main__":
    main()
