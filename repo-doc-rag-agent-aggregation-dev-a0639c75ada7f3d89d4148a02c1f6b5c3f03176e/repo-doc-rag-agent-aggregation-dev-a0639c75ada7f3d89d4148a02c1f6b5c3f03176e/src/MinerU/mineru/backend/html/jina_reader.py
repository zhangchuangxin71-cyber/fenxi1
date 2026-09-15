# Copyright (c) Opendatalab. All rights reserved.
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_JINA_READER_BASE_URL = "https://r.jina.ai/"


class JinaReaderError(RuntimeError):
    pass


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _load_env_files() -> None:
    current_file = Path(__file__).resolve()
    candidate_roots = [
        Path.cwd(),
        current_file.parents[3],
        current_file.parents[4],
    ]
    for env_path in dict.fromkeys(root / ".env" for root in candidate_roots):
        _load_env_file(env_path)


_load_env_files()


def get_jina_api_key() -> str:
    return os.environ.get("JINA_API_KEY", "")


def get_jina_reader_base_url() -> str:
    return os.environ.get("JINA_READER_BASE_URL", DEFAULT_JINA_READER_BASE_URL)


def build_reader_url(target_url: str) -> str:
    return get_jina_reader_base_url().rstrip("/") + "/" + target_url


def _open_with_default_or_direct(request: urllib.request.Request, timeout: int):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.URLError:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=timeout)


def call_jina_reader(
    target_url: str,
    timeout: int = 60,
    no_cache: bool = False,
    remove_images: bool = False,
    use_api_key: bool = True,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "mineru-jina-html-reader/0.1",
        "X-Timeout": str(timeout),
    }
    if no_cache:
        headers["X-No-Cache"] = "true"
    if remove_images:
        headers["X-No-Image"] = "true"
    if use_api_key and get_jina_api_key():
        headers["Authorization"] = f"Bearer {get_jina_api_key()}"

    reader_url = build_reader_url(target_url)
    request = urllib.request.Request(reader_url, headers=headers, method="GET")

    started = time.perf_counter()
    try:
        response = _open_with_default_or_direct(request, timeout=timeout + 30)
        with response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw_body = response.read().decode(charset, errors="replace")
            status_code = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise JinaReaderError(f"Jina Reader HTTP {exc.code}: {body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise JinaReaderError(f"Jina Reader request failed: {exc}") from exc

    elapsed = time.perf_counter() - started
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise JinaReaderError(f"Jina Reader did not return JSON: {raw_body[:1000]}") from exc

    data = payload.get("data")
    if not isinstance(data, dict):
        raise JinaReaderError(f"Jina Reader response missing data object: {payload}")

    content = data.get("content")
    if not isinstance(content, str):
        raise JinaReaderError(f"Jina Reader response missing data.content: {payload}")

    return {
        "url": target_url,
        "reader_url": reader_url,
        "title": data.get("title"),
        "description": data.get("description"),
        "markdown": content,
        "jina_json": payload,
        "usage": data.get("usage") or payload.get("meta", {}).get("usage"),
        "http_status": data.get("httpStatus") or status_code,
        "elapsed_seconds": round(elapsed, 3),
    }

