# Flow B FastAPI 服务接口文档

> **版本**：API v2.30  
> **Base URL**：`http://127.0.0.1:8787`  
> **交互式文档**：[Swagger `/docs`](http://127.0.0.1:8787/docs) · [ReDoc `/redoc`](http://127.0.0.1:8787/redoc)  
> **联调 Walkthrough**：[api-flow-b-fastapi-walkthrough.md](./api-flow-b-fastapi-walkthrough.md)

---

## 一、接口概览

口播文案成片服务，提供 **文案分句 → 配音对齐 → 素材绑定 → 字幕预览 → 视频合成** 的 HTTP 流水线。每个任务对应一个 **workspace**（工作区），**compose 完成前**产物持久化在 `WORKSPACES_ROOT`；**compose 成功并上传 OSS 后，整个 workspace 会被删除**，成片与完整音频、任务日志保存在 OSS。

### 当前服务支持

- 服务健康检查
- 工作区创建 / 状态查询 / 删除
- 异步 - LLM 语义分句（任务提交 / 状态查询 / 取消）；可选 `include_ai_prompts` 生成每句生图/生视频提示词
- 异步 - 配音处理：按句拉取音频 URL → 每次生成一个 **revision**（最多 10 版，须 `label`）→ master.wav + 字幕（任务提交 / 状态查询 / 取消）
- 同步 - 列出配音 revision（`GET /audio/revisions`）
- 同步 - 锁定配音 revision 为 active（`PUT /audio/confirm`，可选）
- 异步 - 素材绑定：按句拉取视频/图片 URL（任务提交 / 状态查询 / 取消）
- 异步 - 字幕样式预览（任务提交 / 状态查询 / 取消）
- 异步 - 视频合成：**上传 OSS（MP4 + 完整音频 + 日志）并删除 workspace**（任务提交 / 状态查询 / 取消）
- 字幕字体列表查询
- 工作区文件下载（**过程预览**：音频、字幕、预览图、素材；**不含成功后的成片**）
- 维护 - 清理过期/未完成工作区（`POST /maintenance/purge-workspaces`）
- 维护 - 全局 Job 协调状态（`GET /maintenance/coord-status`）

### 通用说明

1. **配音与素材（输入）由业务侧准备**：业务侧将每句 wav、每段 mp4 上传至 OSS/CDN，API 通过 HTTPS URL 执行 GET 拉取。
2. **成片（输出）上传 OSS**：须在 API 服务 `.env` 配置 `ALIYUN_OSS_ENDPOINT`、`ALIYUN_OSS_ACCESS_KEY_ID`、`ALIYUN_OSS_ACCESS_KEY_SECRET`、`ALIYUN_OSS_BUCKET`；可选 `ALIYUN_OSS_PREFIX`（默认 `prod/ImagesVideosText2Video`）。
3. **统一响应格式**：成功时 `code: 0`，`message: "ok"`，业务数据在 `data` 中；失败时 `code` 为非 0 业务码，`data.request_id` 与响应头 `X-Request-Id` 一致。
4. **digest 校验**：`text_digest` 来自分句结果；`audio_config_digest` / `revision_id` 标识某一版配音；`media/bind` 须传 `revision_id`（无需先 confirm）。
5. **分句 text 必须逐字一致**：`audio/process`、`media/bind` 中每句 `text` 须与 split Job 的 `result.segments[].text` 完全一致。
6. **compose 成功后**：`GET .../status` 与 `GET .../files/*` 对该 workspace 返回 404；Job 结果归档于 `workspaces/_compose_results/{job_id}.json`，仍可用原 URL 轮询 `GET .../compose/jobs/{job_id}`。

### 异步任务规则

1. 调用流程：`POST .../{stage}/run` 提交任务 → `GET .../{stage}/jobs/{job_id}` 轮询 → `status: done` 时读 `data.result`
2. 路径绑定：`job_id` 必须与提交阶段路径前缀一致（如 split 的 job 不能用于 compose 查询），跨路径访问返回 404
3. 任务状态：`queued`、`running`、`done`、`failed`、`cancelled`
4. 同阶段互斥：同一 workspace 同一阶段同时仅允许一个 `queued`/`running` Job；重复提交返回 `40902`
5. 轮询建议：分句 2～3 秒（`include_ai_prompts=true` 时建议 **120s** timeout，约 2～3 秒轮询一次）；配音/素材绑定/合成 5～10 秒
6. 忘记 job_id：调用 `GET /api/v1/workspaces/{workspace_id}/status`，读 `active_*_job_id` 字段
7. Job 数据落盘在工作区目录；**重启后可继续轮询**已完成/失败/归档 Job；进行中的 `queued`/`running` 会被标为 **`interrupted`**，须重新提交
8. **全局并发**：`GLOBAL_JOB_WORKERS` 限制全机同时执行的 Job 数；打满后新 Job 仍为 `queued`，轮询可见 `queue_position` / `queue_ahead` / `queue_message`（见 §2.3）
9. **单机多 worker**：设置 `UVICORN_WORKERS>1` 时，各 worker 通过 `workspaces/_coord/` 文件锁共享并发计数与 workspace 互斥；**多机多实例**仍须 Redis 等分布式方案

### 部署并发环境变量

| 变量 | 默认 | 描述 |
|------|------|------|
| `UVICORN_WORKERS` | `1` | uvicorn worker 进程数 |
| `GLOBAL_JOB_WORKERS` | `min(4, CPU核数)` | 全机同时执行的 Job 上限（非每 worker 各算） |
| `CLIP_BUILD_WORKERS` | `min(4, CPU核数)`，上限 8 | 全机 CLIP/ffmpeg 并行线程上限 |

Docker：在 `.env` 或 `docker-compose.yml` 中设置；详见 [DOCKER_OPS.md](../DOCKER_OPS.md)。

### OSS 素材 URL 模板（2026-06-25 实测 Bucket：example-bucket）

| 用途 | URL 模板 |
|------|----------|
| 第 N 句音频（输入） | `https://example-bucket.oss-cn-region.aliyuncs.com/audio/N.wav` |
| 第 N 段视频（输入） | `https://example-bucket.oss-cn-region.aliyuncs.com/source_media/N.mp4` |
| 成片 MP4（输出） | `https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/{code}-{id}/{date}/{job_id}.mp4` |
| 完整音频（输出） | `…/{job_id}.wav` |
| 任务日志（输出） | `…/{job_id}.log` |

`N` = 1 … `segment_count`（LLM 分句本次实测 **13 句**，常见 13～16 句，**以 split 返回为准**）。私有桶使用预签名 URL。

### 推荐调用顺序

1. `GET /api/v1/health`
2. `POST /api/v1/workspaces`
3. `POST /api/v1/workspaces/{id}/split/run` → 轮询 `split/jobs/{job_id}`
4. `POST /api/v1/workspaces/{id}/audio/process/run` → 轮询 `audio/process/jobs/{job_id}`
5. `PUT /api/v1/workspaces/{id}/audio/confirm`
6. `POST /api/v1/workspaces/{id}/media/bind/run` → 轮询 `media/bind/jobs/{job_id}`
7. `POST /api/v1/workspaces/{id}/visual/preview/run`（可选）→ 轮询
8. `POST /api/v1/workspaces/{id}/compose/run`（须 `code` + `id`）→ 轮询 `compose/jobs/{job_id}`
9. 从 `compose/jobs/{job_id}` 的 `result.output_video_url` 获取 **OSS 成片**（勿再访问 `/files/deliverables/output.mp4`）

> **2026-06-25 OSS 实测**：`job_b90e677e7fd4`，成片 [`…/job_b90e677e7fd4.mp4`](https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.mp4)

### Job 轮询与超时建议（13 句实测参考）

| 阶段 | 提交 | 轮询 | 建议间隔 | 典型耗时 |
|------|------|------|----------|----------|
| 分句 | `POST .../split/run` | `GET .../split/jobs/{id}` | 2～3 s | ~10 s |
| 配音 | `POST .../audio/process/run` | `GET .../audio/process/jobs/{id}` | 5 s | ~15 s |
| 素材绑定 | `POST .../media/bind/run` | `GET .../media/bind/jobs/{id}` | 5～10 s | ~5 min（OSS 视频拉取） |
| 画面预览 | `POST .../visual/preview/run` | `GET .../visual/preview/jobs/{id}` | 3 s | 10～30 s |
| 合成 | `POST .../compose/run` | `GET .../compose/jobs/{id}` | 5～10 s | ~60 s（含 OSS 上传） |
| **全流程合计（13 句）** | — | — | — | **~5.5 min** |

---

## 二、通用响应结构

### 2.1 成功响应

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `code` | integer | `0` 表示成功 |
| `message` | string | 固定 `"ok"` |
| `data` | object | 业务数据 |

### 2.2 错误响应

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `code` | integer | 业务错误码（见[附录 A](#附录-a常见错误码)） |
| `message` | string | 错误说明 |
| `data.request_id` | string | 请求 ID，与响应头 `X-Request-Id` 一致 |

### 2.3 异步 Job 对象（轮询接口 `data` 字段）

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `job_id` | string | 任务 ID |
| `kind` | string | 任务类型：`split` / `audio_process` / `media_bind` / `visual_preview` / `compose` |
| `status` | string | `queued` / `running` / `done` / `failed` / `cancelled` / `interrupted` |
| `progress` | object | 进度：`phase`、`percent`、`message` |
| `result` | object | `status=done` 时的业务结果 |
| `error` | object | `status=failed` 或 `interrupted` 时的错误详情 |
| `queue_position` | integer | 可选；`status=queued` 且等待全局槽位时，排队位置（从 1 起） |
| `queue_ahead` | integer | 可选；前方等待任务数 |
| `queue_message` | string | 可选；排队说明文案 |
| `created_at` | string | ISO 8601 创建时间 |
| `updated_at` | string | ISO 8601 更新时间 |
| `heartbeat_at` | string | 最近心跳时间 |
| `options` | object | 提交时的请求参数快照 |

---

## 1. 健康检查 API

### 1.1 API 接口

```
GET http://127.0.0.1:8787/api/v1/health
```

### 1.2 Request 参数

无。

### 1.3 Response 响应参数

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `data.ok` | boolean | 依赖是否全部可用 |
| `data.checks.ffmpeg.ok` | boolean | ffmpeg 是否可用 |
| `data.checks.ffprobe.ok` | boolean | ffprobe 是否可用 |
| `data.checks.llm_split.ok` | boolean | LLM 分句是否已配置 |

### 1.4 使用示例

```bash
curl http://127.0.0.1:8787/api/v1/health
```

### 1.5 成功响应

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "ok": true,
    "checks": {
      "ffmpeg": { "ok": true },
      "ffprobe": { "ok": true },
      "llm_split": { "ok": true }
    }
  }
}
```

### 1.6 失败响应

ffmpeg/ffprobe 不可用时 HTTP 503，`code=50301`，`message=health_check_failed`。

---

## 2. 创建工作区 API

### 2.1 API 接口

```
POST http://127.0.0.1:8787/api/v1/workspaces
```

### 2.2 Request Body 请求参数

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| `label` | string | 否 | null | 工作区显示名称 |

### 2.3 Request 示例

```json
{
  "label": "test"
}
```

### 2.4 Response 响应参数

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `data.workspace_id` | string | 工作区 ID，格式 `ws_xxxxxxxxxxxx` |
| `data.label` | string | 显示名称 |
| `data.created_at` | string | 创建时间（UTC ISO 8601） |
| `data.status.split_ready` | boolean | 是否已完成分句 |
| `data.status.audio_ready` | boolean | 是否已完成配音处理 |
| `data.status.audio_confirmed` | boolean | 是否已锁定配音 |
| `data.status.media_bound` | boolean | 是否已绑定素材 |
| `data.status.visual_preview_ready` | boolean | 是否已生成预览 |
| `data.status.compose_ready` | boolean | 是否具备合成条件 |

### 2.5 成功响应（实测）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "workspace_id": "ws_71c42ecc4c3c",
    "label": "test",
    "created_at": "2026-06-24T07:41:21.259454+00:00",
    "status": {
      "split_ready": false,
      "audio_ready": false,
      "audio_confirmed": false,
      "media_bound": false,
      "visual_preview_ready": false,
      "compose_ready": false
    }
  }
}
```

### 2.6 curl 示例

```bash
curl -X POST "http://127.0.0.1:8787/api/v1/workspaces" \
  -H "Content-Type: application/json" \
  -d '{"label": "test"}'
```

---

## 3. 查询工作区状态 API

### 3.1 API 接口

```
GET http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/status
```

### 3.2 Path 参数

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| `workspace_id` | string | 是 | 工作区 ID |

### 3.3 Response 响应参数

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `data.split_ready` | boolean | 分句是否就绪 |
| `data.audio_ready` | boolean | 配音是否就绪 |
| `data.audio_confirmed` | boolean | 配音是否已锁定 |
| `data.media_bound` | boolean | 素材是否已绑定 |
| `data.digests.text_digest` | string | 分句 digest |
| `data.digests.audio_confirmed_digest` | string | 已锁定配音 digest |
| `data.digests.media_binding_digest` | string | 素材绑定 digest |
| `data.digests.render_style_digest` | string | 字幕样式 digest |
| `data.active_split_job_id` | string | 进行中的分句 Job ID，无则为 null |
| `data.active_audio_process_job_id` | string | 进行中的配音 Job ID |
| `data.active_media_bind_job_id` | string | 进行中的素材绑定 Job ID |
| `data.active_visual_preview_job_id` | string | 进行中的预览 Job ID |
| `data.active_compose_job_id` | string | 进行中的合成 Job ID |

### 3.4 curl 示例

```bash
curl "http://127.0.0.1:8787/api/v1/workspaces/ws_71c42ecc4c3c/status"
```

### 3.5 用途说明

- 查询各阶段是否完成
- 忘记 `job_id` 时，通过 `active_*_job_id` 获取正在运行的任务 ID
- `media/bind` 长时间无响应时，查 `media_bound` 与 `active_media_bind_job_id` 判断是否已完成

---

## 4. LLM 语义分句 API（异步任务）

### 4.1 提交分句任务

#### 4.1.1 API 接口

```
POST http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/split/run
```

#### 4.1.2 Request Body 请求参数

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| `text` | string | 是 | - | 口播全文，**须为单行**，不能含换行 |
| `include_ai_prompts` | boolean | 否 | false | 是否在结果中附带 AI 生图/生视频提示词 |
| `global_style` | string | 否 | `电影质感` | `include_ai_prompts=true` 时生效 |

#### 4.1.3 Request 示例（仅分句，默认）

```json
{
  "text": "夜深之后，城市褪去了白日的喧嚣，穿行在空旷的街道上，抬眼望去，街角的面馆亮着一盏暖融融的灯。放眼望去，小店不大，却在冷清的夜色里显得格外温馨，成了晚归之人的一处避风港。细看，大锅里的骨汤咕嘟咕嘟持续翻滚，手工面条下入锅中，在沸水里轻轻起伏。一碗碗热气腾腾的面食被端上桌，升腾的白雾裹着鲜香，瞬间驱散了深夜的寒意。加班至深夜的打工人、独自在外打拼的异乡人安静坐在桌前，低头嗦面、小口喝汤。一口热食下肚，一路奔波的疲惫、独处异乡的孤单，都在这一碗面的温度里慢慢消散。",
  "include_ai_prompts": false
}
```

#### 4.1.4 Request 示例（分句并生成 AI 提示词）

在同一 `POST .../split/run` 接口中，将 `include_ai_prompts` 设为 `true`，并可选传入 `global_style`。无独立「生成提示词」接口；分句完成后在 Job `result` 中返回每句 `ai_prompts` 与全局 `ai_prompts_global`。

```json
{
  "text": "夜深之后，城市褪去了白日的喧嚣，穿行在空旷的街道上，抬眼望去，街角的面馆亮着一盏暖融融的灯。放眼望去，小店不大，却在冷清的夜色里显得格外温馨，成了晚归之人的一处避风港。细看，大锅里的骨汤咕嘟咕嘟持续翻滚，手工面条下入锅中，在沸水里轻轻起伏。一碗碗热气腾腾的面食被端上桌，升腾的白雾裹着鲜香，瞬间驱散了深夜的寒意。加班至深夜的打工人、独自在外打拼的异乡人安静坐在桌前，低头嗦面、小口喝汤。一口热食下肚，一路奔波的疲惫、独处异乡的孤单，都在这一碗面的温度里慢慢消散。",
  "include_ai_prompts": true,
  "global_style": "电影质感，深夜烟火气，暖色调纪录片"
}
```

> **说明**：`include_ai_prompts=true` 会额外调用 LLM 生成提示词，耗时高于普通分句。video 类 prompt 的时长用 **4.5 字/秒** 估算（仅喂 LLM，与后续配音实测时长无关）。

#### 4.1.5 Response 响应参数

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `data.job_id` | string | 任务 ID |
| `data.status` | string | 初始状态，通常为 `queued` |

#### 4.1.6 成功响应

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_74a7e9c43a39",
    "status": "queued"
  }
}
```

#### 4.1.7 curl 示例

仅分句：

```bash
curl -X POST "http://127.0.0.1:8787/api/v1/workspaces/ws_71c42ecc4c3c/split/run" \
  -H "Content-Type: application/json" \
  -d '{"text":"夜深之后，城市褪去了白日的喧嚣，……","include_ai_prompts":false}'
```

分句并生成 AI 提示词：

```bash
curl -X POST "http://127.0.0.1:8787/api/v1/workspaces/ws_71c42ecc4c3c/split/run" \
  -H "Content-Type: application/json" \
  --max-time 120 \
  -d '{
    "text": "夜深之后，城市褪去了白日的喧嚣，穿行在空旷的街道上，抬眼望去，街角的面馆亮着一盏暖融融的灯。放眼望去，小店不大，却在冷清的夜色里显得格外温馨，成了晚归之人的一处避风港。细看，大锅里的骨汤咕嘟咕嘟持续翻滚，手工面条下入锅中，在沸水里轻轻起伏。一碗碗热气腾腾的面食被端上桌，升腾的白雾裹着鲜香，瞬间驱散了深夜的寒意。加班至深夜的打工人、独自在外打拼的异乡人安静坐在桌前，低头嗦面、小口喝汤。一口热食下肚，一路奔波的疲惫、独处异乡的孤单，都在这一碗面的温度里慢慢消散。",
    "include_ai_prompts": true,
    "global_style": "电影质感，深夜烟火气，暖色调纪录片"
  }'
```

### 4.2 查询分句任务状态

#### 4.2.1 API 接口

```
GET http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/split/jobs/{job_id}
```

#### 4.2.2 Path 参数

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| `workspace_id` | string | 是 | 工作区 ID |
| `job_id` | string | 是 | 提交任务返回的 job_id |

#### 4.2.3 Response 响应参数（`result` 字段，`status=done` 时）

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `result.text_digest` | string | 分句摘要，后续 audio/bind 校验用 |
| `result.split_mode_used` | string | `llm` / `rule` / `rule_fallback` |
| `result.segment_count` | integer | 分句数量 |
| `result.segments[].index` | integer | 句序号（1-based） |
| `result.segments[].text` | string | 分句文案 |
| `result.segments[].search_query` | string | 检索关键词 |
| `result.segments[].visual` | boolean | 是否有画面 |
| `result.segments[].ai_prompts` | object | 仅 `include_ai_prompts=true`；含 `image` / `video` 的 `positive_prompt`、`negative_prompt` |
| `result.ai_prompts_global` | object | 仅 `include_ai_prompts=true`；含 `video_global_negative_prompt`、`video_film_look` |
| `result.warnings` | array | 告警信息（如 LLM 回退） |

#### 4.2.4 运行中响应

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_74a7e9c43a39",
    "kind": "split",
    "status": "running",
    "progress": { "phase": "split", "percent": 10, "message": "LLM 分句中…" }
  }
}
```

#### 4.2.5 成功响应（实测，15 句，节选）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_74a7e9c43a39",
    "kind": "split",
    "status": "done",
    "progress": { "phase": "finalize", "percent": 100, "message": "完成" },
    "result": {
      "text_digest": "b8b58d28b62dfec8d0c62f4da5523a57",
      "split_mode_used": "llm",
      "segment_count": 15,
      "segments": [
        { "index": 1, "text": "夜深之后，城市褪去了白日的喧嚣，", "search_query": "深夜城市 褪去喧嚣 街景", "visual": true },
        { "index": 2, "text": "穿行在空旷的街道上，", "search_query": "空旷街道 夜晚 行人穿行", "visual": true },
        { "index": 3, "text": "街角的面馆亮着一盏暖融融的灯。", "search_query": "街角面馆 暖灯 夜晚街景", "visual": true }
      ],
      "warnings": []
    }
  }
}
```

#### 4.2.6 成功响应（含 AI 提示词，`include_ai_prompts=true`，节选）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_74a7e9c43a39",
    "kind": "split",
    "status": "done",
    "result": {
      "text_digest": "b8b58d28b62dfec8d0c62f4da5523a57",
      "split_mode_used": "llm",
      "segment_count": 15,
      "segments": [
        {
          "index": 1,
          "text": "夜深之后，城市褪去了白日的喧嚣，",
          "search_query": "深夜城市 褪去喧嚣 街景",
          "visual": true,
          "ai_prompts": {
            "image": {
              "positive_prompt": "深夜城市街景，褪去喧嚣，暖色路灯，9:16竖屏，电影质感，…",
              "negative_prompt": "低质量，模糊，水印，…"
            },
            "video": {
              "positive_prompt": "缓慢推镜，深夜空旷街道，城市夜景，…",
              "negative_prompt": ""
            }
          }
        }
      ],
      "ai_prompts_global": {
        "video_global_negative_prompt": "抖动，过曝，字幕，…",
        "video_film_look": "35mm film grain, soft contrast, warm tones, …"
      },
      "warnings": []
    }
  }
}
```

#### 4.2.7 curl 示例

```bash
curl "http://127.0.0.1:8787/api/v1/workspaces/ws_71c42ecc4c3c/split/jobs/job_74a7e9c43a39"
```

### 4.3 取消分句任务

#### 4.3.1 API 接口

```
POST http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/split/jobs/{job_id}/cancel
```

#### 4.3.2 说明

仅 `queued`/`running` 可取消。Request body 留空。

---

## 5. 配音处理 API（异步任务）

### 5.1 提交配音任务

#### 5.1.1 API 接口

```
POST http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/audio/process/run
```

#### 5.1.2 Request Body 请求参数

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| `label` | string | **是** | - | 本版配音标签（1–64 字，试听列表展示） |
| `text_digest` | string | 否 | - | split 返回的 text_digest；不匹配返回 40901 |
| `segments` | array | 是 | - | 每句一条，条数须与 split 一致 |
| `segments[].index` | integer | 是 | - | 句序号 |
| `segments[].text` | string | 是 | - | 须与 split 逐字一致 |
| `segments[].audio.url` | string | 是 | - | 该句 wav 的 HTTPS URL |
| `force_refresh` | boolean | 否 | false | true 时忽略同 digest 缓存，强制重新拉取 |
| `speed` | number | 否 | 1.0 | 语速倍率 |

#### 5.1.3 Request 示例（实测，15 句，节选）

```json
{
  "label": "男声第一稿",
  "text_digest": "b8b58d28b62dfec8d0c62f4da5523a57",
  "segments": [
    { "index": 1, "text": "夜深之后，城市褪去了白日的喧嚣，", "audio": { "url": "https://example-bucket.oss-cn-region.aliyuncs.com/audio/1.wav" } },
    { "index": 2, "text": "穿行在空旷的街道上，", "audio": { "url": "https://example-bucket.oss-cn-region.aliyuncs.com/audio/2.wav" } },
    { "index": 15, "text": "独处异乡的孤单，都在这一碗面的温度里慢慢消散。", "audio": { "url": "https://example-bucket.oss-cn-region.aliyuncs.com/audio/15.wav" } }
  ],
  "force_refresh": false
}
```

#### 5.1.4 成功响应（提交）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_0cdcfe908800",
    "status": "queued"
  }
}
```

#### 5.1.5 curl 示例

```bash
curl -X POST "http://127.0.0.1:8787/api/v1/workspaces/ws_71c42ecc4c3c/audio/process/run" \
  -H "Content-Type: application/json" \
  -d @audio_process_request.json
```

### 5.2 查询配音任务状态

#### 5.2.1 API 接口

```
GET http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/audio/process/jobs/{job_id}
```

#### 5.2.2 Response 响应参数（`result` 字段，`status=done` 时）

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `result.revision_id` | string | 本版 ID（`rev_` 前缀）；bind / confirm 使用 |
| `result.label` | string | 本版标签 |
| `result.status` | string | 固定 `draft`（未提升为 active 前） |
| `result.audio_config_digest` | string | 本版 digest |
| `result.segment_count` | integer | 句数 |
| `result.total_speech_sec` | number | 纯口播时长（秒） |
| `result.total_with_gaps_sec` | number | 含句间间隔的总时长 |
| `result.master_audio_url` | string | 本版 master.wav（`audio/revisions/{id}/`） |
| `result.subtitle_srt_url` | string | 本版字幕 SRT |
| `result.subtitle_ass_url` | string | 本版字幕 ASS |
| `result.evicted_revision_ids` | array | 超出 10 版上限时被删除的旧 revision（可选） |
| `result.segments[].duration_sec` | number | 该句口播时长 |
| `result.segments[].clip_duration_sec` | number | 该句 clip 时长（含留白） |

#### 5.2.3 成功响应（实测，关键字段）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_0cdcfe908800",
    "kind": "audio_process",
    "status": "done",
    "result": {
      "revision_id": "rev_a1b2c3d4e5f6",
      "label": "男声第一稿",
      "status": "draft",
      "audio_config_digest": "ed8ebd212cd04c5da753d6de638941bb",
      "segment_count": 15,
      "total_speech_sec": 63.335918,
      "total_with_gaps_sec": 66.435918,
      "master_audio_url": "/api/v1/workspaces/ws_71c42ecc4c3c/files/audio/revisions/rev_a1b2c3d4e5f6/master.wav",
      "subtitle_srt_url": "/api/v1/workspaces/ws_71c42ecc4c3c/files/subtitles/revisions/rev_a1b2c3d4e5f6/subtitle.srt",
      "subtitle_ass_url": "/api/v1/workspaces/ws_71c42ecc4c3c/files/subtitles/revisions/rev_a1b2c3d4e5f6/subtitle.ass"
    }
  }
}
```

#### 5.2.4 耗时参考

15 句音频拉取约 7 秒（视 OSS 网速而定）。建议 timeout：`max(180, segment_count × 90)` 秒。

### 5.3 取消配音任务

#### 5.3.1 API 接口

```
POST http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/audio/process/jobs/{job_id}/cancel
```

---

## 6. 配音 revision 列表（同步）

### 6.1 API 接口

```
GET http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/audio/revisions
```

### 6.2 说明

返回当前 workspace 最多 **10** 条配音 revision（含 `label`、试听 URL、`audio_config_digest`）。`active` 版置顶。

### 6.3 Response 示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "active_revision_id": "rev_a1b2c3d4e5f6",
    "max_revisions": 10,
    "count": 2,
    "revisions": [
      {
        "revision_id": "rev_a1b2c3d4e5f6",
        "label": "男声第一稿",
        "status": "active",
        "is_active": true,
        "audio_config_digest": "ed8ebd212cd04c5da753d6de638941bb",
        "master_audio_url": "/api/v1/workspaces/ws_71c42ecc4c3c/files/audio/revisions/rev_a1b2c3d4e5f6/master.wav",
        "subtitle_srt_url": "/api/v1/workspaces/ws_71c42ecc4c3c/files/subtitles/revisions/rev_a1b2c3d4e5f6/subtitle.srt",
        "subtitle_ass_url": "/api/v1/workspaces/ws_71c42ecc4c3c/files/subtitles/revisions/rev_a1b2c3d4e5f6/subtitle.ass",
        "total_with_gaps_sec": 66.435918
      }
    ]
  }
}
```

---

## 7. 锁定配音 revision（同步，可选）

### 7.1 API 接口

```
PUT http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/audio/confirm
```

### 7.2 Request Body

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| `revision_id` | string | 二选一 | 要提升为 active 的 revision |
| `audio_config_digest` | string | 二选一 | 与 revision 对应的 digest |

```json
{
  "revision_id": "rev_a1b2c3d4e5f6"
}
```

### 7.3 说明

- **可选步骤**：若直接 `media/bind` 并传 `revision_id`，可跳过本接口。
- 将指定 revision 复制到 `audio/active/`、`subtitles/active/`，并置 `audio_confirmed=true`。
- 若换用另一 revision，**须重新** `media/bind`。

### 7.4 Response 示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "revision_id": "rev_a1b2c3d4e5f6",
    "label": "男声第一稿",
    "audio_confirmed": true,
    "audio_config_digest": "ed8ebd212cd04c5da753d6de638941bb",
    "master_audio_url": "/api/v1/workspaces/ws_71c42ecc4c3c/files/audio/active/master.wav",
    "subtitle_srt_url": "/api/v1/workspaces/ws_71c42ecc4c3c/files/subtitles/active/subtitle.srt",
    "subtitle_ass_url": "/api/v1/workspaces/ws_71c42ecc4c3c/files/subtitles/active/subtitle.ass"
  }
}
```

---

## 8. 素材绑定 API（异步任务）

### 8.1 提交素材绑定任务

#### 8.1.1 API 接口

```
POST http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/media/bind/run
```

#### 8.1.2 Request Body 请求参数

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| `revision_id` | string | **是** | - | 要绑定的配音 revision（**无需先 confirm**） |
| `text_digest` | string | 否 | - | split 返回的 text_digest |
| `audio_config_digest` | string | 否 | - | revision 的 digest（建议传入，不一致返回 40901） |
| `segments` | array | 是 | - | 每句一条 |
| `segments[].index` | integer | 是 | - | 句序号 |
| `segments[].text` | string | 是 | - | 须与 split 逐字一致 |
| `segments[].media.url` | string | 是 | - | 视频/图片 HTTPS URL |
| `segments[].media.type` | string | 是 | - | `video` 或 `image` |
| `segments[].media.start_sec` | number | 否 | 0.0 | 源素材裁切起点（秒） |

#### 8.1.3 Request 示例（实测，15 句，节选）

```json
{
  "revision_id": "rev_a1b2c3d4e5f6",
  "text_digest": "b8b58d28b62dfec8d0c62f4da5523a57",
  "audio_config_digest": "ed8ebd212cd04c5da753d6de638941bb",
  "segments": [
    { "index": 1, "text": "夜深之后，城市褪去了白日的喧嚣，", "media": { "url": "https://example-bucket.oss-cn-region.aliyuncs.com/source_media/1.mp4", "type": "video", "start_sec": 0.0 } },
    { "index": 15, "text": "独处异乡的孤单，都在这一碗面的温度里慢慢消散。", "media": { "url": "https://example-bucket.oss-cn-region.aliyuncs.com/source_media/14.mp4", "type": "video", "start_sec": 0.0 } }
  ]
}
```

#### 8.1.4 成功响应（提交）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_fd4d87e57757",
    "status": "queued"
  }
}
```

#### 8.1.5 重复提交

若已有进行中的 media/bind Job，返回 `40902 media_bind_job_active`。应轮询已有 Job 或先 cancel。

### 8.2 查询素材绑定任务状态

#### 8.2.1 API 接口

```
GET http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/media/bind/jobs/{job_id}
```

#### 8.2.2 Response 响应参数（`result` 字段，`status=done` 时）

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `result.bound` | boolean | 是否绑定成功 |
| `result.revision_id` | string | 已绑定的配音 revision |
| `result.segment_count` | integer | 句数 |
| `result.media_binding_digest` | string | 素材绑定 digest |
| `result.segments[].media.source_url` | string | 原始 OSS URL |
| `result.segments[].media.local_path` | string | 工作区内路径 |
| `result.segments[].media.file_url` | string | API 文件访问相对路径 |
| `result.segments[].clip_duration_sec` | number | 该句 clip 时长 |

#### 7.2.3 运行中响应

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_fd4d87e57757",
    "kind": "media_bind",
    "status": "running",
    "progress": { "phase": "fetch", "percent": 36, "message": "拉取素材 5/15（index=5）…" }
  }
}
```

#### 7.2.4 成功响应（实测，关键字段）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_fd4d87e57757",
    "kind": "media_bind",
    "status": "done",
    "result": {
      "bound": true,
      "segment_count": 15,
      "media_binding_digest": "1d99ce20bba64a1bb5714f6373811cf9",
      "segments": [
        {
          "index": 1,
          "text": "夜深之后，城市褪去了白日的喧嚣，",
          "clip_duration_sec": 4.16,
          "media": {
            "source_url": "https://example-bucket.oss-cn-region.aliyuncs.com/source_media/1.mp4",
            "type": "video",
            "local_path": "source_media/1.mp4",
            "file_url": "/api/v1/workspaces/ws_71c42ecc4c3c/files/source_media/1.mp4",
            "start_sec": 0
          }
        }
      ]
    }
  }
}
```

#### 7.2.5 耗时参考

15 段视频本次约 4 分钟。建议 timeout：`max(180, segment_count × 120)` 秒。

### 7.3 取消素材绑定任务

#### 7.3.1 API 接口

```
POST http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/media/bind/jobs/{job_id}/cancel
```

#### 7.3.2 成功响应（实测）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_388db4e5122d",
    "status": "cancelled",
    "cleaned_paths": ["uploads/media_raw_* (13)"]
  }
}
```

---

## 8. 字幕样式预览 API（异步任务，可选）

### 8.1 提交预览任务

#### 8.1.1 API 接口

```
POST http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/visual/preview/run
```

#### 8.1.2 前置条件

`split_ready`、`audio_confirmed`、`media_bound` 均为 true。

#### 8.1.3 Request Body 请求参数

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| `resolution.mode` | string | 否 | `1080x1920` | 输出画布：`1080x1920` / `1920x1080` / `720x1280` / `custom` |
| `subtitle_style.font_name` | string | 是 | - | 字体名，见 `GET /subtitle/fonts` |
| `subtitle_style.font_scale` | number | 否 | 1.0 | 字号倍率，建议 0.5～2.5 |
| `subtitle_style.y_offset` | integer | 否 | 0 | 纵向偏移档位 -10～20，非像素；正上移负下移 |
| `preview_segment_index` | integer | 否 | 1 | 预览第几句（1-based） |

#### 8.1.4 Request 示例（实测）

```json
{
  "resolution": { "mode": "1080x1920" },
  "subtitle_style": {
    "font_name": "思源黑体",
    "font_scale": 1.2,
    "y_offset": -1
  },
  "preview_segment_index": 1
}
```

#### 8.1.5 成功响应（提交）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_448a16c853ce",
    "status": "queued"
  }
}
```

### 8.2 查询预览任务状态

#### 8.2.1 API 接口

```
GET http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/visual/preview/jobs/{job_id}
```

#### 8.2.2 成功响应（实测）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_448a16c853ce",
    "kind": "visual_preview",
    "status": "done",
    "result": {
      "render_style_digest": "98df6bf04d99a8dd3a695f5013071934",
      "resolution": { "width": 1080, "height": 1920 },
      "subtitle_style_applied": {
        "font_name": "思源黑体",
        "font_size_px": 96,
        "font_scale": 1.2,
        "y_offset": -1,
        "y_offset_px": -48,
        "y_offset_label": "下移 1 档"
      },
      "preview": {
        "segment_index": 1,
        "text": "夜深之后，城市褪去了白日的喧嚣，",
        "preview_image_url": "/api/v1/workspaces/ws_71c42ecc4c3c/files/previews/seg_1.jpg"
      }
    }
  }
}
```

#### 8.2.3 预览图完整 URL

```
http://127.0.0.1:8787/api/v1/workspaces/ws_71c42ecc4c3c/files/previews/seg_1.jpg
```

#### 8.2.4 说明

预览样式写入 workspace 后，`compose/run` **自动沿用**，无需在合成请求中再传字幕参数。仅生成一张静态预览图，不影响成片各句画面。

---

## 9. 字幕字体列表 API

### 9.1 API 接口

```
GET http://127.0.0.1:8787/api/v1/subtitle/fonts
```

### 9.2 Request 参数

无。

### 9.3 Response 响应参数

返回可用字体列表（含别名映射）：每项 `name` 用于展示并传给 `subtitle_style.font_name`，`ass_font_name` 为解析后的 ASS 字体名。常见：思源黑体、迷茫体、拼搏体、文楷 等。

---

## 10. 视频合成 API（异步任务）

### 10.1 提交合成任务

#### 10.1.1 API 接口

```
POST http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/compose/run
```

#### 10.1.2 前置条件

`split_ready`、`audio_confirmed`、`media_bound` 均为 true。

#### 10.1.3 Request Body 请求参数

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| `code` | string | **是** | - | 租户编码，用于 OSS 路径 |
| `id` | string | **是** | - | 用户 ID，用于 OSS 路径 |
| `subtitle_mode` | string | 否 | `hard` | `hard`=硬字幕烧录；`soft`=软字幕轨 |
| `reuse_intermediates` | boolean | 否 | true | 复用 clip 中间产物 |
| `cleanup_scratch_on_success` | boolean | 否 | true | 成功后清理本次 compose scratch |

**OSS 路径规则：** `prod/ImagesVideosText2Video/{code}-{id}/{YYYY-MM-DD}/{job_id}.mp4`（及同前缀 `.wav`、`.log`）。

#### 10.1.4 Request 示例（实测）

```json
{
  "code": "e2e",
  "id": "noodle",
  "subtitle_mode": "hard",
  "reuse_intermediates": true,
  "cleanup_scratch_on_success": true
}
```

#### 10.1.5 成功响应（提交）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_3385c4703e1d",
    "status": "queued"
  }
}
```

#### 10.1.6 互斥说明

同一 workspace 同时仅允许一个进行中的 compose Job；重复提交返回 `40902 compose_job_active`。

### 10.2 查询合成任务状态

#### 10.2.1 API 接口

```
GET http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/compose/jobs/{job_id}
```

#### 10.2.2 成功响应（实测）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_b90e677e7fd4",
    "status": "done",
    "progress": { "phase": "finalize", "percent": 100, "message": "完成" },
    "result": {
      "output_video_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.mp4",
      "output_video_object_key": "prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.mp4",
      "output_audio_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.wav",
      "output_audio_object_key": "prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.wav",
      "log_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.log",
      "log_object_key": "prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.log",
      "total_seconds": 55.04,
      "durations_sec": [4.005488, 2.5078, "..."],
      "gaps_sec": [0.15, 0.35, "..."],
      "intermediates_cleaned": true,
      "workspace_deleted": true
    }
  }
}
```

#### 10.2.3 `result` 字段说明（`status=done`）

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `output_video_url` | string | 成片 HTTPS 地址（OSS） |
| `output_video_object_key` | string | 成片 object key |
| `output_audio_url` | string | 完整合成音频 HTTPS 地址 |
| `output_audio_object_key` | string | 音频 object key |
| `log_url` | string | 任务日志 HTTPS 地址（可选） |
| `log_object_key` | string | 日志 object key |
| `total_seconds` | number | 成片时长（秒） |
| `workspace_deleted` | boolean | 固定 `true`（成功时 workspace 已删） |
| `intermediates_cleaned` | boolean | 是否清理 compose scratch |

#### 10.2.4 耗时参考

15 句本次合成约 45 秒。

### 10.3 取消合成任务

#### 10.3.1 API 接口

```
POST http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/compose/jobs/{job_id}/cancel
```

#### 10.3.2 说明

取消后会清理 scratch；**失败/取消时 workspace 保留**。若需再次合成且 workspace 仍在，可直接重试 `compose/run`；**成功后 workspace 已删，须新建 workspace 重跑全流程。

---

## 11. 工作区文件下载 API（过程预览）

> **成片交付不走本接口。** compose 成功后使用 §10.2 的 `output_video_url`（OSS）。

### 11.1 API 接口

```
GET http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}/files/{file_path}
```

### 11.2 Path 参数

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| `workspace_id` | string | 是 | 工作区 ID |
| `file_path` | string | 是 | 工作区内相对路径 |

### 11.3 允许的路径前缀

| 前缀 | 典型文件 | 说明 |
|------|----------|------|
| `audio/` | `1.wav` … `N.wav`、`master.wav` | 试听配音 |
| `subtitles/` | `subtitle.srt`、`subtitle.ass` | 字幕预览 |
| `source_media/` | 绑定后的素材 | 核对画面 |
| `previews/` | `seg_1.jpg` | 字幕预览图 |
| `deliverables/` | `output.mp4` | **仅 compose 成功前或历史 workspace**；OSS 模式下成功后 workspace 已删 |

### 11.4 过程文件 URL 示例

```
http://127.0.0.1:8787/api/v1/workspaces/ws_71c42ecc4c3c/files/previews/seg_1.jpg
http://127.0.0.1:8787/api/v1/workspaces/ws_71c42ecc4c3c/files/audio/active/master.wav
```

### 11.5 说明

- 各 Job `result` 中的 `master_audio_url`、`preview_image_url` 等多为 **相对路径**，拼接 Base URL 访问
- **成片**使用 compose Job 的 `output_video_url`（OSS HTTPS），勿拼接 `/files/deliverables/output.mp4`
- **不要在 Swagger Execute 中打开大视频**；浏览器新标签页或 `curl -L -O` 下载

---

## 12. 删除工作区 API

### 12.1 API 接口

```
DELETE http://127.0.0.1:8787/api/v1/workspaces/{workspace_id}
```

### 12.2 Query 参数

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| `force` | boolean | 否 | false | true 时先取消活跃 Job 再删除 |

### 12.3 说明

若存在进行中的 Job 且 `force=false`，返回 `40902 stage_job_active`。

---

## 13. 全局 Job 协调状态 API（维护接口）

排查 `GLOBAL_JOB_WORKERS` 排队、计数 desync 时使用。

### 13.1 API 接口

```
GET http://127.0.0.1:8787/api/v1/maintenance/coord-status
```

鉴权规则同 §14.2（purge-workspaces）：配置了 `MAINTENANCE_API_TOKEN` 时须 `X-Maintenance-Token`。

### 13.2 Response 关键字段

| 字段 | 说明 |
|------|------|
| `global_job_workers` | 当前全机 Job 并发上限 |
| `coord_state.running` | 文件锁内记录的 running 计数 |
| `coord_state.waiting` | 等待槽位的 job 列表 |
| `jobs_on_disk.running_count` | 磁盘上 status=running 的 Job 数 |
| `jobs_on_disk.queued_count` | 磁盘上 status=queued 的 Job 数 |
| `desync` | `true` 表示 coord 计数与磁盘扫描不一致（重启后应 reconcile） |

### 13.3 curl 示例

```bash
curl http://127.0.0.1:8787/api/v1/maintenance/coord-status
```

---

## 14. 清理过期工作区 API（维护接口）

按 TTL 策略扫描 `workspaces/` 目录，删除过期工作区。适用于运维定期清理磁盘，**不属于业务流水线必需步骤**。

> **与 compose 成功的关系：** compose 成功并上传 OSS 后，API **已自动删除**该 workspace，通常不会留下 `composed` 状态目录。本接口主要清理 **空 workspace**、**长期未完成（abandoned）**、**compose/OSS 失败遗留** 以及 **历史版本未走 OSS 删除逻辑** 的目录。

> API 启动时也会自动执行一次 purge（可通过环境变量 `WORKSPACE_STARTUP_PURGE=0` 关闭）。

### 14.1 API 接口

```
POST http://127.0.0.1:8787/api/v1/maintenance/purge-workspaces
```

### 14.2 鉴权

| 场景 | 请求头 |
|------|--------|
| 未配置 `MAINTENANCE_API_TOKEN` | 无需鉴权头 |
| 已配置 `MAINTENANCE_API_TOKEN` | 必须传 `X-Maintenance-Token: <token>` |

鉴权失败返回 HTTP 401，`code=40101`，`message=maintenance_token_invalid`。

Swagger 仅在服务已配置 `MAINTENANCE_API_TOKEN` 时才显示该请求头参数。

### 14.3 Request Body 请求参数

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| `dry_run` | boolean | 否 | true | true 时仅预览候选，不实际删除 |
| `max_delete` | integer | 否 | 100 | 单次最多删除（或预览）数量，范围 0～1000 |
| `include_composed` | boolean | 否 | false | false 时不删除已合成（composed）工作区 |
| `policy.ttl_empty_days` | number | 否 | 3 | 空工作区保留天数 |
| `policy.ttl_abandoned_days` | number | 否 | 7 | 未完成工作区保留天数 |
| `policy.ttl_composed_days` | number | 否 | 30 | 已合成工作区保留天数（`include_composed=true` 时生效） |
| `policy.grace_hours` | number | 否 | 24 | 新建工作区 grace 期（小时内不删） |

默认 TTL 也可通过环境变量覆盖：`WORKSPACE_TTL_EMPTY_DAYS`、`WORKSPACE_TTL_ABANDONED_DAYS`、`WORKSPACE_TTL_COMPOSED_DAYS`、`WORKSPACE_GRACE_HOURS`。

### 14.4 Request 示例（预览，不删除）

```json
{
  "dry_run": true,
  "max_delete": 50,
  "include_composed": false,
  "policy": {
    "ttl_empty_days": 3,
    "ttl_abandoned_days": 7,
    "ttl_composed_days": 30,
    "grace_hours": 24
  }
}
```

### 14.5 Request 示例（实际删除）

```json
{
  "dry_run": false,
  "max_delete": 10,
  "include_composed": false
}
```

### 14.6 Response 响应参数

| 参数名 | 类型 | 描述 |
|--------|------|------|
| `data.dry_run` | boolean | 是否为预览模式 |
| `data.policy` | object | 实际使用的 TTL 策略 |
| `data.scanned` | integer | 扫描到的工作区总数 |
| `data.eligible` | integer | 符合删除条件的工作区数 |
| `data.deleted` | integer | 本次删除（或 dry_run 下将删除）的数量 |
| `data.deleted_ids` | array | 工作区 ID 列表 |
| `data.skipped` | object | 跳过原因统计（pinned、active_job、within_grace、not_expired 等） |
| `data.candidates` | array | 候选工作区详情（最多 50 条） |
| `data.all_evaluated` | array | dry_run=true 时返回全部评估结果 |

### 14.7 成功响应（dry_run 预览）

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "dry_run": true,
    "policy": {
      "ttl_empty_days": 3,
      "ttl_abandoned_days": 7,
      "ttl_composed_days": 30,
      "grace_hours": 24
    },
    "scanned": 5,
    "eligible": 2,
    "deleted": 2,
    "deleted_ids": ["ws_old001", "ws_old002"],
    "skipped": {
      "pinned": 0,
      "active_job": 1,
      "within_grace": 0,
      "not_expired": 2,
      "state_excluded": 0,
      "max_delete_reached": 0
    },
    "candidates": []
  }
}
```

### 14.8 curl 示例

```bash
# 预览（默认 dry_run=true）
curl -X POST "http://127.0.0.1:8787/api/v1/maintenance/purge-workspaces" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true, "max_delete": 50, "include_composed": false}'

# 实际删除（若配置了 MAINTENANCE_API_TOKEN，须加鉴权头）
curl -X POST "http://127.0.0.1:8787/api/v1/maintenance/purge-workspaces" \
  -H "Content-Type: application/json" \
  -H "X-Maintenance-Token: your-secret-token" \
  -d '{"dry_run": false, "max_delete": 10, "include_composed": false}'
```

### 14.9 CLI 等价命令

也可在服务器上直接执行（不经过 HTTP）：

```bash
PYTHONPATH=src python3 -m videoaudiotext.api.maintenance purge-workspaces --dry-run
PYTHONPATH=src python3 -m videoaudiotext.api.maintenance purge-workspaces --execute --max-delete 10
```

### 14.10 说明

- **钉住工作区**：环境变量 `WORKSPACE_PIN=ws_xxx,ws_yyy` 中的 ID 不会被删除
- **有活跃 Job 的工作区**：跳过，计入 `skipped.active_job`
- **24h grace**：新建 workspace 在 `grace_hours` 内不会被 purge（`skip_reason: within_grace`）；当天测试残留请用 `DELETE /workspaces/{id}` 立即清理
- **建议流程**：先 `dry_run: true` 确认 `deleted_ids`，再 `dry_run: false` 执行
- Docker 部署详见 [DOCKER_OPS.md](../DOCKER_OPS.md)

---

## 附录 A：常见错误码

| HTTP | code | message | 含义与处理 |
|------|------|---------|------------|
| 400 | 40001 | `validation_error` / `text is required` | 参数校验失败；检查 JSON 格式、`y_offset` 范围、`text` 是否单行 |
| 400 | 40003 | `audio_not_confirmed` | 未执行 `audio/confirm` |
| 400 | 40004 | `split_not_ready` 等 | 前置阶段未完成 |
| 409 | 40901 | `text_digest mismatch` | digest 或 text 与 split 不一致 |
| 409 | 40902 | `*_job_active` | 同阶段 Job 进行中；轮询或 cancel 后重试 |
| 409 | 40902 | `compose_job_active` | 合成 Job 进行中 |
| 401 | 40101 | `maintenance_token_invalid` | `purge-workspaces` 鉴权失败 |
| 404 | 40401 | `workspace not found` / `job not found` | 工作区或 Job 不存在 |
| 503 | 50301 | `health_check_failed` | ffmpeg/ffprobe 不可用 |

---

## 附录 B：Swagger Failed to fetch 排查

**现象**：Swagger 显示 Undocumented / Failed to fetch（含 CORS、Network Failure 提示）

**原因**：通常为 **API 进程未运行** 或 **浏览器无法访问 localhost:8787**（远程开发未做端口转发），并非 CORS 问题。

**处理**：

1. `curl http://127.0.0.1:8787/api/v1/health` 确认服务可达
2. 后台常驻：`nohup .venv/bin/python -m videoaudiotext.api > /tmp/videoaudiotext-api.log 2>&1 &`（须 `PYTHONPATH=src` 且 venv 含 oss2）
3. 远程环境配置 Cursor/SSH 端口转发 8787
4. `/run` 接口成功只返回 `job_id`，须再轮询 `jobs/{job_id}` 获取完整 `result`

---

## 附录 C：本次联调实测摘要

### C.1 2026-06-25 端到端（OSS 成片，推荐参照）

| 项目 | 值 |
|------|-----|
| 日期 | 2026-06-25 |
| workspace_id | `ws_87fae3579d67`（compose 后已删除） |
| 文案 | 深夜面馆（单行） |
| segment_count | 13（LLM 分句，每次运行可能略有差异） |
| text_digest | `3732aa0fbf5cc813133b6664db384479` |
| revision_id | `rev_953ccc793c2c` |
| compose job_id | `job_b90e677e7fd4` |
| compose `code` / `id` | `e2e` / `noodle` |
| 成片时长 | 55.04 s |
| 成片大小 | 27.6 MB（本地下载验证） |
| OSS 成片 | [`…/job_b90e677e7fd4.mp4`](https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.mp4) |
| 字幕样式 | 未调 preview，使用默认样式 |
| split 耗时 | ~10 s |
| audio/process 耗时 | ~15 s |
| media/bind 耗时 | ~5 min |
| compose + OSS 耗时 | ~60 s |
| **全流程合计** | **~5.5 min** |
| 复现脚本 | `scripts/e2e_noodle_compose.py` |

### C.2 2026-06-24 参考（本地 workspace · 15 句 · 含 preview）

| 项目 | 值 |
|------|-----|
| workspace_id | `ws_71c42ecc4c3c` |
| segment_count | 15 |
| text_digest | `b8b58d28b62dfec8d0c62f4da5523a57` |
| preview job_id | `job_448a16c853ce` |
| compose job_id | `job_3385c4703e1d` |
| 成片时长 | 66.64 s |
| 字幕样式 | 思源黑体 · font_scale 1.2 · y_offset -1（下移 1 档） |
| 成片交付 | 本地 `/files/deliverables/output.mp4` |
| compose 后 workspace | 保留 |
| media/bind 耗时 | 约 4 分钟（15 段视频） |
| compose 耗时 | 约 45 秒 |

详细 JSON 模板见 [api-flow-b-web-test-examples.json](./api-flow-b-web-test-examples.json)。
