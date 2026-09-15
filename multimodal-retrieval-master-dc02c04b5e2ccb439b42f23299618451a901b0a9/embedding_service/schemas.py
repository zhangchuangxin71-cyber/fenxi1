from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .media_types import ImageContentType, TextContentType, VideoContentType


class OssMediaRef(BaseModel):
    object_key: str = Field(..., min_length=1, description="OSS 对象键（桶名用环境变量 ALIYUN_OSS_BUCKET）")
    content_type: str = Field(
        ...,
        min_length=3,
        description="传输数据的 MIME 类型，如 image/jpeg、video/mp4",
    )


class ImageClipEmbedRequest(OssMediaRef):
    content_type: ImageContentType = Field(..., description="图片 MIME 类型")
    media_id: str = Field(..., min_length=1)
    return_vector: bool = True
    write_vector: bool = False


class CaptionPayload(BaseModel):
    subject: str = ""
    color: str = ""
    action: str = ""
    style: str = ""
    description: str = ""
    caption_text: str | None = None


class ImageBgeEmbedRequest(BaseModel):
    media_id: str = Field(..., min_length=1)
    content_type: ImageContentType = Field(
        ...,
        description="关联图片的 MIME 类型（BGE 对文本编码，用于业务标注）",
    )
    description: str = Field(..., min_length=1, description="Text description for BGE embedding")
    return_vector: bool = True
    write_vector: bool = False


class VideoClipConfiguration(BaseModel):
    """视频 CLIP 抽帧与分段参数。"""

    sample_fps: float = Field(default=1.0, ge=0.1, le=30.0, description="每个片段内每秒抽候选帧数")
    max_frames: int = Field(default=16, ge=1, le=512, description="每个片段最多抽取的候选帧数")
    top_k_frames: int = Field(default=4, ge=1, le=64, description="K-means 多样性筛选后每段保留帧数")
    segment_seconds: int = Field(default=5, ge=1, le=300, description="视频分段时长（秒）")


class VideoClipEmbedRequest(OssMediaRef):
    content_type: VideoContentType = Field(..., description="视频 MIME 类型")
    media_id: str = Field(..., min_length=1)
    return_vector: bool = True
    write_vector: bool = False
    configuration: VideoClipConfiguration = Field(
        default_factory=VideoClipConfiguration,
        description="抽帧与分段配置",
    )


class VideoBgeEmbedRequest(BaseModel):
    media_id: str = Field(..., min_length=1)
    content_type: VideoContentType = Field(
        ...,
        description="关联视频的 MIME 类型（BGE 对文本编码，用于业务标注）",
    )
    description: str = Field(..., min_length=1, description="Text description for BGE embedding")
    return_vector: bool = True
    write_vector: bool = False


class MediaMetadata(BaseModel):
    width: int | None = None
    height: int | None = None
    source: Literal["oss"] = "oss"
    content_type: str | None = None
    bucket: str | None = None
    object_key: str | None = None


class ImageClipEmbedResponse(BaseModel):
    media_id: str
    modality: Literal["image"] = "image"
    scheme: Literal["clip"] = "clip"
    vector_dim: int
    embedding_model: str
    embedding: list[float] | None = None
    metadata: MediaMetadata
    milvus: dict | None = None


class ImageBgeEmbedResponse(BaseModel):
    media_id: str
    modality: Literal["image"] = "image"
    scheme: Literal["bge"] = "bge"
    vector_dim: int
    embedding_model: str
    caption: str
    embedding: list[float] | None = None
    metadata: MediaMetadata
    milvus: dict | None = None


class VideoClipEmbedResponse(BaseModel):
    media_id: str
    modality: Literal["video"] = "video"
    scheme: Literal["clip"] = "clip"
    vector_dim: int
    embedding_model: str
    embedding: list[float] | None = None
    metadata: MediaMetadata
    milvus: dict | None = None


class VideoBgeSegment(BaseModel):
    segment_id: str
    timestamp_seconds: float
    caption: str


class VideoBgeEmbedResponse(BaseModel):
    media_id: str
    modality: Literal["video"] = "video"
    scheme: Literal["bge"] = "bge"
    vector_dim: int
    embedding_model: str
    caption: str
    embedding: list[float] | None = None
    segments: list[VideoBgeSegment] = []
    metadata: MediaMetadata
    milvus: dict | None = None


class DualQueryEmbedRequest(BaseModel):
    query_id: str | None = None
    text: str = Field(..., min_length=1)
    content_type: TextContentType = Field(
        default="text/plain",
        description="查询文本的 MIME 类型",
    )
    return_vector: bool = True


class QueryVectorPayload(BaseModel):
    embedding_model: str
    vector_dim: int
    embedding: list[float] | None = None


class DualQueryEmbedResponse(BaseModel):
    query_id: str | None = None
    text: str
    vector_dim_clip: int
    vector_dim_bge: int
    clip: QueryVectorPayload
    bge: QueryVectorPayload


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "embedding_service"
    vector_store: str
    clip_model_path: str
    clip_device: str
    bge_model_name: str
    bge_device: str
