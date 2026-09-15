"""API v1 Pydantic 模型（对外契约；不暴露本地路径）。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class JobMode(str, Enum):
    """任务模式"""

    MANUAL = "manual"
    AUTO = "auto"


class VideoStatus(str, Enum):
    """源片导入状态"""

    IMPORTING = "importing"
    READY = "ready"
    FAILED = "failed"


class JobStatus(str, Enum):
    """任务状态"""

    PENDING = "pending"
    DETECTING = "detecting"
    PREVIEW = "preview"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class SegmentSource(str, Enum):
    """片段来源"""

    MANUAL = "manual"
    AUTO = "auto"


class SegmentInput(BaseModel):
    """时间轴区间（秒）。切割以 start/end 为准；帧号由服务端换算，请求侧不传。"""

    start: float = Field(ge=0, description="起始时间（秒）")
    end: float = Field(gt=0, description="结束时间（秒）")


def _coerce_oss_blob(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    raw = dict(data)
    for blob_key in ("source", "video", "oss"):
        if blob_key in raw and raw[blob_key]:
            blob = str(raw.pop(blob_key)).strip()
            if blob.lower().startswith(("http://", "https://")):
                raw.setdefault("oss_url", blob)
            else:
                raw.setdefault("oss_key", blob)
            break
    return raw


def _normalize_oss_pair(
    oss_key: Optional[str], oss_url: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    key = (oss_key or "").strip() or None
    url = (oss_url or "").strip() or None
    if key and key.lower().startswith(("http://", "https://")):
        url = key
        key = None
    return key, url


class _OssRefMixin(BaseModel):
    """可选 oss_key / url（与 video_id 二选一体系配合）。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    oss_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("oss_key", "oss-key", "key"),
        description="OSS 对象键；与 video_id 二选一（可直接建任务）",
    )
    oss_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("oss_url", "oss-url", "url"),
        description="完整 URL；与 video_id 二选一",
    )


class CreateManualJobRequest(BaseModel):
    """创建手动切片任务：建档并立即入队切割。须先 import 至 ready。"""

    video_id: str = Field(
        description="已导入且 status=ready 的视频主键（= Video.`video_id`）",
    )
    segments: list[SegmentInput] = Field(
        min_length=1,
        description="切割区间列表（秒）",
    )

    @model_validator(mode="after")
    def _require_video_id(self) -> CreateManualJobRequest:
        vid = (self.video_id or "").strip()
        if not vid:
            raise ValueError("请提供 video_id（须先 POST /videos/import 并轮询至 ready）")
        self.video_id = vid
        return self


class CreateAutoJobRequest(BaseModel):
    """创建自动分镜任务：建档并立即入队检测（停在 preview，须再 /cut）。须先 import 至 ready。"""

    video_id: str = Field(
        description="已导入且 status=ready 的视频主键（= Video.`video_id`）",
    )
    detector: str = Field(
        default="content",
        description="检测算法：content / adaptive / semantic",
    )
    threshold: float = Field(
        default=35.0,
        description="content/adaptive 差异阈值；semantic 请用 sample_fps",
    )
    min_scene_len: float = Field(default=2.0, description="最短场景时长（秒）")
    sample_fps: Optional[float] = Field(
        default=None,
        ge=0.2,
        le=5.0,
        description="仅 detector=semantic 时生效的抽帧密度（fps）",
    )

    @model_validator(mode="after")
    def _require_video_id(self) -> CreateAutoJobRequest:
        vid = (self.video_id or "").strip()
        if not vid:
            raise ValueError("请提供 video_id（须先 POST /videos/import 并轮询至 ready）")
        self.video_id = vid
        return self


class CutJobRequest(BaseModel):
    """启动切割（仅自动 Job）。preview 后可省略 segments。"""

    segments: Optional[list[SegmentInput]] = Field(
        default=None,
        description="切割区间；省略则用当前 preview 分镜",
    )


class PublishRequest(BaseModel):
    """返回全部及选中切片的 OSS 对象列表。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    segment_ids: Optional[list[str]] = Field(
        default=None,
        description="要发布的片段 ID；省略或空表示全部就绪片段",
    )
    oss_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "oss_key", "oss-key", "oss_prefix", "prefix"
        ),
        description=(
            "兼容旧客户端的目标目录字段；当前接口不执行复制或保存，字段会被忽略"
        ),
    )

    @model_validator(mode="after")
    def _normalize_oss_key_prefix(self) -> PublishRequest:
        raw = (self.oss_key or "").strip()
        if not raw:
            self.oss_key = None
            return self
        text = raw.replace("\\", "/").lstrip("/")
        if not text or ".." in text.split("/"):
            raise ValueError("oss_key 目录前缀无效（禁止空路径与 ..）")
        if not text.endswith("/"):
            text += "/"
        self.oss_key = text
        return self


class OssImportRequest(BaseModel):
    """导入源片：oss_key（配置桶）与 url（任意 http(s)）二选一。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    oss_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("oss_key", "oss-key", "key"),
        description="配置 OSS_BUCKET 内对象键，例如 path/to/video.mp4",
    )
    oss_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("oss_url", "oss-url", "url"),
        description=(
            "任意可访问的 http(s) URL（与 oss_key 二选一）；"
            "不要求与 OSS_BUCKET 一致，服务端做 SSRF 校验后流式转存 OSS"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases_and_source(cls, data: Any) -> Any:
        return _coerce_oss_blob(data)

    @model_validator(mode="after")
    def _require_one(self) -> OssImportRequest:
        self.oss_key, self.oss_url = _normalize_oss_pair(self.oss_key, self.oss_url)
        if self.oss_key and self.oss_url:
            raise ValueError("请只传 oss_key（或 oss-key）与 url 其中之一")
        if not self.oss_key and not self.oss_url:
            raise ValueError("请提供 oss_key（或 oss-key）或 url")
        return self


class WaveformData(BaseModel):
    """音频波形峰值（随视频信息一并返回）。"""

    bins: int = Field(description="峰值点数")
    peaks: list[float] = Field(description="峰值 0~1")
    has_audio: bool = Field(description="是否含有效音轨")


class Video(BaseModel):
    """视频资源（GET 轮询；ready 时字段齐全）。"""

    video_id: str = Field(
        description="视频主键。路径 /videos/{video_id}、建 Job 的 body.video_id 都填此值"
    )
    status: VideoStatus = Field(
        description="导入状态：importing | ready | failed"
    )
    progress: float = Field(default=0.0, description="导入进度 0~100")
    message: str = Field(default="", description="状态说明")
    filename: str = Field(description="文件名")
    duration: Optional[float] = Field(
        default=None, description="时长（秒）；importing 时可为 null"
    )
    width: Optional[int] = Field(default=None, description="宽度；importing 时可为 null")
    height: Optional[int] = Field(default=None, description="高度；importing 时可为 null")
    fps: Optional[float] = Field(default=None, description="帧率；importing 时可为 null")
    codec: Optional[str] = Field(default=None, description="编码；importing 时可为 null")
    size_bytes: Optional[int] = Field(
        default=None, description="文件大小（字节）；importing 时可为 null"
    )
    preview_url: Optional[str] = Field(
        default=None,
        description="源片预览 URL（ready 后）；媒体接口会在 CDN 不可用时回退 OSS 签名 URL",
    )
    filmstrip_url: Optional[str] = Field(
        default=None, description="胶片条绝对 URL；ready 后可用"
    )
    waveform: Optional[WaveformData] = Field(
        default=None,
        description="波形峰值；ready 且有音轨时有值",
    )
    has_audio: bool = Field(default=False, description="是否含音轨")
    oss_key: Optional[str] = Field(default=None, description="实际源片 OSS 键；URL 导入时为服务受管 ingest 对象")
    source_url: Optional[str] = Field(
        default=None, description="源片 http(s) URL（url 导入时）"
    )


class ImportSubmitted(BaseModel):
    """异步导入提交结果；完整 Video 请轮询 GET /videos/{video_id}。"""

    video_id: str = Field(description="视频主键（标准 UUID）")
    status: VideoStatus = Field(description="初始多为 importing")
    oss_key: Optional[str] = Field(default=None, description="源片 OSS 键（oss_key 导入时）")
    source_url: Optional[str] = Field(
        default=None, description="源片 http(s) URL（url 导入时）"
    )


# 兼容旧名
VideoImportResponse = Video


class ApiAction(BaseModel):
    """需用指定 HTTP 方法调用的动作链接（勿当 GET 资源）。"""

    method: Literal["POST"] = Field(default="POST", description="HTTP 方法")
    href: str = Field(description="绝对或相对路径，如 /api/v1/jobs/{job_id}/publish")


class Segment(BaseModel):
    """切割 / 分镜片段"""

    segment_id: str = Field(
        description="片段主键；路径 /segments/{segment_id} 填此值"
    )
    job_id: str = Field(description="所属任务外键（= Job.`job_id`）")
    index: int = Field(description="序号")
    start_time: float = Field(description="起始秒")
    end_time: float = Field(description="结束秒")
    start_frame: Optional[int] = Field(
        default=None, description="起始帧（服务端按秒换算回传）"
    )
    end_frame: Optional[int] = Field(
        default=None, description="结束帧（服务端按秒换算回传）"
    )
    oss_key: Optional[str] = Field(default=None, description="预存切片 OSS 键（切割完成后即有）")
    oss_url: Optional[str] = Field(default=None, description="预存切片可访问 URL")
    download_url: Optional[str] = Field(
        default=None,
        description="下载地址；切割完成且有 OSS 对象后可用",
    )
    preview_url: Optional[str] = Field(default=None, description="预览地址")
    thumb_url: Optional[str] = Field(default=None, description="封面地址")
    download_filename: Optional[str] = Field(default=None, description="建议下载文件名")
    source: SegmentSource = Field(description="来源：manual / auto")
    confidence: Optional[float] = Field(
        default=None,
        description="检测侧内部分数；auto 常有（content/adaptive 多为 1.0/0.5），manual 为 null",
    )
    summary: Optional[str] = Field(
        default=None,
        description="仅 detector=semantic 时可能有模型简述；其余为 null",
    )
    status: str = Field(
        default="pending",
        description="pending | preview | done | failed（无 ready）",
    )


class JobSubmitted(BaseModel):
    """异步提交结果（manual/auto/cut）；完整 Job 请轮询 GET /jobs/{job_id}。"""

    job_id: str = Field(description="任务主键（标准 UUID）")
    video_id: str = Field(description="关联视频外键")
    mode: JobMode = Field(description="模式：manual / auto")
    status: JobStatus = Field(description="初始状态，通常为 pending / processing")


class Job(BaseModel):
    """切片任务（轮询完整对象）"""

    job_id: str = Field(description="任务主键；路径 /jobs/{job_id} 填此值（标准 UUID）")
    video_id: str = Field(
        description="关联视频外键（= Video.`video_id`）。拉预览：GET /videos/{video_id}"
    )
    mode: JobMode = Field(description="模式：manual / auto")
    status: JobStatus = Field(description="任务状态")
    progress: float = Field(default=0.0, description="进度 0~100")
    message: str = Field(default="", description="状态说明")
    created_at: datetime = Field(description="创建时间")
    segments: list[Segment] = Field(
        default_factory=list,
        description="片段/分镜列表（preview 及之后阶段有内容；轮询本字段即可）",
    )
    publish_action: Optional[ApiAction] = Field(
        default=None,
        description="返回切片 OSS 对象列表动作（status=done；必须 POST）",
    )


class PublishedSegmentItem(BaseModel):
    """切片 OSS 对象引用"""

    segment_id: str = Field(description="片段主键（= Segment.`segment_id`）")
    index: int = Field(description="序号")
    oss_key: str = Field(description="预存切片 OSS 键")
    oss_url: Optional[str] = Field(default=None, description="预存切片访问 URL")


class SkippedSegmentItem(BaseModel):
    """跳过发布的片段"""

    segment_id: str = Field(description="片段主键（= Segment.`segment_id`）")
    reason: str = Field(description="跳过原因")


class PublishResponse(BaseModel):
    """全部切片与前端选中切片的 OSS 对象列表"""

    all: list[PublishedSegmentItem] = Field(
        default_factory=list, description="全部预存切片对象"
    )
    selected: list[PublishedSegmentItem] = Field(
        default_factory=list, description="前端选择的切片对象"
    )


class OkResponse(BaseModel):
    """通用成功响应"""

    ok: bool = Field(default=True, description="是否成功")


class HealthChecks(BaseModel):
    ffmpeg: dict = Field(description="{ok: bool}")
    ffprobe: dict = Field(description="{ok: bool}")
    oss: dict = Field(description="{ok: bool}；含轻量 bucket 探测")
    store: dict = Field(
        default_factory=dict,
        description="{ok, backend=memory, videos, tasks, segments}",
    )
    scheduler: dict = Field(
        default_factory=dict,
        description="{ok, mode=inline, running, max_jobs}",
    )

class HealthData(BaseModel):
    ok: bool = Field(description="依赖是否全部可用")
    checks: HealthChecks
    version: str = Field(default="", description="APP_VERSION")
    git_sha: str = Field(
        default="unknown",
        description="构建时注入的短 commit；用于确认是否已 redeploy 新镜像",
    )
    build_time: str = Field(
        default="unknown",
        description="镜像构建 UTC 时间（ISO8601）",
    )


class ServiceInfo(BaseModel):
    name: str = Field(description="服务名称")
    version: str = Field(description="版本")
    docs: str = Field(description="Swagger 路径")
    api_prefix: str = Field(description="接口前缀")
