#!/usr/bin/env python3
"""Probe Seedream 5.0 Lite image generation with its built-in Web Search."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_PROMPT = (
    '联网搜索最近大火的电影"牛来"的信息，然后生成一张与电影《牛来》海报同风格的介绍图，'
    "用来介绍这个电影。"
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--url",
        default="https://ark.cn-beijing.volces.com/api/v3/images/generations",
        help="Ark image generation endpoint.",
    )
    value.add_argument(
        "--model",
        default="doubao-seedream-5-0-260128",
        help="Official Seedream 5.0 Lite model ID.",
    )
    value.add_argument("--prompt", default=DEFAULT_PROMPT)
    value.add_argument("--size", default="2048x2048")
    value.add_argument("--output-dir", type=Path, default=Path("/workspace/test"))
    value.add_argument("--timeout-seconds", type=float, default=600.0)
    return value


def main() -> int:
    args = parser().parse_args()
    api_key = os.getenv("ARK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("ARK_API_KEY is not configured in the process environment")

    request_payload = {
        "model": args.model,
        "prompt": args.prompt,
        "response_format": "url",
        "size": args.size,
        "watermark": False,
        "tools": [{"type": "web_search"}],
    }
    timeout = httpx.Timeout(args.timeout_seconds, connect=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.post(
            args.url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=request_payload,
        )
        if response.status_code >= 300:
            raise SystemExit(
                f"Seedream request failed with HTTP {response.status_code}: {response.text[:3000]}"
            )
        payload: dict[str, Any] = response.json()
        data = payload.get("data") or []
        image_url = str(data[0].get("url") or "") if data and isinstance(data[0], dict) else ""
        if not image_url.startswith(("http://", "https://")):
            raise SystemExit(f"Seedream response did not contain an image URL: {payload}")
        image_response = client.get(image_url)
        image_response.raise_for_status()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    extension = _extension(image_response.headers.get("content-type", ""))
    image_path = args.output_dir / f"seedream-5-lite-niulai-web-search-{timestamp}{extension}"
    report_path = args.output_dir / f"seedream-5-lite-niulai-web-search-{timestamp}.json"
    image_path.write_bytes(image_response.content)

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    tool_usage = usage.get("tool_usage") if isinstance(usage.get("tool_usage"), dict) else {}
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "request": request_payload,
        "response": {
            "model": payload.get("model", args.model),
            "created": payload.get("created"),
            "usage": usage,
            "web_search_count": tool_usage.get("web_search"),
            "image_content_type": image_response.headers.get("content-type"),
            "image_bytes": len(image_response.content),
        },
        "image_path": str(image_path),
        "note": "The temporary provider image URL is intentionally not persisted.",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {
        "image_path": str(image_path),
        "report_path": str(report_path),
        **report["response"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _extension(content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    return {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(normalized, ".png")


if __name__ == "__main__":
    raise SystemExit(main())
