#!/usr/bin/env python3
"""Download the five service OpenAPI documents and archive them as YAML."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

import yaml


DEFAULT_URLS = {
    "mineru": "http://127.0.0.1:8135",
    "ingestion": "http://127.0.0.1:8100",
    "report-agent": "http://127.0.0.1:8115",
    "retrieval": "http://127.0.0.1:8120",
    "knowledge-chat": "http://127.0.0.1:8130",
}


def parse_service_url(raw: str) -> tuple[str, str]:
    name, separator, url = raw.partition("=")
    if not separator or name not in DEFAULT_URLS or not url.strip():
        valid = ", ".join(DEFAULT_URLS)
        raise argparse.ArgumentTypeError(f"expected SERVICE=URL; SERVICE must be one of: {valid}")
    return name, url.rstrip("/")


def fetch_schema(base_url: str, timeout: float) -> dict:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/openapi.json",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        document = json.load(response)
    if not isinstance(document, dict) or not document.get("openapi") or not document.get("paths"):
        raise ValueError(f"{base_url} returned an invalid OpenAPI document")
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--service-url",
        action="append",
        default=[],
        type=parse_service_url,
        metavar="SERVICE=URL",
        help="override a service base URL; may be repeated",
    )
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "openapi")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    urls = dict(DEFAULT_URLS)
    urls.update(args.service_url)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    for name, base_url in urls.items():
        document = fetch_schema(base_url, args.timeout)
        output = args.output_dir / f"{name}.openapi.yaml"
        output.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )
        print(f"archived {name}: {len(document['paths'])} paths -> {output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
