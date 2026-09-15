from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.platform.settings import Settings
from dev.config import load_dev_panel_config
from dev.dataset import generate_request

STATIC_DIR = Path(__file__).parent / "static"
# DEFAULT_MANIFEST_DIR = Path("/data/yongren.cai/rag-offline-eval/rag-eval-dataset/manifests")
DEFAULT_MANIFEST_DIR = Path("/workspace/rag-offline-eval/rag-eval-dataset/manifests")


class GenerateRequestInput(BaseModel):
    query_id: str | None = None
    user_id: str = Field(min_length=1)
    kb_id: str = Field(min_length=1)
    noise_count: int = Field(default=3, ge=0, le=99)
    top_k: int = Field(default=5, ge=1, le=20)
    max_return_tokens: int = 32768
    seed: int = 17
    manifest_dir: str | None = None


def load_runtime_api_key() -> str:
    return Settings().default_inbound_api_key


def create_dev_app(
    *,
    chat_base_url: str,
    api_key: str,
    manifest_dir: Path,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    owns_client = http_client is None
    upstream = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_client:
            await upstream.aclose()

    app = FastAPI(title="rag-knowledge-chat dev panel", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {
            "chat_base_url": chat_base_url,
            "manifest_dir": str(manifest_dir),
            "api_key_configured": bool(api_key),
        }

    @app.post("/api/generate-request")
    async def generate(body: GenerateRequestInput):
        selected_dir = Path(body.manifest_dir) if body.manifest_dir else manifest_dir
        try:
            return generate_request(
                manifest_dir=selected_dir,
                query_id=body.query_id,
                user_id=body.user_id,
                kb_id=body.kb_id,
                noise_count=body.noise_count,
                seed=body.seed,
                top_k=body.top_k,
                max_return_tokens=body.max_return_tokens,
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.post("/api/chat")
    async def proxy_chat(request: Request):
        payload = await request.json()
        headers = {}
        if api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        upstream_request = upstream.build_request(
            "POST",
            f"{chat_base_url.rstrip('/')}/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        try:
            response = await upstream.send(upstream_request, stream=True)
        except httpx.RequestError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "error": "deployed knowledge-chat service is unavailable",
                    "type": type(exc).__name__,
                },
            )
        if response.status_code != 200:
            body = await response.aread()
            await response.aclose()
            return Response(
                content=body,
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/json"),
            )

        async def stream():
            async for chunk in response.aiter_bytes():
                yield chunk

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            background=BackgroundTask(response.aclose),
        )

    return app


panel_config = load_dev_panel_config()
app = create_dev_app(
    chat_base_url=panel_config.chat_base_url,
    api_key=panel_config.api_key,
    manifest_dir=Path(os.getenv("DEV_DATASET_MANIFEST_DIR", str(DEFAULT_MANIFEST_DIR))),
)
