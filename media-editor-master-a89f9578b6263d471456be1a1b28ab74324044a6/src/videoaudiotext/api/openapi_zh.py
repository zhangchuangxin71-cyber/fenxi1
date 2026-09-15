"""Flow B API — OpenAPI 中文标签与说明。"""

from __future__ import annotations

OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "系统",
        "description": "健康检查、维护清理与服务依赖探测。",
    },
    {
        "name": "Task",
        "description": "Task 取消（POST /tasks/{task_id}/cancel）。",
    },
    {
        "name": "分句",
        "description": "LLM 自动分句（POST /split/run）→ Job + 轮询。",
    },
    {
        "name": "配音",
        "description": "按句拉取音频 URL，拼接 master.wav 与 subtitle.srt 并上传 OSS。",
    },
    {
        "name": "画面预览",
        "description": "字幕样式与分辨率预览（POST /visual/preview/run，可选）。",
    },
    {
        "name": "合成",
        "description": "异步合成成片，上传 OSS（MP4 + 音频 + 日志）并删除 Job 目录。",
    },
]

API_DESCRIPTION = """
## 流程 B 独立阶段 API（v3.1）

四个阶段接口**完全独立**，互不引用上游 `task_id`：
分句、配音、画面预览、合成。

每步 `POST .../run` 的请求体自带本步所需的 **segments + OSS URL**；
返回的 `task_id` **仅用于轮询本步状态**，下一步从上一轮 poll 的 `result` 复制 OSS URL 即可。

**OSS 交付物**（须传 `code` + `id`）：
- `audio/process` → master.wav、subtitle.srt、分段 wav
- `visual/preview` → 预览图 JPG
- `compose` → 成片 MP4、音轨 WAV、日志（ASS 由 SRT + 样式在合成时生成）

### 典型流程

1. `POST /split/run` → `GET /split/tasks/{id}` → 保存 segments
2. `POST /audio/process/run`（segments + 原始 wav URL）→ 保存 master/字幕 OSS URL
3. `POST /visual/preview/run`（可选：首句素材 URL + 字幕 + 样式）
4. `POST /compose/run`（master + subtitle.srt + segments + 素材 URL）→ `output_video_url`

`compose` 会在内部拉取每句素材，**无需**单独的素材绑定接口。

### OSS

- **输入**：HTTPS 可 GET 拉取（音频/素材/已处理配音）
- **输出**：须在 `.env` 配置 `ALIYUN_OSS_*`
"""


def apply_minimal_request_examples(schema: dict) -> None:
    """Swagger 请求体示例：compose 默认展示样式/BGM；其余接口仅必填字段。"""
    from pydantic import BaseModel

    from videoaudiotext.api.schemas import (
        AudioProcessRequest,
        ComposeRunRequest,
        PurgeJobsRequest,
        SplitRequest,
        VisualPreviewRequest,
    )

    routes: dict[str, type[BaseModel]] = {
        "/api/v1/split/run": SplitRequest,
        "/api/v1/audio/process/run": AudioProcessRequest,
        "/api/v1/visual/preview/run": VisualPreviewRequest,
        "/api/v1/compose/run": ComposeRunRequest,
        "/api/v1/maintenance/purge-jobs": PurgeJobsRequest,
        "/api/v1/maintenance/purge-workspaces": PurgeJobsRequest,
    }
    compose_path = "/api/v1/compose/run"
    for path, model in routes.items():
        post = schema.get("paths", {}).get(path, {}).get("post")
        if not post:
            continue
        content = (
            post.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
        )
        extra = model.model_config.get("json_schema_extra") or {}
        examples = extra.get("examples")
        if not examples:
            continue
        if path == compose_path and len(examples) >= 2:
            full, minimal = examples[0], examples[1]
            content["example"] = full
            content["examples"] = {
                "full": {
                    "summary": "含字幕样式 / 分辨率 / BGM",
                    "value": full,
                },
                "required_only": {
                    "summary": "仅必填字段",
                    "value": minimal,
                },
            }
        else:
            minimal = examples[0]
            content["example"] = minimal
            content["examples"] = {
                "required_only": {
                    "summary": "仅必填字段",
                    "value": minimal,
                }
            }
