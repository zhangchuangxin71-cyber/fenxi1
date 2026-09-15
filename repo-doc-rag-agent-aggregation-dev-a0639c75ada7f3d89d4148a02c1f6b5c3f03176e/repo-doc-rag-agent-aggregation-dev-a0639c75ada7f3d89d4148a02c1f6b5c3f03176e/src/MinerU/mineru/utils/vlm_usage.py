# Copyright (c) Opendatalab. All rights reserved.
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()
_SUMMARY: dict[str, Any] = {
    "calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "input_cost_cny": 0.0,
    "output_cost_cny": 0.0,
    "total_cost_cny": 0.0,
    "models": {},
}


def _env_float(*names: str) -> float | None:
    for name in names:
        value = os.getenv(name)
        if value is None or value.strip() == "":
            continue
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _token_count(usage: dict[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    return 0


def _round_money(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 8)


def _append_jsonl(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _write_json(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _price_config() -> dict[str, float | None]:
    input_price = _env_float(
        "MINERU_VL_INPUT_PRICE_CNY_PER_1M",
        "MINERU_VL_INPUT_PRICE_CNY_PER_MILLION",
    )
    output_price = _env_float(
        "MINERU_VL_OUTPUT_PRICE_CNY_PER_1M",
        "MINERU_VL_OUTPUT_PRICE_CNY_PER_MILLION",
    )
    return {
        "input_price_cny_per_1m_tokens": input_price,
        "output_price_cny_per_1m_tokens": output_price,
    }


def _usage_cost(prompt_tokens: int, completion_tokens: int) -> dict[str, float | None]:
    prices = _price_config()
    input_price = prices["input_price_cny_per_1m_tokens"]
    output_price = prices["output_price_cny_per_1m_tokens"]

    input_cost = None
    output_cost = None
    if input_price is not None:
        input_cost = prompt_tokens * input_price / 1_000_000
    if output_price is not None:
        output_cost = completion_tokens * output_price / 1_000_000

    total_cost = None
    if input_cost is not None and output_cost is not None:
        total_cost = input_cost + output_cost

    return {
        **prices,
        "input_cost_cny": _round_money(input_cost),
        "output_cost_cny": _round_money(output_cost),
        "total_cost_cny": _round_money(total_cost),
    }


def _request_id(response: Any) -> str | None:
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    for name in (
        "x-request-id",
        "x-tt-logid",
        "x-volc-request-id",
        "x-ark-request-id",
    ):
        value = headers.get(name)
        if value:
            return value
    return None


def record_vlm_usage(response_data: dict[str, Any], *, client: Any, response: Any) -> None:
    usage = response_data.get("usage")
    if not isinstance(usage, dict):
        return

    log_path = os.getenv("MINERU_VL_USAGE_LOG", "").strip()
    summary_path = os.getenv("MINERU_VL_USAGE_SUMMARY", "").strip()
    if not log_path and not summary_path:
        return

    prompt_tokens = _token_count(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _token_count(usage, "completion_tokens", "output_tokens")
    total_tokens = _token_count(usage, "total_tokens")
    if not total_tokens:
        total_tokens = prompt_tokens + completion_tokens

    model = str(
        response_data.get("model")
        or getattr(client, "model_name", None)
        or os.getenv("MINERU_VL_MODEL_NAME", "")
    )
    cost = _usage_cost(prompt_tokens, completion_tokens)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": os.getenv("MINERU_VL_PROVIDER", "").strip() or None,
        "model": model,
        "request_id": _request_id(response),
        "usage": usage,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        **cost,
    }

    with _LOCK:
        _SUMMARY["calls"] += 1
        _SUMMARY["prompt_tokens"] += prompt_tokens
        _SUMMARY["completion_tokens"] += completion_tokens
        _SUMMARY["total_tokens"] += total_tokens
        if cost["input_cost_cny"] is not None:
            _SUMMARY["input_cost_cny"] += cost["input_cost_cny"]
        if cost["output_cost_cny"] is not None:
            _SUMMARY["output_cost_cny"] += cost["output_cost_cny"]
        if cost["total_cost_cny"] is not None:
            _SUMMARY["total_cost_cny"] += cost["total_cost_cny"]

        model_summary = _SUMMARY["models"].setdefault(
            model,
            {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        )
        model_summary["calls"] += 1
        model_summary["prompt_tokens"] += prompt_tokens
        model_summary["completion_tokens"] += completion_tokens
        model_summary["total_tokens"] += total_tokens

        if log_path:
            payload["call_index"] = _SUMMARY["calls"]
            _append_jsonl(log_path, payload)

        if summary_path:
            prices = _price_config()
            summary_payload = {
                **_SUMMARY,
                **prices,
                "input_cost_cny": (
                    _round_money(_SUMMARY["input_cost_cny"])
                    if prices["input_price_cny_per_1m_tokens"] is not None
                    else None
                ),
                "output_cost_cny": (
                    _round_money(_SUMMARY["output_cost_cny"])
                    if prices["output_price_cny_per_1m_tokens"] is not None
                    else None
                ),
                "total_cost_cny": (
                    _round_money(_SUMMARY["total_cost_cny"])
                    if (
                        prices["input_price_cny_per_1m_tokens"] is not None
                        and prices["output_price_cny_per_1m_tokens"] is not None
                    )
                    else None
                ),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_json(summary_path, summary_payload)
