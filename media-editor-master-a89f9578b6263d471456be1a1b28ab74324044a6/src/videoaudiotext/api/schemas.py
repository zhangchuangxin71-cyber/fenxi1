"""Pydantic request bodies for Flow B API."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# OpenAPI / Swagger 请求体示例
_EXAMPLE_SPLIT_TEXT = (
    "夜深之后，城市褪去了白日的喧嚣，穿行在空旷的街道上，"
    "抬眼望去，街角的面馆亮着一盏暖融融的灯。"
)
_EXAMPLE_HTTPS = "https://example.com"
_COMPOSE_EXAMPLE_SEGMENT = {
    "index": 1,
    "text": "夜深之后，城市褪去了白日的喧嚣，",
    "media": {"url": f"{_EXAMPLE_HTTPS}/1.mp4", "type": "video"},
}
_COMPOSE_EXAMPLE_REQUIRED = {
    "code": "demo",
    "id": "user-001",
    "master_audio_url": f"{_EXAMPLE_HTTPS}/master.wav",
    "subtitle_srt_url": f"{_EXAMPLE_HTTPS}/subtitle.srt",
    "segments": [_COMPOSE_EXAMPLE_SEGMENT],
}
_COMPOSE_EXAMPLE_FULL = {
    **_COMPOSE_EXAMPLE_REQUIRED,
    "voice_volume": 1.0,
    "resolution": {"width": 1080, "height": 1920},
    "subtitle_style": {
        "font_name": "思源黑体",
        "font_scale": 1.2,
        "y_offset": -1,
    },
    "bgm": {
        "url": f"{_EXAMPLE_HTTPS}/bgm.mp3",
        "bgm_volume": 0.15,
    },
}


class CreateWorkspaceRequest(BaseModel):
    """Deprecated — kept for type compatibility only."""

    label: Optional[str] = None
    workspace_id: Optional[str] = None


class SplitRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"text": _EXAMPLE_SPLIT_TEXT}],
        },
    )

    text: str = Field(
        ...,
        description="口播全文（段内换行自动合并；空行分段并在段末无句号时补。）",
    )
    include_ai_prompts: bool = Field(False, description="是否在分句结果中附带 AI 检索提示词")
    global_style: Optional[str] = Field(
        None,
        description="AI 提示词全局风格；仅 include_ai_prompts=true 时生效，未传时服务端随机选取",
    )

    @field_validator("text")
    @classmethod
    def normalize_text(cls, v: str) -> str:
        from videoaudiotext.text.split import normalize_script_text

        normalized = normalize_script_text(v)
        if not normalized:
            raise ValueError("text is required")
        return normalized


class SegmentAudioInput(BaseModel):
    url: str = Field(
        ...,
        description="该句音频 HTTP(S) URL 或 OSS object key（如 prod/.../1.wav）",
    )


class AudioProcessSegment(BaseModel):
    index: int = Field(..., ge=1, description="分句序号，与 split 一致")
    text: str = Field(..., description="分句文案，须与 split 返回逐字一致")
    audio: SegmentAudioInput = Field(..., description="该句配音来源")


class AudioProcessRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "code": "demo",
                    "id": "user-001",
                    "segments": [
                        {
                            "index": 1,
                            "text": "夜深之后，城市褪去了白日的喧嚣，",
                            "audio": {"url": f"{_EXAMPLE_HTTPS}/1.wav"},
                        }
                    ],
                }
            ],
        },
    )

    code: str = Field(..., description="租户编码，配音交付物 OSS 路径前缀")
    id: str = Field(..., description="用户 ID，配音交付物 OSS 路径前缀")
    segments: list[AudioProcessSegment] = Field(
        ...,
        min_length=1,
        description="每句一条，条数须与 split 一致",
    )
    force_refresh: bool = Field(
        False, description="true 时忽略缓存，强制重新拉取并处理音频"
    )
    speed: float = Field(
        1.0,
        gt=0,
        description="语速倍率；句间留白秒数 = 0.35 / speed（末句无留白）",
    )


class MediaBindUrlInput(BaseModel):
    url: str = Field(
        ...,
        description="素材 HTTP(S) URL 或 OSS object key",
    )
    type: Literal["image", "video"] = Field(..., description="素材类型：image 或 video")
    start_sec: Optional[float] = Field(
        0.0, description="从源素材第几秒开始裁切，默认 0"
    )


class MediaBindSegment(BaseModel):
    index: int = Field(..., ge=1, description="分句序号")
    text: str = Field(..., description="分句文案，须与 split 完全一致")
    media: MediaBindUrlInput = Field(..., description="该句绑定的画面素材")


class AudioTimingSegment(BaseModel):
    index: int = Field(..., ge=1)
    text: str = Field(..., description="分句文案")
    duration_sec: float = Field(..., gt=0, description="该句语音时长（秒，不含句间 gap）")
    clip_duration_sec: Optional[float] = Field(None, description="含留白的 clip 时长（秒）")


class ComposeSegment(BaseModel):
    index: int = Field(..., ge=1, description="分句序号")
    text: str = Field(..., description="分句文案，须与 subtitle.srt 对应句一致")
    duration_sec: Optional[float] = Field(
        None,
        gt=0,
        description="可选；不传时由 subtitle_srt_url 解析口播时长",
    )
    clip_duration_sec: Optional[float] = Field(
        None,
        description="可选；含 gap 的画面 clip 时长；不传则由 SRT/服务端计算",
    )
    media: MediaBindUrlInput = Field(..., description="该句绑定的画面素材")


class ResolutionInput(BaseModel):
    width: int = Field(
        1080,
        ge=64,
        le=7680,
        description="输出宽度（像素，服务端会自动取偶数）",
    )
    height: int = Field(
        1920,
        ge=64,
        le=7680,
        description="输出高度（像素，服务端会自动取偶数）",
    )


class VisualPreviewRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "code": "demo",
                    "id": "user-001",
                    "media_url": f"{_EXAMPLE_HTTPS}/1.jpg",
                    "media_type": "image",
                    "text": "预览字幕文案",
                    "resolution": {"width": 1080, "height": 1920},
                    "subtitle_style": {
                        "font_name": "思源黑体",
                        "font_scale": 1.2,
                        "y_offset": -1,
                    },
                }
            ],
        },
    )

    code: str = Field(..., description="租户编码，用于 OSS 路径前缀（预览图上传）")
    id: str = Field(..., description="用户 ID，用于 OSS 路径前缀（预览图上传）")
    media_url: str = Field(
        ...,
        description="首句画面素材 HTTP(S) URL 或 OSS object key（图片或视频）",
    )
    media_type: Literal["image", "video"] = Field(..., description="素材类型")
    start_sec: Optional[float] = Field(
        0.0, description="视频素材裁切起点（秒）；图片可忽略"
    )
    text: str = Field(..., description="预览字幕文案（单句）")
    resolution: ResolutionInput = Field(..., description="预览输出分辨率")
    subtitle_style: SubtitleStyleInput = Field(..., description="字幕样式")


class BgmInput(BaseModel):
    url: str = Field(
        ...,
        description="BGM HTTP(S) URL 或 OSS object key（mp3/wav/m4a/aac/flac/ogg）",
    )
    bgm_volume: float = Field(
        0.15,
        ge=0,
        description="BGM 音量倍率；口播时默认 ducking 压低背景",
    )


class ComposeRunRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [_COMPOSE_EXAMPLE_FULL, _COMPOSE_EXAMPLE_REQUIRED],
        },
    )

    code: str = Field(..., description="租户编码，用于 OSS 路径前缀")
    id: str = Field(..., description="用户 ID，用于 OSS 路径前缀")
    master_audio_url: str = Field(
        ...,
        description="完整配音轨 master.wav HTTP(S) URL 或 OSS object key",
    )
    subtitle_srt_url: str = Field(
        ...,
        description="字幕时间轴 subtitle.srt HTTP(S) URL 或 OSS object key",
    )
    subtitle_ass_url: Optional[str] = Field(
        None,
        description="（已废弃）勿传；compose 由 SRT + subtitle_style 生成 ASS",
    )
    voice_volume: float = Field(
        1.0,
        ge=0,
        description="口播音量倍率，1.0 为 master 原始响度；混 BGM 时同样作用于口播轨",
    )
    segment_urls: Optional[dict[str, str]] = Field(
        None,
        description="（已废弃）分段 wav URL；compose 改由 subtitle.srt 推导时间轴",
    )
    speed: float = Field(
        1.0,
        gt=0,
        description="可选；仅当无法从 SRT 解析 gap 时用于 fallback 计算句间留白",
    )
    segments: list[ComposeSegment] = Field(
        ...,
        min_length=1,
        description="分句文案 + 画面素材（每句一条；口播时长由 subtitle.srt 解析）",
    )
    subtitle_mode: Literal["hard", "soft"] = Field(
        "hard", description="hard=硬字幕烧录；soft=软字幕轨"
    )
    resolution: Optional[ResolutionInput] = Field(
        None, description="输出分辨率（不传则用默认 1080×1920）"
    )
    subtitle_style: Optional[SubtitleStyleInput] = Field(
        None, description="字幕样式（不传则用默认字体）"
    )
    bgm: Optional[BgmInput] = Field(
        None,
        description="可选背景音乐；与 master 混音后写入成片",
    )
    reuse_intermediates: bool = Field(
        True, description="复用已生成的 clip 等中间产物（二次合成建议 true）"
    )
    cleanup_scratch_on_success: bool = Field(
        True,
        description="成功后删除本次 compose scratch（Job 目录会在 OSS 上传后删除）",
    )


class AudioConfirmRequest(BaseModel):
    revision_id: Optional[str] = Field(
        None, description="（已废弃）要锁定的配音 revision_id"
    )
    audio_config_digest: Optional[str] = Field(
        None, description="（已废弃）要锁定的 revision 的 audio_config_digest"
    )


class StatelessAudioConfirmRequest(AudioConfirmRequest):
    task_id: str = Field(..., description="（已废弃）audio/process Task ID")
    master_audio_url: Optional[str] = Field(None, description="（已废弃）")
    subtitle_srt_url: Optional[str] = Field(None, description="（已废弃）")
    subtitle_ass_url: Optional[str] = Field(None, description="（已废弃）")


class SubtitleStyleInput(BaseModel):
    font_name: str = Field(
        ...,
        description=(
            "字幕字体名或别名，如「思源黑体」「迷茫体」「拼搏体」。"
            "传 GET /api/v1/subtitle/fonts 返回的 choices[].name，勿传 ass_font_name。"
        ),
    )
    font_scale: float = Field(
        1.0,
        ge=0.5,
        le=15.0,
        description="字号倍率，相对画布默认字号，范围 0.5–15.0",
    )
    y_offset: int = Field(
        0,
        ge=-10,
        le=20,
        description=(
            "字幕纵向偏移（档位，非像素）。1=上移 1 档，2=上移 2 档，-1=下移 1 档，0=默认。"
            "竖屏 1080×1920 下 1 档约 48px（屏高 2.5%）。正数上移，负数下移。"
        ),
    )


class PurgePolicyInput(BaseModel):
    ttl_empty_days: Optional[float] = Field(None, description="空 workspace 保留天数")
    ttl_abandoned_days: Optional[float] = Field(None, description="未完成 workspace 保留天数")
    ttl_composed_days: Optional[float] = Field(None, description="已合成 workspace 保留天数")
    grace_hours: Optional[float] = Field(None, description="新建 grace 小时数")


class PurgeJobsRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{}],
        },
    )

    dry_run: bool = Field(True, description="true 时只预览候选，不删除")
    max_delete: int = Field(100, ge=0, le=1000, description="单次最多删除数量")
    include_composed: bool = Field(
        False,
        description="是否包含已合成 Job 目录；默认 false",
    )
    policy: Optional[PurgePolicyInput] = Field(None, description="覆盖默认 TTL 策略")


class PurgeWorkspacesRequest(PurgeJobsRequest):
    """Deprecated alias."""
