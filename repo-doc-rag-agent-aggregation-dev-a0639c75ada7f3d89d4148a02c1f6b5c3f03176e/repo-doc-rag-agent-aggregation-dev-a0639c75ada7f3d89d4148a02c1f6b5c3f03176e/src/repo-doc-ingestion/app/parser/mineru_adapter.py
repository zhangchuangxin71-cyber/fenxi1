from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Mapping

import httpx

from .conversion_core.exceptions import ParseError, TimeoutError
from .weak_heading_splitter import split_long_leaf_nodes


LOGGER = logging.getLogger(__name__)
_CLIENT_SEMAPHORE: asyncio.Semaphore | None = None
_CLIENT_SEMAPHORE_LIMIT: int | None = None

SUPPORTED_MINERU_DOC_TYPES = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "docx",
    ".md": "md",
    ".markdown": "markdown",
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".csv": "xlsx",
    ".txt": "txt",
    ".pptx": "pptx",
    ".ppt": "pptx",
    ".png": "pdf",
    ".jpg": "pdf",
    ".jpeg": "pdf",
    ".bmp": "pdf",
    ".webp": "pdf",
    ".tiff": "pdf",
    ".gif": "pdf",
    ".jp2": "pdf",
}


@dataclass(frozen=True)
class MinerUClientConfig:
    api_url: str
    timeout_seconds: float = 1800.0
    retry_times: int = 2
    retry_backoff_base: float = 1.5
    poll_interval_seconds: float = 2.0
    backend: str = "pipeline"
    parse_method: str = "auto"
    lang: str = "ch"
    use_async_tasks: bool = True


class MinerUClient:
    """Small HTTP client for the MinerU FastAPI service."""

    def __init__(self, config: MinerUClientConfig) -> None:
        self.config = config
        self.base_url = config.api_url.rstrip("/")

    async def parse_file(self, file_path: str | Path) -> Mapping[str, Any]:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        if self.config.use_async_tasks:
            return await self._parse_file_async_task(path)
        return await self._parse_file_sync(path)

    async def _post_file(self, endpoint: str, path: Path) -> httpx.Response:
        timeout = httpx.Timeout(self.config.timeout_seconds, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            with path.open("rb") as f:
                files = [("files", (path.name, f, "application/octet-stream"))]
                data = {
                    "backend": self.config.backend,
                    "parse_method": self.config.parse_method,
                    "lang_list": self.config.lang,
                    "formula_enable": "true",
                    "table_enable": "true",
                    "return_md": "true",
                    "return_middle_json": "true",
                    "return_model_output": "false",
                    "return_content_list": "true",
                    "return_images": "false",
                    "response_format_zip": "false",
                }
                return await client.post(f"{self.base_url}{endpoint}", data=data, files=files)

    async def _parse_file_sync(self, path: Path) -> Mapping[str, Any]:
        response = await self._with_retries(lambda: self._post_file("/file_parse", path))
        if response.status_code >= 400:
            raise ParseError(
                f"MinerU sync parse failed: status={response.status_code} body={response.text[:500]}",
                stage="mineru_sync_parse",
            )
        return response.json()

    async def _parse_file_async_task(self, path: Path) -> Mapping[str, Any]:
        submit_response = await self._with_retries(lambda: self._post_file("/tasks", path))
        if submit_response.status_code >= 400:
            raise ParseError(
                f"MinerU task submission failed: status={submit_response.status_code} body={submit_response.text[:500]}",
                stage="mineru_submit",
            )
        payload = submit_response.json()
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            raise ParseError("MinerU task submission response did not include task_id", stage="mineru_submit")
        return await self._wait_task_result(task_id)

    async def _wait_task_result(self, task_id: str) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.config.timeout_seconds
        timeout = httpx.Timeout(30.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"MinerU task timed out: {task_id}", stage="mineru_task_timeout")
                status_response = await client.get(f"{self.base_url}/tasks/{task_id}")
                if status_response.status_code >= 500:
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue
                if status_response.status_code >= 400:
                    raise ParseError(
                        f"MinerU task status failed: status={status_response.status_code} body={status_response.text[:500]}",
                        stage="mineru_task_status",
                    )
                status_payload = status_response.json()
                status = str(status_payload.get("status") or "").lower()
                if status == "completed":
                    result_response = await client.get(f"{self.base_url}/tasks/{task_id}/result")
                    if result_response.status_code >= 400:
                        raise ParseError(
                            f"MinerU task result failed: status={result_response.status_code} body={result_response.text[:500]}",
                            stage="mineru_task_result",
                        )
                    return result_response.json()
                if status == "failed":
                    raise ParseError(
                        f"MinerU task failed: {status_payload.get('error') or status_payload}",
                        stage="mineru_task_failed",
                    )
                await asyncio.sleep(self.config.poll_interval_seconds)

    async def _with_retries(self, fn: Any) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(max(1, self.config.retry_times + 1)):
            try:
                response = await fn()
                if response.status_code < 500:
                    return response
                last_exc = ParseError(
                    f"MinerU transient response: status={response.status_code}",
                    stage="mineru_http",
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
            if attempt < self.config.retry_times:
                await asyncio.sleep(self.config.retry_backoff_base ** attempt)
        raise ParseError(f"MinerU request failed after retries: {last_exc}", stage="mineru_http")


def infer_mineru_doc_type(source_path: str | Path) -> str:
    suffix = Path(source_path).suffix.lower()
    return SUPPORTED_MINERU_DOC_TYPES.get(suffix, suffix.lstrip(".") or "txt")


def extract_single_result(response_payload: Mapping[str, Any], source_path: str | Path) -> Mapping[str, Any]:
    results = response_payload.get("results")
    if not isinstance(results, Mapping) or not results:
        raise ParseError("MinerU response did not include results", stage="mineru_response")
    stem = Path(source_path).stem
    if stem in results and isinstance(results[stem], Mapping):
        return results[stem]
    for value in results.values():
        if isinstance(value, Mapping):
            return value
    raise ParseError("MinerU response results did not include a parse result object", stage="mineru_response")


async def parse_document_with_mineru_async(
    file_path: str | Path,
    *,
    api_url: str,
    timeout_seconds: float = 1800.0,
    retry_times: int = 2,
    retry_backoff_base: float = 1.5,
    poll_interval_seconds: float = 2.0,
    backend: str = "pipeline",
    parse_method: str = "auto",
    lang: str = "ch",
    use_async_tasks: bool = True,
    client_concurrency: int | None = None,
    weak_heading_split_enabled: bool = False,
    weak_heading_split_min_chars: int = 800,
    weak_heading_split_max_chars: int = 8000,
) -> dict[str, Any]:
    config = MinerUClientConfig(
        api_url=api_url,
        timeout_seconds=timeout_seconds,
        retry_times=retry_times,
        retry_backoff_base=retry_backoff_base,
        poll_interval_seconds=poll_interval_seconds,
        backend=backend,
        parse_method=parse_method,
        lang=lang,
        use_async_tasks=use_async_tasks,
    )
    client = MinerUClient(config)
    semaphore = _get_client_semaphore(client_concurrency)
    async with semaphore:
        response_payload = await client.parse_file(file_path)
    result = extract_single_result(response_payload, file_path)
    return mineru_result_to_legacy_payload(
        source_path=file_path,
        doc_type=infer_mineru_doc_type(file_path),
        md_content=str(result.get("md_content") or ""),
        content_list=_parse_json_field(result.get("content_list"), default=[]),
        middle_json=_parse_json_field(result.get("middle_json"), default={}),
        weak_heading_split_enabled=weak_heading_split_enabled,
        weak_heading_split_min_chars=weak_heading_split_min_chars,
        weak_heading_split_max_chars=weak_heading_split_max_chars,
    )


def parse_document_with_mineru(file_path: str | Path, **kwargs: Any) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(parse_document_with_mineru_async(file_path, **kwargs))
    raise RuntimeError("parse_document_with_mineru() cannot be called inside a running event loop")


def _get_client_semaphore(limit: int | None) -> asyncio.Semaphore:
    global _CLIENT_SEMAPHORE, _CLIENT_SEMAPHORE_LIMIT
    if limit is None:
        raw_limit = os.getenv("MINERU_CLIENT_CONCURRENCY", "8")
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 8
    limit = max(1, int(limit))
    if _CLIENT_SEMAPHORE is None or _CLIENT_SEMAPHORE_LIMIT != limit:
        _CLIENT_SEMAPHORE = asyncio.Semaphore(limit)
        _CLIENT_SEMAPHORE_LIMIT = limit
    return _CLIENT_SEMAPHORE


def mineru_result_to_legacy_payload(
    *,
    source_path: str | Path,
    doc_type: str,
    md_content: str,
    content_list: Any,
    middle_json: Any | None = None,
    weak_heading_split_enabled: bool = False,
    weak_heading_split_min_chars: int = 800,
    weak_heading_split_max_chars: int = 8000,
) -> dict[str, Any]:
    path = Path(source_path)
    blocks = content_list if isinstance(content_list, list) else []
    pages = _build_pages(blocks, md_content)
    structure = _build_structure(blocks, pages, md_content)
    if not structure:
        fallback_text = "\n\n".join(str(page.get("content") or "") for page in pages).strip() or md_content.strip()
        structure = [
            {
                "title": path.stem or "全文",
                "node_id": "0000",
                "level": 1,
                "start_index": 1,
                "end_index": max(1, len(pages)),
                "text": fallback_text,
                "summary": "",
            }
        ]
    structure = split_long_leaf_nodes(
        structure,
        doc_type=doc_type,
        enabled=weak_heading_split_enabled,
        min_chars=weak_heading_split_min_chars,
        max_chars=weak_heading_split_max_chars,
    )
    _renumber_nodes(structure)
    return {
        "id": "",
        "type": doc_type,
        "path": str(path.resolve()),
        "doc_name": path.name,
        "doc_description": "",
        "page_count": len(pages),
        "structure": structure,
        "pages": pages,
        "raw_mineru": {
            "md_content": md_content,
            "middle_json": middle_json or {},
            "content_list": blocks,
        },
    }


def _parse_json_field(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            LOGGER.warning("Failed to decode MinerU JSON field, using default")
    return default


def _build_pages(blocks: list[Any], md_content: str) -> list[dict[str, Any]]:
    page_texts: dict[int, list[str]] = {}
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        page_number = int(block.get("page_idx") or 0) + 1
        text = _block_to_text(block)
        if text:
            page_texts.setdefault(page_number, []).append(text)
    if not page_texts and md_content.strip():
        return [{"page": 1, "content": md_content.strip()}]
    return [
        {"page": page_number, "content": "\n\n".join(parts).strip()}
        for page_number, parts in sorted(page_texts.items())
        if "\n\n".join(parts).strip()
    ] or [{"page": 1, "content": ""}]


def _build_structure(blocks: list[Any], pages: list[dict[str, Any]], md_content: str = "") -> list[dict[str, Any]]:
    has_structured_headings = any(
        isinstance(block, Mapping) and _block_heading_title(block) for block in blocks
    )
    if not has_structured_headings:
        markdown_structure = _build_markdown_heading_structure(md_content, pages)
        if markdown_structure:
            return markdown_structure

    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        page_number = int(block.get("page_idx") or 0) + 1
        heading_level = _block_heading_level(block)
        text = _block_to_text(block)
        if heading_level is not None:
            title = _block_heading_title(block)
            if not title:
                LOGGER.debug(
                    "Skipping MinerU heading block without textual content",
                    extra={"page_number": page_number, "bbox": block.get("bbox")},
                )
                continue
            node = {
                "title": title[:200],
                "level": heading_level,
                "start_index": page_number,
                "end_index": page_number,
                "text": "",
                "summary": "",
            }
            while stack and int(stack[-1].get("level") or 1) >= heading_level:
                stack.pop()
            if stack:
                stack[-1].setdefault("nodes", []).append(node)
            else:
                roots.append(node)
            stack.append(node)
            current = node
            continue
        if not text:
            continue
        if current is None:
            current = {
                "title": _first_non_empty_line(text)[:200] or f"第 {page_number} 页",
                "level": 1,
                "start_index": page_number,
                "end_index": page_number,
                "text": "",
                "summary": "",
            }
            roots.append(current)
            stack = [current]
        _append_node_text(current, text)
        current["end_index"] = max(int(current.get("end_index") or page_number), page_number)
    if not roots:
        return []
    _refresh_ranges(roots)
    return roots


def _build_markdown_heading_structure(md_content: str, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not md_content.strip():
        return []
    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    page_end = max(1, len(pages))
    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    for raw_line in md_content.splitlines():
        line = raw_line.rstrip()
        match = heading_re.match(line.strip())
        if match:
            level = len(match.group(1))
            title = re.sub(r"\s+#+$", "", match.group(2)).strip()
            if not title:
                continue
            node = {
                "title": title[:200],
                "level": level,
                "start_index": 1,
                "end_index": page_end,
                "text": "",
                "summary": "",
            }
            while stack and int(stack[-1].get("level") or 1) >= level:
                stack.pop()
            if stack:
                stack[-1].setdefault("nodes", []).append(node)
            else:
                roots.append(node)
            stack.append(node)
            current = node
            continue
        if current is not None and line.strip():
            _append_node_text(current, line.strip())
    return roots


def _block_heading_level(block: Mapping[str, Any]) -> int | None:
    if block.get("type") == "text" and block.get("text_level"):
        try:
            return max(1, int(block.get("text_level") or 1))
        except (TypeError, ValueError):
            return 1
    if block.get("type") == "title":
        content = block.get("content")
        if isinstance(content, Mapping):
            try:
                return max(1, int(content.get("level") or 1))
            except (TypeError, ValueError):
                return 1
        return 1
    return None


def _block_heading_title(block: Mapping[str, Any]) -> str:
    if _block_heading_level(block) is None:
        return ""
    return (_block_title(block) or _first_non_empty_line(_block_to_text(block))).strip()


def _block_title(block: Mapping[str, Any]) -> str:
    if isinstance(block.get("text"), str):
        return str(block.get("text") or "").strip()
    content = block.get("content")
    if isinstance(content, Mapping):
        return _span_text(content.get("title_content")).strip()
    return ""


def _block_to_text(block: Mapping[str, Any]) -> str:
    block_type = str(block.get("type") or "").strip()
    if block_type == "text":
        return str(block.get("text") or "").strip()
    if block_type == "title":
        return _block_title(block)
    if block_type in {"paragraph", "list", "index"}:
        content = block.get("content")
        if isinstance(content, Mapping):
            if block_type == "paragraph":
                return _span_text(content.get("paragraph_content")).strip()
            items = content.get("list_items") or content.get("index_items")
            if isinstance(items, list):
                return "\n".join(f"- {_span_text(item).strip()}" for item in items if _span_text(item).strip())
        return str(block.get("text") or "").strip()
    if block_type == "table":
        caption = _join_texts(block.get("table_caption"))
        body = _html_table_to_markdown(str(block.get("table_body") or ""))
        footnote = _join_texts(block.get("table_footnote"))
        return "\n\n".join(part for part in [caption, body, footnote] if part).strip()
    if block_type in {"image", "chart"}:
        caption = _join_texts(block.get("image_caption") or block.get("chart_caption"))
        footnote = _join_texts(block.get("image_footnote") or block.get("chart_footnote"))
        label = "图片" if block_type == "image" else "图表"
        return "\n\n".join(part for part in [f"[{label}]", caption, footnote] if part).strip()
    if block_type == "equation":
        return str(block.get("text") or "").strip()
    if block_type == "code":
        return str(block.get("code_body") or block.get("text") or "").strip()
    return str(block.get("text") or "").strip()


def _append_node_text(node: dict[str, Any], text: str) -> None:
    existing = str(node.get("text") or "").strip()
    node["text"] = f"{existing}\n\n{text}".strip() if existing else text.strip()


def _refresh_ranges(nodes: Iterable[dict[str, Any]]) -> None:
    for node in nodes:
        children = node.get("nodes")
        if isinstance(children, list) and children:
            _refresh_ranges(children)
            child_starts = [int(child.get("start_index") or 0) for child in children if child.get("start_index")]
            child_ends = [int(child.get("end_index") or 0) for child in children if child.get("end_index")]
            if child_starts:
                node["start_index"] = min(int(node.get("start_index") or child_starts[0]), min(child_starts))
            if child_ends:
                node["end_index"] = max(int(node.get("end_index") or child_ends[0]), max(child_ends))


def _renumber_nodes(nodes: Iterable[dict[str, Any]], start: int = 0) -> int:
    current = start
    for node in nodes:
        node["node_id"] = f"{current:04d}"
        current += 1
        children = node.get("nodes")
        if isinstance(children, list):
            current = _renumber_nodes(children, current)
    return current


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _span_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if "content" in value:
            return str(value.get("content") or "")
        return " ".join(_span_text(item) for item in value.values())
    if isinstance(value, list):
        return "".join(_span_text(item) for item in value)
    return ""


def _join_texts(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return ""


def _html_table_to_markdown(html: str) -> str:
    text = unescape(html or "")
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.IGNORECASE | re.DOTALL)
    parsed_rows: list[list[str]] = []
    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.IGNORECASE | re.DOTALL)
        cleaned = [re.sub(r"<[^>]+>", "", cell).strip() for cell in cells]
        cleaned = [cell for cell in cleaned if cell]
        if cleaned:
            parsed_rows.append(cleaned)
    if not parsed_rows:
        return re.sub(r"<[^>]+>", " ", text).strip()
    width = max(len(row) for row in parsed_rows)
    normalized = [row + [""] * (width - len(row)) for row in parsed_rows]
    lines = ["| " + " | ".join(normalized[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)
