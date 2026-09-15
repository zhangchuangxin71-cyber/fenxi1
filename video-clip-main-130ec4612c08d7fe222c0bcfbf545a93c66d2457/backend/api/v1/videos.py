"""v1 视频资源：异步导入、查询、任务列表、预览、删除。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request

from backend.api.v1.response import (
    ImportSubmittedResponse,
    JobListResponse,
    OkEnvelope,
    VideoResponse,
    ok,
)
from backend.api.v1.schemas import OkResponse, OssImportRequest
from backend.api.v1.urls import public_base_url
from backend.services import job_service, video_service

router = APIRouter(prefix="/api/v1", tags=["源片与视频"])

VideoIdPath = Annotated[
    str,
    Path(
        description="视频主键（标准 UUID），等于 Video 响应字段 `video_id`",
        examples=["516436ac-ac25-46cf-b19e-c92e1ae0e13a"],
    ),
]


@router.post(
    "/videos/import",
    response_model=ImportSubmittedResponse,
    summary="提交源片导入（异步）",
    description=(
        "传入 **oss_key**（配置桶）或 **url**（SSRF 校验后流式转存受管 OSS）二选一。"
        "立刻返回瘦身 `{video_id, status=importing, oss_key?, source_url?}`；随后 "
        "`GET /videos/{video_id}` 轮询至 `status=ready`（或 `failed`）。"
        "主键为标准 UUID。"
    ),
)
async def import_video(body: OssImportRequest):
    submitted = await video_service.submit_import(
        oss_key=body.oss_key,
        oss_url=body.oss_url,
    )
    return ok(submitted)


@router.get(
    "/videos/{video_id}",
    response_model=VideoResponse,
    summary="获取视频信息（轮询导入状态）",
    description=(
        "路径参数 **`video_id`** = 响应字段 **`video_id`**。"
        "导入中：`status=importing`；完成后：`status=ready` 且含 waveform / preview_url。"
    ),
)
async def get_video(video_id: VideoIdPath, http_request: Request):
    video = await video_service.build_video(
        video_id, base=public_base_url(http_request)
    )
    return ok(video)


@router.get(
    "/videos/{video_id}/jobs",
    response_model=JobListResponse,
    summary="列出该视频的切片任务",
    description=(
        "按 **`video_id`** 查其下全部 Job（手动 + 自动，新→旧）。"
        "单条形状与 `GET /jobs/{job_id}` 相同（含 segments）。"
    ),
)
async def list_video_jobs(video_id: VideoIdPath, http_request: Request):
    jobs = await job_service.list_jobs_by_video(
        video_id, base=public_base_url(http_request)
    )
    return ok(jobs)


@router.get(
    "/videos/{video_id}/media",
    summary="源片预览媒体（二进制）",
    include_in_schema=False,
    responses={200: {"content": {"video/mp4": {}}}},
)
async def stream_video_media(video_id: VideoIdPath):
    return await video_service.stream_media(video_id)


@router.get(
    "/videos/{video_id}/filmstrip",
    summary="时间轴胶片条（媒体资源）",
    include_in_schema=False,
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def get_filmstrip(video_id: VideoIdPath):
    return await video_service.get_filmstrip_jpeg(video_id)


@router.delete(
    "/videos/{video_id}",
    response_model=OkEnvelope,
    summary="删除视频",
    description="删除视频记录及关联本地产物。",
)
async def delete_video(video_id: VideoIdPath):
    await video_service.delete_video(video_id)
    return ok(OkResponse(ok=True))
