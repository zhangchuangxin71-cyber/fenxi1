from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl
from starlette.background import BackgroundTask

from app.config import DevPanelSettings

STATIC_DIR = Path(__file__).parent / "static"
CONFIG_FILE = Path(__file__).with_name("config.yaml")
DEFAULT_MANIFEST_FILE = Path("/workspace/wechat-article-agent/wechat-article-documents.manifest.jsonl")


class DevPanelDefaults(BaseModel):
    agent_base_url: HttpUrl
    user_id: str = Field(min_length=1)
    kb_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


def load_panel_defaults(path: Path = CONFIG_FILE) -> DevPanelDefaults:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"failed to load development panel config: {path}") from exc
    if not isinstance(raw, Mapping):
        raise RuntimeError(f"development panel config must be a YAML object: {path}")
    return DevPanelDefaults.model_validate(raw)


class RandomDocumentsRequest(BaseModel):
    manifest_file: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    kb_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    count: int = Field(default=3, ge=1, le=20)
    seed: int | None = None
    retrieval_base_url: HttpUrl


class ProxyRequest(BaseModel):
    agent_base_url: HttpUrl
    payload: dict[str, Any]


class CancelProxyRequest(BaseModel):
    agent_base_url: HttpUrl
    session_id: str = Field(min_length=1)


def load_manifest_documents(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"manifest file not found: {path}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"manifest row is not an object at {path}:{line_number}")
            if value.get("status") == "completed" and str(value.get("doc_id") or "").strip():
                rows.append(value)
    if not rows:
        raise ValueError(f"manifest has no completed documents: {path}")
    return rows


def create_dev_app(
    *,
    agent_base_url: str,
    retrieval_base_url: str,
    manifest_file: Path,
    user_id: str = "wechat-article-agent-dev-user",
    kb_id: str = "wechat-article-agent-dev-kb",
    session_id: str = "wechat-article-agent-dev-session",
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    owns_client = http_client is None
    upstream = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=None, write=60, pool=10),
        trust_env=False,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_client:
            await upstream.aclose()

    app = FastAPI(title="WeChat Article Agent development panel", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {
            "agent_base_url": agent_base_url,
            "retrieval_base_url": retrieval_base_url,
            "manifest_file": str(manifest_file),
            "user_id": user_id,
            "kb_id": kb_id,
            "session_id": session_id,
        }

    @app.post("/api/random-documents")
    async def random_documents(body: RandomDocumentsRequest) -> Response:
        try:
            rows = await asyncio.to_thread(load_manifest_documents, Path(body.manifest_file))
            eligible = [
                row
                for row in rows
                if str(row.get("user_id") or body.user_id) == body.user_id
                and str(row.get("kb_id") or body.kb_id) == body.kb_id
            ]
            if not eligible:
                raise ValueError("manifest has no documents for the selected user_id and kb_id")
            selected = random.Random(body.seed).sample(eligible, min(body.count, len(eligible)))
            doc_ids = [str(row["doc_id"]) for row in selected]
            response = await upstream.post(
                f"{str(body.retrieval_base_url).rstrip('/')}/rag/v1/documents/meta",
                json={
                    "user_id": body.user_id,
                    "kb_id": body.kb_id,
                    "session_id": body.session_id,
                    "doc_ids": doc_ids,
                    "temp_doc_ids": [],
                    "include_missing": True,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ValueError("retrieval metadata response is not an object")
            return JSONResponse(
                {
                    "doc_ids": doc_ids,
                    "documents": payload.get("documents", []),
                    "missing_doc_ids": payload.get("missing_doc_ids", []),
                    "manifest_rows": selected,
                }
            )
        except (ValueError, httpx.HTTPError) as exc:
            return JSONResponse(
                status_code=400 if isinstance(exc, ValueError) else 502,
                content={"error": str(exc), "type": type(exc).__name__},
            )

    @app.post("/api/responses")
    async def proxy_responses(body: ProxyRequest) -> Response:
        request = upstream.build_request(
            "POST",
            f"{str(body.agent_base_url).rstrip('/')}/v1/responses",
            json=body.payload,
        )
        try:
            response = await upstream.send(request, stream=True)
        except httpx.RequestError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": "agent service is unavailable", "type": type(exc).__name__},
            )
        if response.status_code != 200:
            content = await response.aread()
            await response.aclose()
            return Response(
                content=content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/json"),
            )

        async def stream() -> AsyncIterator[bytes]:
            async for chunk in response.aiter_bytes():
                yield chunk

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
            background=BackgroundTask(response.aclose),
        )

    @app.post("/api/responses/{response_id}/cancel")
    async def proxy_cancel(response_id: str, body: CancelProxyRequest) -> Response:
        try:
            response = await upstream.post(
                f"{str(body.agent_base_url).rstrip('/')}/v1/responses/{response_id}/cancel",
                json={"context": {"session_id": body.session_id}},
            )
        except httpx.RequestError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": "agent service is unavailable", "type": type(exc).__name__},
            )
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )

    return app


settings = DevPanelSettings()  # type: ignore[call-arg]
panel_defaults = load_panel_defaults()
app = create_dev_app(
    agent_base_url=str(panel_defaults.agent_base_url),
    retrieval_base_url=str(settings.dev_retrieval_base_url),
    manifest_file=Path(settings.dev_dataset_manifest_file),
    user_id=panel_defaults.user_id,
    kb_id=panel_defaults.kb_id,
    session_id=panel_defaults.session_id,
)
