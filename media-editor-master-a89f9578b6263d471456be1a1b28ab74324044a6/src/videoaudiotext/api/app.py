"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Path

from videoaudiotext.api.api_logging import RequestLogMiddleware, configure_api_logging
from videoaudiotext.api.errors import ApiError
from videoaudiotext.api.exception_handlers import register_exception_handlers
from videoaudiotext.api.jobs import cancel_job, start_compose_job
from videoaudiotext.api.maintenance_auth import verify_maintenance_token
from videoaudiotext.api.openapi_zh import API_DESCRIPTION, OPENAPI_TAGS, apply_minimal_request_examples
from videoaudiotext.api.purge import PurgePolicy, purge_workspaces
from videoaudiotext.api.request_id import RequestIdMiddleware
from videoaudiotext.api.responses import error_content, ok
from videoaudiotext.api.schemas import (
    AudioProcessRequest,
    ComposeRunRequest,
    PurgeJobsRequest,
    SplitRequest,
    VisualPreviewRequest,
)
from videoaudiotext.api.pipeline_bootstrap import (
    bootstrap_confirmed_audio,
    bootstrap_split_plan,
    prepare_standalone_job,
)
from videoaudiotext.api.standalone_payload import standalone_compose_payload
from videoaudiotext.api.stage_jobs import (
    cancel_stage_job,
    start_audio_process_job,
    start_split_job,
    start_visual_preview_job,
)
from videoaudiotext.api.job_cancel import (
    assert_cancellable_or_cancelled,
    cancel_result,
    finish_cancel_if_already_terminal,
    resolve_task_for_cancel,
)
from videoaudiotext.api.job_context import (
    jobs_root,
    load_job_record,
    read_job_index,
)
from videoaudiotext.concurrency.coordination import enrich_job_queue_fields
from videoaudiotext.core.ffmpeg_util import check_ffmpeg

from videoaudiotext.api.task_ids import require_uuid_task_id

TaskId = Annotated[
    str,
    Path(
        description="Task ID（各阶段 /run 返回，标准 UUID）",
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    ),
]
STAGE_JOB_POLL_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("split", "split", "分句"),
    ("audio/process", "audio_process", "配音"),
    ("visual/preview", "visual_preview", "画面预览"),
    ("compose", "compose", "合成"),
)


def _require_job_kind(task_id: str, expected_kind: str) -> dict[str, Any]:
    task_id = require_uuid_task_id(task_id)
    job = load_job_record(task_id)
    if not job:
        raise ApiError(404, 40401, "task not found")
    index = read_job_index(task_id)
    kind = str((index or {}).get("kind") or job.get("kind") or "")
    if kind != expected_kind:
        raise ApiError(404, 40401, "task not found")
    return job


def _cancel_job_by_id(task_id: str) -> dict[str, Any]:
    task_id = require_uuid_task_id(task_id)
    index, job, store = resolve_task_for_cancel(task_id)
    if assert_cancellable_or_cancelled(job) == "cancelled":
        return cancel_result(task_id)
    if not store:
        raced = finish_cancel_if_already_terminal(task_id)
        if raced:
            return raced
        raise ApiError(404, 40401, "task not found")
    kind = index.get("kind")
    if kind == "compose":
        return cancel_job(store, task_id)
    prefix = str(index.get("prefix") or "")
    return cancel_stage_job(store, prefix, task_id)


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        from videoaudiotext.api.purge import purge_workspaces_on_startup
        from videoaudiotext.api.reconcile import reconcile_all_workspaces

        configure_api_logging()
        from videoaudiotext.concurrency.coordination import reset_global_coordination

        reconcile_all_workspaces(startup=True)
        reset_global_coordination(startup=True)
        purge_workspaces_on_startup()
        yield

    app = FastAPI(
        title="VideoAudioText 流程 B API",
        description=API_DESCRIPTION,
        version="3.1",
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    register_exception_handlers(app)

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse(url="/docs")

    @app.get(
        "/api/v1/health",
        tags=["系统"],
        summary="健康检查",
        description="探测 ffmpeg / ffprobe 是否可用。依赖不可用时返回 HTTP 503。",
    )
    def health():
        checks = {
            "ffmpeg": {"ok": False},
            "ffprobe": {"ok": False},
            "llm_split": {"ok": True},
        }
        try:
            check_ffmpeg()
            checks["ffmpeg"]["ok"] = True
            checks["ffprobe"]["ok"] = True
        except Exception:
            pass
        ok_flag = checks["ffmpeg"]["ok"] and checks["ffprobe"]["ok"]
        if not ok_flag:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=503,
                content=error_content(
                    code=50301,
                    message="health_check_failed",
                    data={"ok": False, "checks": checks},
                ),
            )
        return ok({"ok": True, "checks": checks})

    @app.post(
        "/api/v1/maintenance/purge-jobs",
        tags=["系统"],
        summary="清理过期 Job 目录",
        description=(
            "按 TTL 策略扫描 jobs/ 并删除过期目录。"
            "默认 dry_run=true 仅预览。"
            "仅当服务配置了 MAINTENANCE_API_TOKEN 时，才须在请求头传 X-Maintenance-Token。"
        ),
    )
    @app.post(
        "/api/v1/maintenance/purge-workspaces",
        tags=["系统"],
        summary="清理过期 Job 目录（兼容旧路径）",
        include_in_schema=False,
    )
    def maintenance_purge_jobs(
        body: PurgeJobsRequest,
        _: None = Depends(verify_maintenance_token),
    ):
        policy = PurgePolicy()
        if body.policy:
            p = body.policy
            if p.ttl_empty_days is not None:
                policy.ttl_empty_days = p.ttl_empty_days
            if p.ttl_abandoned_days is not None:
                policy.ttl_abandoned_days = p.ttl_abandoned_days
            if p.ttl_composed_days is not None:
                policy.ttl_composed_days = p.ttl_composed_days
            if p.grace_hours is not None:
                policy.grace_hours = p.grace_hours
        include_states = None if body.include_composed else frozenset({"empty", "in_progress"})
        data = purge_workspaces(
            dry_run=body.dry_run,
            max_delete=body.max_delete,
            policy=policy,
            include_states=include_states,
            include_all_evaluated=body.dry_run,
        )
        return ok(data)

    @app.get(
        "/api/v1/maintenance/coord-status",
        tags=["系统"],
        summary="查询全局 Job 协调状态",
        description=(
            "返回 GLOBAL_JOB_WORKERS、workspaces/_coord/global_jobs.json 内容，"
            "以及磁盘上 queued/running Job 统计；`desync=true` 表示计数与磁盘不一致。"
            "仅当配置了 MAINTENANCE_API_TOKEN 时，才须在请求头传 X-Maintenance-Token。"
        ),
    )
    def maintenance_coord_status(_: None = Depends(verify_maintenance_token)):
        from videoaudiotext.concurrency.coordination import get_coord_status

        return ok(get_coord_status())

    @app.post(
        "/api/v1/split/run",
        tags=["分句"],
        summary="提交分句 Task",
        description=(
            "异步 LLM/规则分句。返回 task_id；"
            "轮询 GET /api/v1/split/tasks/{task_id} 获取 segments、text_digest。"
            "本接口独立，不依赖其他 Task。"
        ),
    )
    def split_run(body: SplitRequest):
        store = prepare_standalone_job("split")
        data = start_split_job(
            store,
            text=body.text,
            include_ai_prompts=body.include_ai_prompts,
            global_style=body.global_style,
        )
        return ok(data)

    @app.post(
        "/api/v1/audio/process/run",
        tags=["配音"],
        summary="提交配音处理 Task",
        description=(
            "请求体自带 segments（index、text、audio.url）。"
            "完成后 master.wav 与 subtitle.srt 上传 OSS；"
            "轮询 GET /api/v1/audio/process/tasks/{task_id}。"
            "不依赖 split Task ID；下一步可选 preview，再将 OSS URL 传入 compose。"
        ),
    )
    def audio_process_run(body: AudioProcessRequest):
        segments = [
            {"index": s.index, "text": s.text, "audio": {"url": s.audio.url}}
            for s in body.segments
        ]
        store = prepare_standalone_job("audio_process")
        bootstrap_split_plan(store, segments)
        data = start_audio_process_job(
            store,
            segments,
            code=body.code,
            user_id=body.id,
            force_refresh=body.force_refresh,
            speed=body.speed,
        )
        return ok(data)

    @app.put(
        "/api/v1/audio/confirm",
        tags=["配音"],
        summary="确认配音交付物（已废弃）",
        description="v3.1 起各接口自包含 OSS URL，无需 confirm。",
        include_in_schema=False,
    )
    def audio_confirm_deprecated():
        raise ApiError(404, 40401, "endpoint removed; pass audio URLs in compose body")

    @app.get(
        "/api/v1/subtitle/fonts",
        tags=["画面预览"],
        summary="字幕字体下拉选项",
        description=(
            "供 preview / compose 的字体选择器使用：每项含 name（展示并传给 "
            "subtitle_style.font_name）、ass_font_name（解析后的 ASS 字体名，只读）。"
        ),
    )
    def list_subtitle_fonts():
        from videoaudiotext.config.subtitle import subtitle_font_picker_choices

        return ok({"choices": subtitle_font_picker_choices()})

    @app.post(
        "/api/v1/visual/preview/run",
        tags=["画面预览"],
        summary="提交字幕预览 Task（可选）",
        description=(
            "传入首句素材 URL、字幕文案、分辨率与字体样式，生成单帧预览图。"
            "轮询 GET /api/v1/visual/preview/tasks/{task_id}。"
        ),
    )
    def visual_preview_run(body: VisualPreviewRequest):
        store = prepare_standalone_job("visual_preview")
        data = start_visual_preview_job(
            store,
            code=body.code,
            user_id=body.id,
            resolution=body.resolution.model_dump(exclude_none=True),
            subtitle_style=body.subtitle_style.model_dump(),
            preview_input={
                "media_url": body.media_url,
                "media_type": body.media_type,
                "text": body.text,
                "start_sec": body.start_sec or 0.0,
            },
        )
        return ok(data)

    @app.post(
        "/api/v1/compose/run",
        tags=["合成"],
        summary="提交合成 Task",
        description=(
            "请求体：master_audio_url、subtitle_srt_url、segments（text + media）。"
            "口播时长与句间 gap 由 SRT 解析；可选 voice_volume、bgm（url + bgm_volume）。"
            "可选 subtitle_style / resolution。完成后返回 MP4、WAV、最终样式 ASS 的 OSS URL。"
        ),
    )
    def compose_run(body: ComposeRunRequest):
        store = prepare_standalone_job("compose")
        data = start_compose_job(
            store,
            code=body.code,
            user_id=body.id,
            subtitle_mode=body.subtitle_mode,
            reuse_intermediates=body.reuse_intermediates,
            cleanup_scratch_on_success=body.cleanup_scratch_on_success,
            standalone_pipeline=standalone_compose_payload(body),
        )
        return ok(data)

    for path_prefix, kind, tag in STAGE_JOB_POLL_ROUTES:
        stage_path = f"/api/v1/{path_prefix}/tasks/{{task_id}}"

        @app.get(
            stage_path,
            tags=[tag],
            summary="查询本阶段 Task 状态",
            description=(
                f"轮询 {path_prefix} 阶段 Task；status=done 时 result 含本阶段产物。"
                f"task_id 须属于本阶段，否则返回 404。"
            ),
        )
        def stage_task_status(task_id: TaskId, *, _kind: str = kind):
            job = _require_job_kind(task_id, _kind)
            return ok(enrich_job_queue_fields(job))

    @app.post(
        "/api/v1/tasks/{task_id}/cancel",
        tags=["Task"],
        summary="取消 Task",
    )
    def task_cancel(task_id: TaskId):
        return ok(_cancel_job_by_id(task_id))

    from fastapi.openapi.utils import get_openapi

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            description=app.description,
            routes=app.routes,
        )
        apply_minimal_request_examples(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi

    return app


def main() -> None:
    import os

    import uvicorn

    from videoaudiotext.concurrency.limits import uvicorn_workers

    jobs_root().mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("API_PORT", "8787"))
    workers = uvicorn_workers()
    if workers > 1:
        uvicorn.run(
            "videoaudiotext.api.app:create_app",
            factory=True,
            host="0.0.0.0",
            port=port,
            workers=workers,
        )
    else:
        uvicorn.run(create_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
