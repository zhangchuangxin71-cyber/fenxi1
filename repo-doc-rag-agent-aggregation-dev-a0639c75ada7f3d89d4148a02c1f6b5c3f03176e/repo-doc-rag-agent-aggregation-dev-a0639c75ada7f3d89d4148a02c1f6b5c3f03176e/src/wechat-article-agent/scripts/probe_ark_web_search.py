#!/usr/bin/env python3
"""Probe Ark built-in Web Search and, optionally, the standalone Doubao Search API.

This is a development diagnostic only. It never prints API keys and does not write
probe results unless --output is explicitly provided.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

import httpx


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if re.search(r"key|token|secret|authorization", key, re.IGNORECASE)
            else redact(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact(child) for child in value]
    return value


def parse_sse(
    response: httpx.Response,
    *,
    search_items: dict[str, dict[str, Any]],
    annotations: list[dict[str, Any]],
    output_text: list[str],
    event_counts: Counter[str],
    usage: dict[str, Any],
) -> None:
    for line in response.iter_lines():
        if not line.startswith("data:") or line == "data: [DONE]":
            continue
        event = json.loads(line[5:].strip())
        event_type = str(event.get("type", ""))
        event_counts[event_type] += 1
        if event_type == "response.output_text.delta":
            output_text.append(str(event.get("delta", "")))
        for item in walk(event):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "web_search_call" and item.get("id"):
                search_items[str(item["id"])] = item
            if item.get("type") in {"url_citation", "web_search_url_citation"}:
                if item not in annotations:
                    annotations.append(item)
        if event_type == "response.completed":
            usage.update((event.get("response") or {}).get("usage") or {})


def probe_builtin(
    client: httpx.Client,
    *,
    url: str,
    api_key: str,
    model: str,
    query: str,
    sources: list[str] | None = None,
    tool_choice: str | dict[str, str] = "auto",
) -> dict[str, Any]:
    tool: dict[str, Any] = {"type": "web_search", "max_keyword": 2, "limit": 5}
    if sources:
        tool["sources"] = sources
    payload = {
        "model": model,
        "stream": True,
        "store": False,
        "thinking": {"type": "disabled"},
        "tools": [tool],
        "tool_choice": tool_choice,
        "max_tool_calls": 2,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": query}]}],
    }
    search_items: dict[str, dict[str, Any]] = {}
    annotations: list[dict[str, Any]] = []
    output_text: list[str] = []
    event_counts: Counter[str] = Counter()
    usage: dict[str, Any] = {}
    result: dict[str, Any] = {
        "query": query,
        "sources_requested": sources,
        "tool_choice": tool_choice,
        "request": redact(payload),
    }
    try:
        with client.stream(
            "POST",
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        ) as response:
            result["http_status"] = response.status_code
            if response.status_code >= 400:
                result["status"] = "error"
                result["error_body"] = response.read().decode(errors="replace")[:3000]
                return result
            parse_sse(
                response,
                search_items=search_items,
                annotations=annotations,
                output_text=output_text,
                event_counts=event_counts,
                usage=usage,
            )
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result.update(
        {
            "status": "ok",
            "event_counts": dict(event_counts),
            "search_calls": list(search_items.values()),
            "annotation_count": len(annotations),
            "annotations": annotations,
            "usage": usage,
            "answer_preview": "".join(output_text)[:3000],
            "has_direct_image_result": False,
            "annotation_cover_images": sum(1 for annotation in annotations if annotation.get("cover_image")),
        }
    )
    return result


def probe_standalone_search(
    client: httpx.Client,
    *,
    api_key: str,
    url: str,
    query: str,
    search_type: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Query": query,
        "SearchType": search_type,
        "Count": 5 if search_type == "web" else 3,
        "Filter": {"NeedContent": True, "NeedUrl": True} if search_type == "web" else {},
        "QueryControl": {"QueryRewrite": True},
    }
    result: dict[str, Any] = {"query": query, "search_type": search_type, "request": redact(payload)}
    try:
        response = client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        result["http_status"] = response.status_code
        data = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    metadata = data.get("ResponseMetadata") or {}
    if response.status_code >= 400 or metadata.get("Error"):
        result["status"] = "error"
        result["error"] = redact(metadata.get("Error") or data)
        return result
    search_result = data.get("Result") or {}
    result.update(
        {
            "status": "ok",
            "result_count": search_result.get("ResultCount", 0),
            "web_results": search_result.get("WebResults") or [],
            "image_results": search_result.get("ImageResults") or [],
            "search_context": search_result.get("SearchContext"),
            "time_cost_ms": search_result.get("TimeCost"),
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument(
        "--standalone-api-key-env",
        default="DOUBAO_SEARCH_API_KEY",
        help="独立豆包搜索 API Key 的环境变量名；缺少时只跳过独立 API 探测。",
    )
    args = parser.parse_args()
    env = load_env(args.env)
    api_key = env.get("ARK_API_KEY") or os.getenv("ARK_API_KEY", "")
    ark_url = env.get("ARK_RESPONSES_URL", "https://ark.cn-beijing.volces.com/api/v3/responses")
    model = env.get("ARK_MODEL_FAST", "doubao-seed-2-1-pro-260628")
    if not api_key:
        raise SystemExit("ARK_API_KEY is missing")

    report: dict[str, Any] = {
        "probe": "ark_web_search",
        "model": model,
        "official_capability_notes": {
            "builtin_tool": (
                "Responses API tools[].type=web_search; text web search, not a documented image search mode"
            ),
            "standalone_api": (
                "豆包搜索 API supports SearchType=web and SearchType=image with a separate Search API key"
            ),
        },
        "builtin": [],
        "standalone": [],
    }
    queries = [
        ("时效性新闻", "请搜索今天的中国科技热点，返回来源和发布时间。", None, "auto"),
        (
            "用户声音_小红书",
            "请搜索小红书上关于新能源汽车续航焦虑的真实用户讨论，区分用户观点与事实。",
            None,
            "auto",
        ),
        (
            "用户声音_抖音_b站",
            "请搜索抖音和哔哩哔哩上关于‘牛来’这个近期网络梗的常见用法和受众语气，只做创作参考。",
            None,
            "auto",
        ),
        (
            "明确图片意图",
            "请搜索广州日报总部大楼的真实图片，并返回图片 URL、来源网页和图片说明。",
            None,
            "auto",
        ),
        (
            "指定附加来源",
            "请搜索最近一周的天气信息，优先使用可用的官方或天气来源。",
            ["search_engine", "moji"],
            "auto",
        ),
        ("强制搜索", "请搜索火山方舟 Web Search 官方文档并列出来源 URL。", None, {"type": "web_search"}),
    ]
    timeout = httpx.Timeout(connect=10, read=args.timeout, write=30, pool=10)
    with httpx.Client(timeout=timeout) as client:
        for name, query, sources, tool_choice in queries:
            item = probe_builtin(
                client,
                url=ark_url,
                api_key=api_key,
                model=model,
                query=query,
                sources=sources,
                tool_choice=tool_choice,
            )
            item["case"] = name
            report["builtin"].append(item)

        standalone_key = env.get(args.standalone_api_key_env) or os.getenv(args.standalone_api_key_env, "")
        if standalone_key:
            standalone_url = env.get(
                "DOUBAO_SEARCH_URL", "https://open.feedcoopapi.com/search_api/web_search"
            )
            for name, query, search_type in (
                ("网页正文", "广州日报公司介绍和发展历史", "web"),
                ("图片结果", "广州日报总部大楼", "image"),
                ("网络梗创作参考", "牛来 网络梗 用法", "web"),
            ):
                item = probe_standalone_search(
                    client,
                    api_key=standalone_key,
                    url=standalone_url,
                    query=query,
                    search_type=search_type,
                )
                item["case"] = name
                report["standalone"].append(item)
        else:
            report["standalone"] = [
                {
                    "status": "skipped",
                    "reason": (
                        f"未配置 {args.standalone_api_key_env}；ARK_API_KEY 不能假定可调用独立豆包搜索 API。"
                    ),
                }
            ]

    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if all(item.get("status") != "error" for item in report["builtin"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
