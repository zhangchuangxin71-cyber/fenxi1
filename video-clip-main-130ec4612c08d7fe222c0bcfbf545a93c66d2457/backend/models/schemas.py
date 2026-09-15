"""内部领域模型（Worker / DB 状态机）；HTTP 契约见 backend.api.v1.schemas。"""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskMode(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"


class TaskStatus(str, Enum):
    PENDING = "pending"
    DETECTING = "detecting"
    PREVIEW = "preview"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class SegmentSource(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"


class SegmentInput(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    start_frame: Optional[int] = Field(default=None, ge=0)
    end_frame: Optional[int] = Field(default=None, ge=0)
    summary: Optional[str] = None
