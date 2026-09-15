"""v1 片段媒体（二进制，不包 envelope）。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request

from backend.services import segment_service

router = APIRouter(prefix="/api/v1/segments")

SegmentIdPath = Annotated[
    str,
    Path(
        description="片段主键（标准 UUID），等于 Segment 响应字段 `segment_id`",
        examples=["516436ac-ac25-46cf-b19e-c92e1ae0e13a"],
    ),
]


@router.get(
    "/{segment_id}/preview",
    summary="预览片段视频",
    include_in_schema=False,
    responses={200: {"content": {"video/mp4": {}}}, 206: {"description": "分段内容"}},
)
async def preview_segment(segment_id: SegmentIdPath, request: Request):
    return await segment_service.preview(segment_id, request)


@router.get(
    "/{segment_id}/thumb",
    summary="获取片段封面",
    include_in_schema=False,
    responses={200: {"content": {"image/jpeg": {}}}, 302: {"description": "跳转签名 URL"}},
)
async def segment_thumb(segment_id: SegmentIdPath):
    return await segment_service.thumb(segment_id)


@router.get(
    "/{segment_id}/download",
    summary="下载片段文件",
    include_in_schema=False,
    responses={200: {"content": {"video/mp4": {}}}, 302: {"description": "跳转签名 URL"}},
)
async def download_segment(segment_id: SegmentIdPath):
    return await segment_service.download(segment_id)
