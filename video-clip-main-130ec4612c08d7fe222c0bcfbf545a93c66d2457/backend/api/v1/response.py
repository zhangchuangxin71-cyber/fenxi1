"""统一成功响应 envelope：{code: 0, message: "ok", data: ...}。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from backend.api.v1.schemas import (
    HealthData,
    ImportSubmitted,
    Job,
    JobSubmitted,
    OkResponse,
    PublishResponse,
    ServiceInfo,
    Video,
)

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """成功响应包装。"""

    code: int = Field(default=0, description="0 表示成功")
    message: str = Field(default="ok", description="固定 ok")
    data: T = Field(description="业务数据")


class VideoResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: Video


class ImportSubmittedResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: ImportSubmitted


class JobResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: Job


class JobListResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: list[Job]


class JobSubmittedResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: JobSubmitted


class PublishEnvelope(BaseModel):
    code: int = 0
    message: str = "ok"
    data: PublishResponse


class OkEnvelope(BaseModel):
    code: int = 0
    message: str = "ok"
    data: OkResponse


class HealthResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: HealthData


class ServiceInfoResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: ServiceInfo


def ok(data: Any) -> dict[str, Any]:
    """构造统一成功 envelope dict（配合 response_model）。"""
    if hasattr(data, "model_dump"):
        payload = data.model_dump(mode="json")
    elif isinstance(data, list):
        payload = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in data
        ]
    else:
        payload = data
    return {"code": 0, "message": "ok", "data": payload}
