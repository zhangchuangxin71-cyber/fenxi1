# Flow B FastAPI 服务接口文档 — JPG 快速联调 Walkthrough

> **版本**：API **v3.1**（五阶段独立 · 无 workspace）  
> **Base URL（主服务）**：`http://127.0.0.1:8787`  
> **本文联调 Base URL**：`http://127.0.0.1:8797`（**隔离实例**，不影响 8787）  
> **交互式文档**：Swagger [`/docs`](http://127.0.0.1:8797/docs) · ReDoc [`/redoc`](http://127.0.0.1:8797/redoc)  
> **本文来源**：2026-06-25 端到端实测（深夜面馆 · **14 句** · JPG 占位素材 · 跳过 bind · OSS 输出）  
> **一键脚本**：[`scripts/run_isolated_jpg_e2e.sh`](../scripts/run_isolated_jpg_e2e.sh) · [`scripts/e2e_jpg_compose_isolated.py`](../scripts/e2e_jpg_compose_isolated.py)  
> **相关文档**：[通用 Walkthrough](./api-flow-b-fastapi-walkthrough.md) · [接口规范 v3.1](./api-flow-b-fastapi-service-api.md) · [文档索引](./README.md)

---

## 1. 本文解决什么问题

生产联调若每句使用 **~105 MB 的 mp4**（`source_media/N.mp4`），仅 `media/bind` 拉取 15 段就可能耗时 **1 小时**，且易因 API 重启变为 `interrupted`。

**本文路径**用一张 **7 KB 的 JPG** 作为每句画面素材，并 **跳过 `media/bind`**，直接 `compose/run`，在 **~2 分钟**内验证整条 v3.1 流水线：

```
split/run → audio/process/run → compose/run → output_video_url（OSS）
```

| 对比项 | MP4 全量联调 | 本文 JPG 快速联调 |
|--------|--------------|-------------------|
| 素材 | `source_media/N.mp4`（~105 MB/段） | 每句共用 1 张 JPG（~7 KB） |
| bind | 建议执行（~5～60 min） | **跳过**（compose 内拉取 JPG） |
| 全流程 | ~5.5 min～1 h+ | **~2 min**（14 句实测） |
| 成片大小 | ~27 MB | **~1.1 MB**（静态图 Ken Burns） |
| 适用 | 真实画面/裁切/转场验收 | API 通路、OSS、字幕、合成逻辑 |

---

## 2. 服务架构（v3.1）

### 2.1 与 v2.x workspace 模型的区别

| v2.x（旧 walkthrough 部分章节） | v3.1（本文） |
|--------------------------------|--------------|
| `POST /workspaces` 创建工作区 | **无** workspace；每步 `POST .../run` 自包含 |
| `GET /workspaces/{id}/split/jobs/{job_id}` | `GET /api/v1/split/tasks/{task_id}` |
| `PUT /audio/confirm` 锁定配音 | **已移除**；下游 body 直接传 OSS 配音 URL |
| 成片可能走 `/files/deliverables/output.mp4` | 成片 **仅** `result.output_video_url`（OSS HTTPS） |
| 标识符 `job_id` / `workspace_id` | 统一 **`task_id`**（格式 `task_xxxxxxxxxxxx`） |

### 2.2 数据流

```
浏览器 / 脚本                         阿里云 OSS
     │                                      │
     │  JSON API（8797 隔离实例）              │  输入：audio/N.wav、JPG 测试图
     ▼                                      │  输出：prod/ImagesVideosText2Video/…/*.mp4
┌──────────────────────────────────────────────────────┐
│  FastAPI  :8797                                       │
│  POST /split/run                                      │
│  POST /audio/process/run  → 上传 master/字幕/分段 wav   │
│  POST /compose/run        → 拉 JPG + 合成 + 上传 MP4   │
└──────────────────────────────────────────────────────┘
     │
     ▼
jobs_isolated_jpg_test/          本地临时（JOB_EPHEMERAL=1，完成后删目录）
jobs_isolated_jpg_test/_job_archive/   小 JSON 归档（可继续 poll 历史 task）
```

### 2.3 核心分工

| 谁做 | 做什么 |
|------|--------|
| **API** | LLM 分句、拉取 URL 音频/JPG、生成 master + 字幕、合成成片、**上传 OSS** |
| **业务侧 / 联调脚本** | 准备每句 wav（OSS URL）；画面联调用 **同一张 JPG URL**；成片读 `output_video_url` |
| **OSS 凭证** | **输入**：公网/预签名 HTTPS，API 做 GET；**输出**：`.env` 配置 `ALIYUN_OSS_*` |

### 2.4 异步 Task 模式

1. **`POST .../{stage}/run`** — 立即返回 `task_id`（通常 &lt; 1 s）
2. **`GET .../{stage}/tasks/{task_id}`** — 轮询直到 `status` 为 `done` / `failed` / `cancelled` / `interrupted`
3. `status: done` 时，业务字段在 **`data.result`**

> **Swagger 注意：** `/run` 成功只表示 Task 已入队；**必须**再 poll 对应 `tasks/{task_id}`。

### 2.5 统一响应格式

```json
{
  "code": 0,
  "message": "ok",
  "data": { }
}
```

- `code: 0` 成功；非 0 见 §9
- 错误响应 `data.request_id` 与响应头 `X-Request-Id` 一致

---

## 3. 启动隔离服务（不影响 8787）

主服务若在 **8787** 运行（例如正在跑 `media/bind` 大视频任务），请用 **8797 + 独立 JOBS_ROOT** 做本文联调。

### 3.1 一键（推荐）

```bash
cd /path/to/videoaudiotext
./scripts/run_isolated_jpg_e2e.sh
```

脚本会：

1. 检测 **8797** 未被占用（**不**动 8787）
2. `source .env` 后 **强制** `API_PORT=8797`、`JOBS_ROOT=./jobs_isolated_jpg_test`
3. 启动 API → 跑 `e2e_jpg_compose_isolated.py` → 下载 `output_e2e_jpg_isolated.mp4` → 停止 8797 进程

日志：

| 文件 | 内容 |
|------|------|
| `/tmp/videoaudiotext-api-8797.log` | 隔离 API 标准输出 |
| `/tmp/e2e_jpg_isolated.log` | e2e 脚本输出 |

### 3.2 手动常驻（Swagger 手测）

**终端 A — 隔离 API：**

```bash
cd /path/to/videoaudiotext
set -a && source .env && set +a

export API_PORT=8797
export JOBS_ROOT="$PWD/jobs_isolated_jpg_test"
export PYTHONPATH=src
export GLOBAL_JOB_WORKERS=2

mkdir -p "$JOBS_ROOT"
.venv/bin/python -m videoaudiotext.api
# 期望：Uvicorn running on http://0.0.0.0:8797
```

> **注意：** `.env` 里若有 `API_PORT=8787`，必须在 `source .env` **之后**再 `export API_PORT=8797`，否则仍会占用 8787。

**终端 B — 健康检查：**

```bash
curl -s http://127.0.0.1:8797/api/v1/health | jq .
curl -s http://127.0.0.1:8787/api/v1/health | jq .   # 主服务应仍正常
```

### 3.3 环境变量（隔离实例）

| 变量 | 隔离联调值 | 说明 |
|------|------------|------|
| `API_PORT` | `8797` | 与主服务错开 |
| `JOBS_ROOT` | `./jobs_isolated_jpg_test` | 不与 `./jobs` 混用 |
| `GLOBAL_JOB_WORKERS` | `2`（建议） | 降低与主服务争抢 CPU |
| `JOB_EPHEMERAL` | `1`（默认） | 阶段完成后删本地目录 |
| `ARK_API_KEY` / `ALIYUN_OSS_*` | 同 `.env` | split 与 OSS 上传必需 |

---

## 4. OSS 与测试素材

### 4.1 默认 JPG（本文全程使用）

| 项 | 值 |
|----|-----|
| URL | `https://example-bucket.oss-cn-region.aliyuncs.com/oss%E6%B5%8B%E8%AF%95%E6%96%87%E4%BB%B6/s0000000.jpg` |
| 大小 | **6948 字节**（约 7 KB） |
| `media.type` | **`image`**（必填；勿写 `video`） |
| 用法 | **每句 `segments[].media.url` 相同**；图片按句时长 Ken Burns 展示 |

### 4.2 音频输入（仍用 OSS wav）

Bucket **`example-bucket`**（深圳）：

| 用途 | URL 模板 |
|------|----------|
| 第 N 句音频 | `https://example-bucket.oss-cn-region.aliyuncs.com/audio/N.wav` |

联调脚本规则：OSS 上仅有 `audio/1.wav` … `audio/14.wav` 时，**第 15 句及以后**用 `audio/14.wav` 替代（见脚本 `oss_audio_index()`）。

### 4.3 成片输出（compose 完成后）

```
https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/{code}-{id}/{YYYY-MM-DD}/{task_id}.mp4
```

本文实测：

| 字段 | 值 |
|------|-----|
| `code` | `e2e-jpg` |
| `id` | `isolated-test` |
| `task_id` | `task_a9784b3efe01` |
| `output_video_url` | 见 §6 步骤 3 |

---

## 5. 接口一览（本文用到的）

| 步骤 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 0 | GET | `/api/v1/health` | 健康检查 |
| 1a | POST | `/api/v1/split/run` | 提交分句 Task |
| 1b | GET | `/api/v1/split/tasks/{task_id}` | 轮询分句 |
| 2a | POST | `/api/v1/audio/process/run` | 提交配音 Task（须 `code` + `id`） |
| 2b | GET | `/api/v1/audio/process/tasks/{task_id}` | 轮询配音 |
| 3a | POST | `/api/v1/compose/run` | 提交合成（**跳过 bind**；须 `code` + `id`） |
| 3b | GET | `/api/v1/compose/tasks/{task_id}` | 轮询合成；**成片在 `result.output_video_url`** |
| — | POST | `/api/v1/tasks/{task_id}/cancel` | 取消 queued/running Task |

**本文未使用（可选）：**

| 方法 | 路径 | 何时需要 |
|------|------|----------|
| POST | `/api/v1/visual/preview/run` | 调字幕样式/分辨率预览 |
| GET | `/api/v1/tasks/{task_id}/files/{path}` | 过程文件调试；**非成片交付** |
| GET | `/api/v1/subtitle/fonts` | 预览/compose 自定义字体 |

### 5.1 流水线中需保存的变量

| 变量 | 来源 | 用途 |
|------|------|------|
| `TEXT_DIGEST` | split `result.text_digest` | audio / compose 可选校验 |
| `SPLIT_SEGMENTS` | split `result.segments` | 构造 audio 与 compose 的 `segments` |
| `AUDIO_BLOCK` | audio `result` 组装 | compose 的 `audio` 对象 |
| `COMPOSE_CODE` | compose body `"code"` | OSS 路径；实测 `e2e-jpg` |
| `COMPOSE_USER_ID` | compose body `"id"` | OSS 路径；实测 `isolated-test` |
| `OUTPUT_VIDEO_URL` | compose `result.output_video_url` | **成片 HTTPS** |

### 5.2 轮询与超时（14 句 · JPG 实测）

| 阶段 | 轮询 URL | 建议间隔 | 本文实测耗时 |
|------|----------|----------|--------------|
| split | `GET .../split/tasks/{id}` | 2～3 s | **~25 s** |
| audio/process | `GET .../audio/process/tasks/{id}` | 3 s | **~25 s** |
| compose | `GET .../compose/tasks/{id}` | 5 s | **~70 s** |
| **合计** | | | **~121 s（约 2 min）** |

### 5.3 Task 状态

| `status` | 含义 |
|----------|------|
| `queued` | 已入队 |
| `running` | 执行中；看 `progress.message` |
| `done` | 成功；读 `data.result` |
| `failed` | 失败；读 `data.error` |
| `cancelled` | 已取消 |
| `interrupted` | **服务重启**导致中断；须重新 `POST .../run`（不会续传） |

---

## 6. 逐步操作（2026-06-25 JPG 隔离实测）

### 6.0 实测概览

| 项目 | 值 |
|------|-----|
| 日期 | 2026-06-25 |
| API Base | `http://127.0.0.1:8797`（隔离；8787 未重启） |
| `JOBS_ROOT` | `jobs_isolated_jpg_test/` |
| 文案 | 深夜面馆（单行，见下方） |
| `segment_count` | **14**（LLM 分句，每次运行可能 13～16） |
| `text_digest` | `ae919e3ab47b4502c306dcf250820a10` |
| 素材 | 每句 JPG `s0000000.jpg`，`type: image` |
| compose `code` / `id` | `e2e-jpg` / `isolated-test` |
| compose `task_id` | `task_a9784b3efe01` |
| 成片时长 | **60.656 s** |
| 成片大小 | **1.1 MB**（本地下载 `output_e2e_jpg_isolated.mp4`） |
| 字幕 | 默认硬字幕（未调 preview） |

**口播全文（`text` 必须单行）：**

```
夜深之后，城市褪去了白日的喧嚣，穿行在空旷的街道上，抬眼望去，街角的面馆亮着一盏暖融融的灯。放眼望去，小店不大，却在冷清的夜色里显得格外温馨，成了晚归之人的一处避风港。细看，大锅里的骨汤咕嘟咕嘟持续翻滚，手工面条下入锅中，在沸水里轻轻起伏。一碗碗热气腾腾的面食被端上桌，升腾的白雾裹着鲜香，瞬间驱散了深夜的寒意。加班至深夜的打工人、独自在外打拼的异乡人安静坐在桌前，低头嗦面、小口喝汤。一口热食下肚，一路奔波的疲惫、独处异乡的孤单，都在这一碗面的温度里慢慢消散。
```

---

### 步骤 0：健康检查

```http
GET /api/v1/health
```

```bash
curl -s http://127.0.0.1:8797/api/v1/health
```

**期望：**

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

---

### 步骤 1：LLM 分句

#### 1a — 提交 Task

```http
POST /api/v1/split/run
Content-Type: application/json
```

```json
{
  "text": "夜深之后，城市褪去了白日的喧嚣，穿行在空旷的街道上，抬眼望去，街角的面馆亮着一盏暖融融的灯。放眼望去，小店不大，却在冷清的夜色里显得格外温馨，成了晚归之人的一处避风港。细看，大锅里的骨汤咕嘟咕嘟持续翻滚，手工面条下入锅中，在沸水里轻轻起伏。一碗碗热气腾腾的面食被端上桌，升腾的白雾裹着鲜香，瞬间驱散了深夜的寒意。加班至深夜的打工人、独自在外打拼的异乡人安静坐在桌前，低头嗦面、小口喝汤。一口热食下肚，一路奔波的疲惫、独处异乡的孤单，都在这一碗面的温度里慢慢消散。",
  "include_ai_prompts": false
}
```

**curl：**

```bash
curl -s -X POST http://127.0.0.1:8797/api/v1/split/run \
  -H 'Content-Type: application/json' \
  -d '{"text":"夜深之后，城市褪去了白日的喧嚣，穿行在空旷的街道上，抬眼望去，街角的面馆亮着一盏暖融融的灯。放眼望去，小店不大，却在冷清的夜色里显得格外温馨，成了晚归之人的一处避风港。细看，大锅里的骨汤咕嘟咕嘟持续翻滚，手工面条下入锅中，在沸水里轻轻起伏。一碗碗热气腾腾的面食被端上桌，升腾的白雾裹着鲜香，瞬间驱散了深夜的寒意。加班至深夜的打工人、独自在外打拼的异乡人安静坐在桌前，低头嗦面、小口喝汤。一口热食下肚，一路奔波的疲惫、独处异乡的孤单，都在这一碗面的温度里慢慢消散。","include_ai_prompts":false}'
```

**立即响应：**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "task_id": "task_f36ad459a07a",
    "kind": "split",
    "status": "queued"
  }
}
```

#### 1b — 轮询

```http
GET /api/v1/split/tasks/{task_id}
```

每 **2～3 s** 轮询，直到 `status: done`。

**实测进度：**

```
queued → running 10% LLM 分句中… → done 100% 完成
```

**`status: done` 时 `result` 关键字段：**

```json
{
  "text_digest": "ae919e3ab47b4502c306dcf250820a10",
  "split_mode_used": "llm",
  "segment_count": 14,
  "segments": [
    { "index": 1, "text": "夜深之后，城市褪去了白日的喧嚣，", "visual": true },
    { "index": 2, "text": "穿行在空旷的街道上，", "visual": true }
  ],
  "warnings": []
}
```

> **重要：** 后续 audio / compose 中每句 `text` 必须与 `segments` **逐字一致**；LLM 每次分句数量可能变化，以本次 `segment_count` 为准。

---

### 步骤 2：配音处理

#### 2a — 提交 Task

```http
POST /api/v1/audio/process/run
Content-Type: application/json
```

按 split 的 **14 句**构造 `segments`；每句 `audio.url` 指向 OSS wav。须传 **`code` + `id`**（OSS 输出路径）。

```json
{
  "code": "e2e-jpg",
  "id": "isolated-test",
  "label": "e2e-jpg",
  "text_digest": "ae919e3ab47b4502c306dcf250820a10",
  "force_refresh": true,
  "segments": [
    {
      "index": 1,
      "text": "夜深之后，城市褪去了白日的喧嚣，",
      "audio": { "url": "https://example-bucket.oss-cn-region.aliyuncs.com/audio/1.wav" }
    },
    {
      "index": 2,
      "text": "穿行在空旷的街道上，",
      "audio": { "url": "https://example-bucket.oss-cn-region.aliyuncs.com/audio/2.wav" }
    }
  ]
}
```

（完整 14 句见 [`scripts/e2e_jpg_compose_isolated.py`](../scripts/e2e_jpg_compose_isolated.py)。）

| 字段 | 必填 | 说明 |
|------|------|------|
| `code` | 是 | OSS 租户编码 |
| `id` | 是 | OSS 用户 ID |
| `label` | 是 | 配音版本标签 |
| `text_digest` | 否 | 与 split 不一致 → **40901** |
| `segments[].audio.url` | 是 | 该句原始 wav HTTPS URL |
| `force_refresh` | 否 | 联调建议 `true` |

#### 2b — 轮询

```http
GET /api/v1/audio/process/tasks/{task_id}
```

**实测进度：**

```
queued → running 45% 处理配音 7/14 → 88% 拼接 master.wav → 99% 归档中… → done
```

**`status: done` 时 `result` 关键字段：**

```json
{
  "revision_id": "rev_xxxxxxxxxxxx",
  "label": "e2e-jpg",
  "audio_config_digest": "bab6d00ca28dcb6dbd463a119fe511a9",
  "segment_count": 14,
  "segments": [
    {
      "index": 1,
      "text": "夜深之后，城市褪去了白日的喧嚣，",
      "duration_sec": 4.005488,
      "clip_duration_sec": 4.16
    }
  ],
  "gaps_sec": [0.15, 0.35, 0.15, 0.35, 0.15, 0.35, 0.15, 0.15, 0.35, 0.15, 0.15, 0.35, 0.15, 0.0],
  "master_audio_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_xxx_master.wav",
  "subtitle_srt_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_xxx_subtitle.srt",
  "subtitle_ass_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_xxx_subtitle.ass",
  "segment_urls": {
    "1": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/.../task_xxx_seg1.wav"
  }
}
```

#### 2c — 组装 `audio` 块（供 compose 使用）

从 poll 的 `result` 复制以下字段到 compose body 的 **`audio`** 对象：

```json
{
  "master_audio_url": "<result.master_audio_url>",
  "subtitle_srt_url": "<result.subtitle_srt_url>",
  "subtitle_ass_url": "<result.subtitle_ass_url>",
  "revision_id": "<result.revision_id>",
  "audio_config_digest": "<result.audio_config_digest>",
  "segments": [
    {
      "index": 1,
      "text": "夜深之后，城市褪去了白日的喧嚣，",
      "duration_sec": 4.005488,
      "clip_duration_sec": 4.16
    }
  ],
  "segment_urls": { "1": "https://..." }
}
```

> v3.1 **无** `PUT /audio/confirm`；无需确认步骤。

---

### 步骤 3：视频合成（跳过 media/bind）

compose 会在内部 **GET 拉取** 每句 JPG，无需先调 `media/bind/run`。

#### 3a — 提交 Task

```http
POST /api/v1/compose/run
Content-Type: application/json
```

```json
{
  "code": "e2e-jpg",
  "id": "isolated-test",
  "text_digest": "ae919e3ab47b4502c306dcf250820a10",
  "subtitle_mode": "hard",
  "reuse_intermediates": true,
  "cleanup_scratch_on_success": true,
  "audio": {
    "master_audio_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_xxx_master.wav",
    "subtitle_srt_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_xxx_subtitle.srt",
    "subtitle_ass_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_xxx_subtitle.ass",
    "revision_id": "rev_xxxxxxxxxxxx",
    "audio_config_digest": "bab6d00ca28dcb6dbd463a119fe511a9",
    "segments": [
      {
        "index": 1,
        "text": "夜深之后，城市褪去了白日的喧嚣，",
        "duration_sec": 4.005488,
        "clip_duration_sec": 4.16
      }
    ]
  },
  "segments": [
    {
      "index": 1,
      "text": "夜深之后，城市褪去了白日的喧嚣，",
      "media": {
        "url": "https://example-bucket.oss-cn-region.aliyuncs.com/oss%E6%B5%8B%E8%AF%95%E6%96%87%E4%BB%B6/s0000000.jpg",
        "type": "image"
      }
    },
    {
      "index": 2,
      "text": "穿行在空旷的街道上，",
      "media": {
        "url": "https://example-bucket.oss-cn-region.aliyuncs.com/oss%E6%B5%8B%E8%AF%95%E6%96%87%E4%BB%B6/s0000000.jpg",
        "type": "image"
      }
    }
  ]
}
```

**素材字段说明：**

| 字段 | 说明 |
|------|------|
| `media.url` | JPG/MP4 的 HTTPS URL；本文 14 句 **共用同一 JPG** |
| `media.type` | `image` 或 `video`；JPG 必须为 **`image`** |
| `media.start_sec` | 仅 **`video`** 有效；图片可省略 |
| `audio` | 步骤 2 组装的完整块（含 OSS master/字幕 URL + 各句时长） |
| `code` / `id` | OSS 成片路径必填 |
| `subtitle_mode` | `hard`（默认）硬字幕烧录 |

#### 3b — 轮询

```http
GET /api/v1/compose/tasks/{task_id}
```

**实测进度：**

```
queued → running 10% 准备合成…
       → running 50% 画面叠化 xfade（音轨硬切）0.30s（14 段）
       → running 90% 上传 OSS…
       → done 100% 完成
```

**`status: done` 时完整 `result`（2026-06-25 实测）：**

```json
{
  "output_video_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_a9784b3efe01.mp4",
  "output_video_object_key": "prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_a9784b3efe01.mp4",
  "output_audio_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_a9784b3efe01.wav",
  "output_audio_object_key": "prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_a9784b3efe01.wav",
  "log_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_a9784b3efe01.log",
  "log_object_key": "prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_a9784b3efe01.log",
  "oss_prefix": "prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25",
  "total_seconds": 60.656,
  "durations_sec": [4.005488, 2.5078, 4.307324, 5.329478, 3.925488, 3.925488, 4.725283, 4.214444, 5.503175, 3.018639, 4.307324, 2.600658, 3.703605, 5.630862],
  "gaps_sec": [0.15, 0.35, 0.15, 0.35, 0.15, 0.35, 0.15, 0.15, 0.35, 0.15, 0.15, 0.35, 0.15, 0.0],
  "intermediates_cleaned": true,
  "job_context_deleted": true
}
```

| 交付字段 | 用途 |
|----------|------|
| **`output_video_url`** | **成片 MP4（业务主用）** |
| `output_audio_url` | 合成用完整音轨 |
| `log_url` | 任务日志 |
| `job_context_deleted: true` | 本地 job 目录已删；归档 JSON 在 `JOBS_ROOT/_job_archive/` |

---

### 步骤 4：下载成片

```bash
curl -L -o output_e2e_jpg_isolated.mp4 \
  "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-jpg-isolated-test/2026-06-25/task_a9784b3efe01.mp4"
```

浏览器直接打开 `output_video_url` 即可播放（公网 Bucket）。

**勿依赖** `GET /api/v1/tasks/{task_id}/files/deliverables/output.mp4` — compose 成功后本地目录通常已删除。

---

## 7. 过程文件下载 API（非必需）

```http
GET /api/v1/tasks/{task_id}/files/{file_path}
```

允许前缀：`audio/`、`subtitles/`、`source_media/`、`previews/`、`uploads/`、`deliverables/`。

| 场景 | 是否需要 |
|------|----------|
| 成片交付 | **否** — 用 `output_video_url` |
| JPG 快速联调 | **否** — 素材已是 OSS URL |

Job 目录删除后，若归档中有 OSS 映射，可能 **302 重定向**到 OSS。

---

## 9. 错误码与排查

### 9.1 常见 HTTP / Job 错误

| HTTP | code | message | 处理 |
|------|------|---------|------|
| 400 | 40001 | 参数缺失/非法 | 检查 `text` 单行、`media.type`、必填字段 |
| 404 | 40401 | task not found | 确认 poll URL 阶段与 `task_id` 匹配 |
| 409 | 40901 | digest mismatch | 更新 `text_digest` / `audio_config_digest` |
| 502 | 50201 | 输入 URL 拉取失败 | 检查 OSS 公网可达、wav/jpg URL |
| 503 | 50301 | health_check_failed | 安装 ffmpeg、配置 LLM |
| — | 50003 | `service_interrupted` | API 重启；**重新 POST `/run`**，不会续传 |

### 9.2 FAQ

| 现象 | 原因 | 处理 |
|------|------|------|
| bind 跑很久 | 大 MP4 下载 | 联调改用 JPG + 跳过 bind |
| `status: interrupted` | 8787/8797 进程重启 | 对新 task 重新 POST；勿重复 poll 旧 task |
| 8797 起不来却占用 8787 | `source .env` 覆盖了 `API_PORT` | 在 source **之后** `export API_PORT=8797` |
| JPG 报错 | `type` 写成 `video` | 改为 `"type": "image"` |
| Swagger Failed to fetch | API 未启动或端口错 | `curl .../health` |
| 成片 404 | 用了 `/files/.../output.mp4` | 改用 `output_video_url` |

### 9.3 取消 Task

```http
POST /api/v1/tasks/{task_id}/cancel
```

仅 `queued` / `running` 可取消；`done` / `failed` / `interrupted` 不可。

---

## 10. 与 MP4 全量联调如何切换

| 步骤 | JPG 快速（本文） | MP4 生产向 |
|------|------------------|------------|
| 素材 URL | 共用 `s0000000.jpg` | `source_media/N.mp4` |
| `media.type` | `image` | `video` |
| bind | 跳过 | 建议执行（或 compose 直连） |
| 端口 | 8797 隔离（可选） | 8787 主服务 |
| 脚本 | `run_isolated_jpg_e2e.sh` | `e2e_noodle_compose_stateless.py` |
| 文档 | **本文** | [api-flow-b-fastapi-walkthrough.md](./api-flow-b-fastapi-walkthrough.md) |

---

## 11. 附录：完整 curl 流水线（变量版）

```bash
BASE=http://127.0.0.1:8797
JPG='https://example-bucket.oss-cn-region.aliyuncs.com/oss%E6%B5%8B%E8%AF%95%E6%96%87%E4%BB%B6/s0000000.jpg'
TEXT='夜深之后，城市褪去了白日的喧嚣，穿行在空旷的街道上，抬眼望去，街角的面馆亮着一盏暖融融的灯。放眼望去，小店不大，却在冷清的夜色里显得格外温馨，成了晚归之人的一处避风港。细看，大锅里的骨汤咕嘟咕嘟持续翻滚，手工面条下入锅中，在沸水里轻轻起伏。一碗碗热气腾腾的面食被端上桌，升腾的白雾裹着鲜香，瞬间驱散了深夜的寒意。加班至深夜的打工人、独自在外打拼的异乡人安静坐在桌前，低头嗦面、小口喝汤。一口热食下肚，一路奔波的疲惫、独处异乡的孤单，都在这一碗面的温度里慢慢消散。'

# 1) split
SPLIT_TASK=$(curl -s -X POST "$BASE/api/v1/split/run" \
  -H 'Content-Type: application/json' \
  -d "{\"text\":\"$TEXT\",\"include_ai_prompts\":false}" | jq -r '.data.task_id')
echo "split_task=$SPLIT_TASK"
# 轮询直至 done，提取 text_digest / segments（建议用 Python 脚本）

# 2) audio — 见 e2e_jpg_compose_isolated.py 构造 segments

# 3) compose — body 含 audio 块 + 每句 media.url=$JPG, type=image
```

生产环境请使用 [`scripts/e2e_jpg_compose_isolated.py`](../scripts/e2e_jpg_compose_isolated.py) 或 `./scripts/run_isolated_jpg_e2e.sh`，避免手写 JSON 出错。

---

## 12. 变更记录

| 日期 | 说明 |
|------|------|
| 2026-06-25 | 初版：JPG 隔离 e2e 实测；v3.1 task 路径；跳过 bind；8797 端口 |
