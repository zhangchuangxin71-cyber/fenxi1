"""v1 切片任务：手动 / 自动 / 轮询 / 发布。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request

from backend.api.v1.response import (
    JobResponse,
    JobSubmittedResponse,
    PublishEnvelope,
    ok,
)
from backend.api.v1.schemas import (
    CreateAutoJobRequest,
    CreateManualJobRequest,
    CutJobRequest,
    PublishRequest,
)
from backend.api.v1.urls import public_base_url
from backend.services import job_service, video_service

router = APIRouter(prefix="/api/v1/jobs")

TAG_MANUAL = "手动切片"
TAG_AUTO = "自动分镜"
TAGS_BOTH = [TAG_MANUAL, TAG_AUTO]

JobIdPath = Annotated[
    str,
    Path(
        description="任务主键（标准 UUID），等于 Job 响应字段 `job_id`",
        examples=["516436ac-ac25-46cf-b19e-c92e1ae0e13a"],
    ),
]


@router.post(
    "/manual",
    response_model=JobSubmittedResponse,
    tags=[TAG_MANUAL],
    summary="创建并切割（手动）",
    description=(
        "须先 `POST /videos/import` 并轮询至 `ready`。body：`video_id` + `segments`（必填）。\n"
        "返回瘦身 `{job_id, video_id, mode, status}`；随后 `GET /jobs/{job_id}` 轮询至 `done`。\n"
        "重切：再次调用本接口建新 Job（不使用 `/cut`）。"
    ),
)
async def create_manual_job(body: CreateManualJobRequest):
    video_service.require_ready(body.video_id)
    submitted = await job_service.create_manual(
        video_id=body.video_id,
        segments=body.segments,
    )
    return ok(submitted)


@router.post(
    "/auto",
    response_model=JobSubmittedResponse,
    tags=[TAG_AUTO],
    summary="创建并检测（自动）",
    description=(
        "须先 import 至 `ready`。建档并**立即入队分镜检测**，完成后停在 `preview`。\n"
        "返回瘦身提交结果；下一步：轮询至 preview → `POST /jobs/{job_id}/cut`。\n"
        "检测失败请再次调用本接口建新 Job。"
    ),
)
async def create_auto_job(body: CreateAutoJobRequest):
    video_service.require_ready(body.video_id)
    submitted = await job_service.create_auto(
        video_id=body.video_id,
        detector=body.detector,
        threshold=body.threshold,
        min_scene_len=body.min_scene_len,
        sample_fps=body.sample_fps,
    )
    return ok(submitted)


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    tags=TAGS_BOTH,
    summary="查询任务状态",
    description=(
        "轮询进度与 `segments`。"
        "`status=done` 时带 `publish_action`（POST）。"
    ),
)
async def get_job(job_id: JobIdPath, http_request: Request):
    job = await job_service.get_job(job_id, base=public_base_url(http_request))
    return ok(job)


@router.post(
    "/{job_id}/cut",
    response_model=JobSubmittedResponse,
    tags=[TAG_AUTO],
    summary="启动切割（仅自动）",
    description=(
        "仅 **自动** Job：须已 `preview`；`segments` 可省略（用当前分镜）。\n"
        "失败或完成后可再 cut 重切。\n"
        f"**{TAG_MANUAL}** 不支持本接口：请再次 `POST /jobs/manual` 建新 Job。"
    ),
)
async def cut_job(
    job_id: JobIdPath,
    body: CutJobRequest | None = None,
):
    body = body or CutJobRequest()
    submitted = await job_service.start_cut(job_id, segments=body.segments)
    return ok(submitted)


@router.post(
    "/{job_id}/publish",
    response_model=PublishEnvelope,
    tags=TAGS_BOTH,
    summary="返回切片 OSS 对象列表",
    description=(
        "不执行媒资库复制或保存；segment_ids 省略或空=全部切片，"
        "返回 all / selected 两组 oss_key / oss_url 对象列表。"
    ),
)
async def publish_job(job_id: JobIdPath, body: PublishRequest | None = None):
    body = body or PublishRequest()
    result = await job_service.publish(
        job_id,
        segment_ids=body.segment_ids,
        oss_key=body.oss_key,
    )
    return ok(result)
