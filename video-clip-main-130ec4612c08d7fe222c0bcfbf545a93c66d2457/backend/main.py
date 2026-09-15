from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.errors import RequestIdMiddleware, register_exception_handlers
from backend.api.v1 import jobs as v1_jobs
from backend.api.v1 import segments as v1_segments
from backend.api.v1 import system as v1_system
from backend.api.v1 import videos as v1_videos
from backend.api.v1.response import ServiceInfoResponse, ok
from backend.api.v1.schemas import ServiceInfo
from backend.config import APP_VERSION, BASE_DIR, CLEANUP_INTERVAL_MINUTES, CORS_ORIGINS
from backend.storage.file_manager import ensure_dirs
from backend.storage.workspace import run_periodic_cleanup, run_startup_cleanup
from backend.workers.inline_scheduler import scheduler

logger = logging.getLogger(__name__)

_DOCS_DIR = BASE_DIR / "docs"

_OPENAPI_TAGS = [
    {
        "name": "系统",
        "description": "健康检查与服务信息",
    },
    {
        "name": "源片与视频",
        "description": "异步导入源片、视频信息，以及该视频下的 Job 列表",
    },
    {
        "name": "手动切片",
        "description": (
            "`POST /jobs/manual`（建档并切割）→ 轮询 → publish；"
            "重切请再次 `POST /jobs/manual` 建新 Job"
        ),
    },
    {
        "name": "自动分镜",
        "description": (
            "`POST /jobs/auto`（建档并检测）→ preview → `POST .../cut` → 轮询 → publish"
        ),
    },
]


async def _cleanup_loop(stop: asyncio.Event) -> None:
    interval = CLEANUP_INTERVAL_MINUTES
    if interval <= 0:
        await stop.wait()
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval * 60)
            break
        except asyncio.TimeoutError:
            try:
                await asyncio.to_thread(run_periodic_cleanup)
            except Exception:
                logger.exception("periodic cleanup failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    scheduler.start()
    try:
        run_startup_cleanup()
    except Exception:
        logger.exception("startup cleanup failed")

    stop = asyncio.Event()
    cleanup_task = asyncio.create_task(_cleanup_loop(stop))
    try:
        yield
    finally:
        scheduler.shutdown()
        stop.set()
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="视频切片服务 API",
    description=(
        "视频手动切片与自动分镜切片服务。\n\n"
        "- **唯一接口**：`/api/v1/*`\n"
        "- **响应格式**：`{code, message, data}`（二进制媒体除外）\n"
        "- **主键**：`video_id` / `job_id` / `segment_id` 均为标准 UUID\n"
        "- **主路径**：异步 import（轮询 Video 至 ready）→ "
        "`POST /jobs/manual` 或 `POST /jobs/auto`"
        " →（自动再 `/cut`）→ 轮询 → publish\n"
        "- **运行模型**：单 API 进程内存账本 + 同进程后台任务；"
        "**不跨重启恢复**（重启后旧 ID 失效）\n"
        "- **映射说明**：[/docs/static/api-mapping.md](/docs/static/api-mapping.md)\n"
        "- 无鉴权：勿对公网裸暴露"
    ),
    version=APP_VERSION,
    lifespan=lifespan,
    openapi_tags=_OPENAPI_TAGS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestIdMiddleware)

register_exception_handlers(app)

app.include_router(v1_system.router)
app.include_router(v1_videos.router)
app.include_router(v1_jobs.router)
app.include_router(v1_segments.router)

if _DOCS_DIR.is_dir():
    app.mount("/docs/static", StaticFiles(directory=_DOCS_DIR), name="project-docs")


@app.get(
    "/",
    response_model=ServiceInfoResponse,
    tags=["系统"],
    summary="服务信息",
)
async def root():
    return ok(
        ServiceInfo(
            name="视频切片服务 API",
            version=APP_VERSION,
            docs="/docs",
            api_prefix="/api/v1",
        )
    )
