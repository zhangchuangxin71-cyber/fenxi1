# Flow B FastAPI 服务接口文档 — 联调 Walkthrough

> **版本**：API v2.30  
> **Base URL**：`http://127.0.0.1:8787`  
> **交互式文档**：Swagger [`/docs`](http://127.0.0.1:8787/docs) · ReDoc [`/redoc`](http://127.0.0.1:8787/redoc)  
> **本文来源**：2026-06-25 端到端实测（深夜面馆 · **13 句** · OSS 输入/输出 · 脚本 `scripts/e2e_noodle_compose.py`）  
> **历史参考**：2026-06-24 Swagger 逐步联调（14 句 · 本地 workspace，无 OSS 删除）  
> **相关文档**：[接口规范](./api-flow-b-fastapi-service-api.md) · [设计契约](./api-flow-b-production.md) · [文档索引](./README.md)

---

## 1. 服务做什么

Flow B API 提供 **文案分句 → 配音对齐 → 素材绑定 → 字幕预览 → 视频合成** 的 HTTP 流水线。每个任务对应一个 **workspace**（工作区），**compose 完成前**产物落盘在 `WORKSPACES_ROOT`；**compose 成功并上传 OSS 后，整个 workspace 会被删除**。

```
浏览器 / 业务前端                         阿里云 OSS
     │                                      │
     │  JSON API                            │  输入：audio/N.wav、source_media/N.mp4
     ▼                                      │  输出：prod/ImagesVideosText2Video/…/*.mp4
┌──────────────────────────────────────────────────────┐
│  FastAPI  :8787                                       │
│  split/run → audio/process/run → confirm →          │
│  media/bind/run → visual/preview/run（可选）→         │
│  compose/run → 上传 OSS（MP4 + 完整音频 + 日志）       │
└──────────────────────────────────────────────────────┘
     │
     ▼（compose 成功前）
workspaces/ws_xxx/   （config、audio、source_media …）
     │
     ▼（compose 成功后）
workspaces/_compose_results/{job_id}.json   （Job 归档，供轮询结果）
OSS prod/ImagesVideosText2Video/{code}-{id}/{date}/{job_id}.*
```

### 1.1 核心分工

| 谁做 | 做什么 |
|------|--------|
| **API** | LLM 分句、拉取 URL 音频/视频、生成 master + 字幕、合成成片、**上传 OSS 并删除 workspace** |
| **业务侧** | TTS/录音生成每句 wav、检索/生成每句视频，**上传 OSS 后把 HTTPS URL 传给 API**；成片从 compose Job 的 `result.output_video_url` 获取 |
| **OSS 凭证** | **输入**素材拉取：仅需公网/预签名 URL，API 做 HTTP GET；**输出**成片上传：须在 `.env` 配置 `ALIYUN_OSS_*`（见 `.env.example`） |

### 1.2 异步 Job 模式（v2.27）

分句、配音、素材绑定、画面预览、合成均为 **Job 异步执行**：

1. **`POST .../{stage}/run`** — 立即返回 `job_id`（通常 &lt; 1 秒）
2. **`GET .../{stage}/jobs/{job_id}`** — 轮询直到 `status` 为 `done` / `failed` / `cancelled`
3. `status: done` 时，业务字段在 **`data.result`** 中（如 `text_digest`、`segments`、`master_audio_url`）

> **Swagger 注意：** `/run` 成功只表示 Job 已入队，**不会**直接返回 `segments` 或成片 URL；必须再调对应的 `jobs/{job_id}` 查询。

### 1.3 统一响应格式

```json
{
  "code": 0,
  "message": "ok",
  "data": { }
}
```

- `code: 0` 成功；非 0 为业务/校验错误（见 §9）
- 错误响应 `data.request_id` 与响应头 `X-Request-Id` 一致，便于查日志

---

## 2. 启动服务

### 2.1 Docker（推荐）

```bash
cp .env.example .env    # 填写 ARK_API_KEY 等
mkdir -p workspaces fonts
sudo chown -R 1000:1000 workspaces

bash scripts/deploy.sh
```

详见 [DOCKER_OPS.md](../DOCKER_OPS.md)。

**并发调参（可选，`.env`）：**

| 变量 | 默认 | 说明 |
|------|------|------|
| `UVICORN_WORKERS` | `1` | uvicorn 进程数；`>1` 时启用跨进程文件锁（单机多 worker） |
| `GLOBAL_JOB_WORKERS` | `min(4, CPU核数)` | **全机**同时执行的 Job 数（所有 worker 共享） |
| `CLIP_BUILD_WORKERS` | `min(4, CPU核数)` | **全机** CLIP/ffmpeg 并行线程上限 |

8 核单机示例：`UVICORN_WORKERS=4`、`GLOBAL_JOB_WORKERS=4`、`CLIP_BUILD_WORKERS=4`。  
仅联调/低负载可保持默认 `UVICORN_WORKERS=1`。

### 2.2 本地 Python

```bash
cd /path/to/videoaudiotext

mkdir -p workspaces
export WORKSPACES_ROOT="$PWD/workspaces"
export API_PORT=8787

# .env 中配置 ARK_API_KEY、TEXT_SPLIT_MODE=llm、ALIYUN_OSS_* 等
set -a && source .env && set +a
PYTHONPATH=src .venv/bin/python -m videoaudiotext.api
```

看到 `Uvicorn running on http://0.0.0.0:8787` 即成功。

> **重要：**
> - 须加 `PYTHONPATH=src`，确保加载当前仓库代码。
> - 须用 **`.venv/bin/python`**（或 Docker）；系统裸 `python3` 若无 `oss2`，compose 会在 upload 阶段失败（`50002`）。
> - `.env` 须含 `ARK_API_KEY` 与 `ALIYUN_OSS_*`（成片上传）。

**后台常驻（避免联调中途进程退出）：**

```bash
cd /path/to/videoaudiotext
export WORKSPACES_ROOT="$PWD/workspaces" PYTHONPATH=src
set -a && source .env && set +a
nohup .venv/bin/python -m videoaudiotext.api > /tmp/videoaudiotext-api.log 2>&1 &
```

### 2.3 健康检查（每步联调前建议执行）

```http
GET /api/v1/health
```

```bash
curl http://127.0.0.1:8787/api/v1/health
```

期望 `data.checks.ffmpeg.ok`、`ffprobe.ok`、`llm_split.ok` 均为 `true`。

若连不上（`curl: (7) Failed to connect`），Swagger 会显示 **`Failed to fetch`**（并非 CORS 问题），请先确认 API 进程在跑、端口 `8787` 已监听。

---

## 3. OSS 素材准备

业务侧先将每句音频/视频上传 OSS，API 通过 URL 拉取。

### 3.1 URL 模板

**通用（替换 Bucket 与路径）：**

| 用途 | URL 模板 |
|------|----------|
| 第 N 句音频 | `https://your-bucket.oss-cn-hangzhou.aliyuncs.com/flow-b/audio/N.wav` |
| 第 N 段视频 | `https://your-bucket.oss-cn-hangzhou.aliyuncs.com/flow-b/source_media/N.mp4` |

**2026-06-25 实测 Bucket（输入 + 输出）：**

| 用途 | URL 模板 |
|------|----------|
| 第 N 句音频（输入） | `https://example-bucket.oss-cn-region.aliyuncs.com/audio/N.wav` |
| 第 N 段视频（输入） | `https://example-bucket.oss-cn-region.aliyuncs.com/source_media/N.mp4` |
| 成片 MP4（输出） | `https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/{code}-{id}/{date}/{job_id}.mp4` |
| 完整音频（输出） | `…/{job_id}.wav` |
| 任务日志（输出） | `…/{job_id}.log` |

- `N` = 1 … `segment_count`（同文案 LLM 分句本次为 **13 句**，常见 13～16，**以 split Job 的 `result` 为准**）
- 私有 Bucket 使用**预签名 URL**，有效期须覆盖整条 pipeline
- API 服务器须能**出网 GET** 上述地址

**可复制 JSON：** [`api-flow-b-web-test-examples.json`](./api-flow-b-web-test-examples.json)（含 16 句完整请求体模板；用编辑器打开复制到 Swagger，避免从 Markdown 带入隐藏字符）

### 3.2 Job 轮询与超时建议

| 阶段 | 提交 | 轮询 | 建议间隔 | 典型耗时 |
|------|------|------|----------|----------|
| 分句 | `POST .../split/run` | `GET .../split/jobs/{id}` | 2～3 s | 5～30 s（LLM） |
| 配音 | `POST .../audio/process/run` | `GET .../audio/process/jobs/{id}` | 5 s | `max(180, segment_count × 90)` s |
| 素材绑定 | `POST .../media/bind/run` | `GET .../media/bind/jobs/{id}` | 5～10 s | `max(180, segment_count × 120)` s |
| 画面预览 | `POST .../visual/preview/run` | `GET .../visual/preview/jobs/{id}` | 3 s | 10～30 s |
| 合成 | `POST .../compose/run` | `GET .../compose/jobs/{id}` | 5～10 s | **~60 s**（13 句，含 OSS 上传） |

**2026-06-25 端到端实测（13 句，同文案）：**

| 阶段 | 耗时（约） |
|------|------------|
| split | ~10 s |
| audio/process | ~15 s |
| media/bind | **~5 min**（13 段 OSS 视频拉取） |
| compose + OSS 上传 | **~60 s** |
| **全流程合计** | **~5.5 min** |

**2026-06-24 参考：** 14 段视频合计约 **636 MB**，`media/bind` ~300 s；compose ~45 s（本地 workspace，无 OSS 删除）。

### 3.3 Job 状态说明

| `status` | 含义 |
|----------|------|
| `queued` | 已入队；若全局并发已满，在此状态等待执行槽位（见下表可选字段） |
| `running` | 执行中；`progress.message` 可读当前阶段 |
| `done` | 成功；读 `data.result` |
| `failed` | 失败；读 `data.error` |
| `cancelled` | 已取消 |
| `interrupted` | 服务重启/强杀导致中断；须重新 `POST .../run` |

**`status=queued` 且正在等待全局槽位时**，轮询响应可能额外包含：

| 字段 | 说明 |
|------|------|
| `queue_position` | 排队位置（1 表示下一个获得槽位） |
| `queue_ahead` | 前方等待任务数 |
| `queue_message` | 人类可读说明，如「前方还有 2 个任务等待执行槽位」 |

示例：

```json
{
  "job_id": "job_abc123",
  "status": "queued",
  "queue_position": 2,
  "queue_ahead": 1,
  "queue_message": "前方还有 1 个任务等待执行槽位"
}
```

---

## 4. 接口一览

| 步骤 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 0 | GET | `/api/v1/health` | 健康检查 |
| 1 | POST | `/api/v1/workspaces` | 创建工作区 |
| — | GET | `/api/v1/workspaces/{id}/status` | 查询阶段状态 |
| 2a | POST | `/api/v1/workspaces/{id}/split/run` | 提交分句 Job |
| 2b | GET | `/api/v1/workspaces/{id}/split/jobs/{job_id}` | 轮询分句结果 |
| 3a | POST | `/api/v1/workspaces/{id}/audio/process/run` | 提交配音 Job |
| 3b | GET | `/api/v1/workspaces/{id}/audio/process/jobs/{job_id}` | 轮询配音结果 |
| 4 | PUT | `/api/v1/workspaces/{id}/audio/confirm` | 锁定音频 |
| 5a | POST | `/api/v1/workspaces/{id}/media/bind/run` | 提交素材绑定 Job |
| 5b | GET | `/api/v1/workspaces/{id}/media/bind/jobs/{job_id}` | 轮询绑定结果 |
| 6a | POST | `/api/v1/workspaces/{id}/visual/preview/run` | 提交预览 Job（可选） |
| 6b | GET | `/api/v1/workspaces/{id}/visual/preview/jobs/{job_id}` | 轮询预览结果 |
| — | GET | `/api/v1/subtitle/fonts` | 字体下拉选项 |
| 7 | POST | `/api/v1/workspaces/{id}/compose/run` | 提交合成 Job（须 `code` + `id`） |
| 8 | GET | `/api/v1/workspaces/{id}/compose/jobs/{job_id}` | 轮询合成 Job；**成片 URL 在 `result` 中（OSS）** |
| 9 | GET | `/api/v1/workspaces/{id}/files/{file_path}` | **过程预览**（音频/字幕/素材/预览图）；**不含成功后的成片** |
| — | POST | `/api/v1/maintenance/purge-workspaces` | **运维**：清理未完成/失败的 workspace（非业务必需） |

各阶段均有 `POST .../jobs/{job_id}/cancel` 可取消进行中的 Job。

### 4.1 流水线中需保存的变量

| 变量 | 来源 | 用途 |
|------|------|------|
| `WS_ID` | 步骤 1 → `data.workspace_id` | 所有路径中的 `{workspace_id}` |
| `SPLIT_JOB_ID` | 步骤 2a → `data.job_id` | 轮询分句 |
| `TEXT_DIGEST` | 步骤 2b → `data.result.text_digest` | audio/process、media/bind 校验 |
| `AUDIO_JOB_ID` | 步骤 3a → `data.job_id` | 轮询配音 |
| `REVISION_ID` | 步骤 3 process → `data.result.revision_id` | media/bind 必填 |
| `AUDIO_CONFIG_DIGEST` | 步骤 3 process → `data.result.audio_config_digest` | media/bind 建议校验 |
| `MEDIA_JOB_ID` | 步骤 5a → `data.job_id` | 轮询素材绑定 |
| `MEDIA_BINDING_DIGEST` | 步骤 5b → `data.result.media_binding_digest` | 状态查询 |
| `RENDER_STYLE_DIGEST` | 步骤 6b → `data.result.render_style_digest` | compose 自动沿用预览样式（E2E 未调用 preview 时无） |
| `COMPOSE_CODE` | 步骤 7 body `"code"` | OSS 路径；实测 `e2e` |
| `COMPOSE_USER_ID` | 步骤 7 body `"id"` | OSS 路径；实测 `noodle` |
| `COMPOSE_JOB_ID` | 步骤 7 → `data.job_id` | 轮询合成进度；实测 `job_b90e677e7fd4` |
| `OUTPUT_VIDEO_URL` | 步骤 8 → `data.result.output_video_url` | **成片 HTTPS 地址（OSS）** |
| `OUTPUT_AUDIO_URL` | 步骤 8 → `data.result.output_audio_url` | 完整合成音频（OSS） |
| `OUTPUT_VIDEO_OBJECT_KEY` | 步骤 8 → `data.result.output_video_object_key` | OSS object key（私有桶签名用） |

---

## 5. 逐步操作（2026-06-25 端到端实测）

### 5.0 实测概览

| 项目 | 值 |
|------|-----|
| 日期 | 2026-06-25 |
| workspace_id | `ws_87fae3579d67`（compose 成功后**已删除**） |
| 文案 | 深夜面馆（单行，见下方） |
| segment_count | **13**（LLM 分句，每次运行可能略有差异） |
| text_digest | `3732aa0fbf5cc813133b6664db384479` |
| revision_id | `rev_953ccc793c2c` |
| compose job_id | `job_b90e677e7fd4` |
| compose `code` / `id` | `e2e` / `noodle` |
| 成片时长 | 55.04 s |
| 成片大小 | 27.6 MB（本地下载验证） |
| 字幕样式 | **未调 preview**，使用默认样式 |
| 全流程脚本 | `scripts/e2e_noodle_compose.py` |

**一键复现（API 已启动且 `.env` 已配置 OSS）：**

```bash
cd /path/to/videoaudiotext
PYTHONPATH=src .venv/bin/python scripts/e2e_noodle_compose.py
# 日志：/tmp/e2e_noodle_compose.log
# 本地成片：output_e2e_noodle.mp4
```

**OSS 成片（公网 Bucket 可直接打开）：**

```
https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.mp4
```

---

以下按 Swagger 手工联调顺序说明；ID 与上方实测一致。

### 步骤 1：创建工作区

```http
POST /api/v1/workspaces
Content-Type: application/json
```

```json
{
  "label": "e2e-noodle-test"
}
```

**实测响应（节选）：**

```json
{
  "code": 0,
  "data": {
    "workspace_id": "ws_87fae3579d67",
    "label": "e2e-noodle-test",
    "status": {
      "split_ready": false,
      "audio_ready": false,
      "audio_confirmed": false,
      "media_bound": false
    }
  }
}
```

---

### 步骤 2：LLM 分句

#### 2a — 提交 Job

```http
POST /api/v1/workspaces/ws_87fae3579d67/split/run
```

```json
{
  "text": "夜深之后，城市褪去了白日的喧嚣，穿行在空旷的街道上，抬眼望去，街角的面馆亮着一盏暖融融的灯。放眼望去，小店不大，却在冷清的夜色里显得格外温馨，成了晚归之人的一处避风港。细看，大锅里的骨汤咕嘟咕嘟持续翻滚，手工面条下入锅中，在沸水里轻轻起伏。一碗碗热气腾腾的面食被端上桌，升腾的白雾裹着鲜香，瞬间驱散了深夜的寒意。加班至深夜的打工人、独自在外打拼的异乡人安静坐在桌前，低头嗦面、小口喝汤。一口热食下肚，一路奔波的疲惫、独处异乡的孤单，都在这一碗面的温度里慢慢消散。",
  "include_ai_prompts": false
}
```

**注意：** `text` 必须**单行**，不能含换行。

**立即响应：** `{ "job_id": "job_…", "status": "queued" }`

#### 2b — 轮询结果

```http
GET /api/v1/workspaces/ws_87fae3579d67/split/jobs/{job_id}
```

每 2～3 秒轮询，直到 `status: done`。

**2026-06-25 实测 `result` 关键字段：**

```json
{
  "text_digest": "3732aa0fbf5cc813133b6664db384479",
  "split_mode_used": "llm",
  "segment_count": 13,
  "segments": [
    { "index": 1, "text": "夜深之后，城市褪去了白日的喧嚣，", "visual": true },
    { "index": 2, "text": "穿行在空旷的街道上，", "visual": true }
  ]
}
```

> LLM 分句结果每次可能不同（本次 **13 句**，历史联调曾为 14～16 句）。**后续 audio/bind 中每句 `text` 必须与 `result.segments` 逐字一致**；OSS 素材条数须等于 `segment_count`。

#### 2a'（可选）分句并生成 AI 提示词

将 `include_ai_prompts` 设为 `true`，可加 `global_style`（见 [`api-flow-b-web-test-examples.json`](./api-flow-b-web-test-examples.json)）。建议 curl `--max-time 120`。

---

### 步骤 3：配音处理

#### 3a — 提交 Job

```http
POST /api/v1/workspaces/ws_87fae3579d67/audio/process/run
```

按 split 的 **13 句**构造 `segments`（每句 `audio.url` 指向 OSS；`index` 与 split 一致）：

```json
{
  "label": "e2e男声",
  "text_digest": "3732aa0fbf5cc813133b6664db384479",
  "segments": [
    { "index": 1, "text": "夜深之后，城市褪去了白日的喧嚣，", "audio": { "url": "https://example-bucket.oss-cn-region.aliyuncs.com/audio/1.wav" } },
    { "index": 2, "text": "穿行在空旷的街道上，", "audio": { "url": "https://example-bucket.oss-cn-region.aliyuncs.com/audio/2.wav" } }
  ],
  "force_refresh": true
}
```

（完整 13 句见脚本 `scripts/e2e_noodle_compose.py` 或 Swagger 中补全 `index` 3～13。）

#### 3b — 轮询结果

```http
GET /api/v1/workspaces/ws_87fae3579d67/audio/process/jobs/{job_id}
```

**2026-06-25 实测：**

| 字段 | 值 |
|------|-----|
| `revision_id` | `rev_953ccc793c2c` |
| `label` | `e2e男声` |
| `segment_count` | 13 |
| 进度示例 | `处理配音 11/13` → `生成字幕` → `完成` |
| `master_audio_url` | `/api/v1/workspaces/ws_87fae3579d67/files/audio/revisions/rev_953ccc793c2c/master.wav`（过程预览） |

---

### 步骤 4：锁定配音 revision（可选，本次 E2E 跳过）

```http
PUT /api/v1/workspaces/ws_87fae3579d67/audio/confirm
```

```json
{ "revision_id": "rev_953ccc793c2c" }
```

> **2026-06-25 E2E 未调用本步**：`media/bind` 传 `revision_id` 时会自动提升为 active 并 `audio_confirmed=true`。

---

### 步骤 5：绑定素材

#### 5a — 提交 Job

```http
POST /api/v1/workspaces/ws_87fae3579d67/media/bind/run
```

```json
{
  "revision_id": "rev_953ccc793c2c",
  "text_digest": "3732aa0fbf5cc813133b6664db384479",
  "audio_config_digest": "<audio/process result 中的 digest>",
  "segments": [
    { "index": 1, "text": "夜深之后，城市褪去了白日的喧嚣，", "media": { "url": "https://example-bucket.oss-cn-region.aliyuncs.com/source_media/1.mp4", "type": "video", "start_sec": 0.0 } },
    { "index": 2, "text": "穿行在空旷的街道上，", "media": { "url": "https://example-bucket.oss-cn-region.aliyuncs.com/source_media/2.mp4", "type": "video", "start_sec": 0.0 } }
  ]
}
```

（完整 13 句同上，见 E2E 脚本。）

#### 5b — 轮询结果

```http
GET /api/v1/workspaces/ws_87fae3579d67/media/bind/jobs/{job_id}
```

**2026-06-25 实测进度：**

```
拉取素材 1/13 … 13/13 → done
```

耗时约 **5 分钟**（瓶颈在 OSS 视频下载，勿重复提交 `/run`）。

---

### 步骤 6：字幕样式预览（可选，本次 E2E 跳过）

未调用 `visual/preview/run`，compose 使用**默认字幕样式**（思源黑体默认参数）。

若需调字体/位置，参考下方示例（历史 2026-06-24 联调曾用 `font_scale: 1.2`、`y_offset: -1`）：

```http
POST /api/v1/workspaces/ws_87fae3579d67/visual/preview/run
```

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

预览图：`GET .../files/previews/seg_1.jpg`（仅 compose 完成前有效）。

#### `y_offset` 档位说明

| 项 | 值 |
|----|-----|
| API 允许范围 | **`-10` ～ `20`**（整数档位，**不是像素**） |
| 竖屏 1080×1920 | 1 档 ≈ **48px**；正数上移，负数下移 |

字体列表：**GET** `/api/v1/subtitle/fonts`。

---

### 步骤 7：提交合成

```http
POST /api/v1/workspaces/ws_87fae3579d67/compose/run
```

```json
{
  "code": "e2e",
  "id": "noodle",
  "subtitle_mode": "hard",
  "reuse_intermediates": true,
  "cleanup_scratch_on_success": true
}
```

| 字段 | 说明 |
|------|------|
| `code` | **必填**。租户编码 → OSS 路径 `prod/ImagesVideosText2Video/{code}-{id}/…` |
| `id` | **必填**。用户 ID |
| `subtitle_mode` | `hard` 硬字幕烧录 |
| `reuse_intermediates` | 同 workspace 内复用 clip |
| `cleanup_scratch_on_success` | 成功后清理 compose scratch |

**实测响应：** `{ "job_id": "job_b90e677e7fd4", "status": "queued" }`

---

### 步骤 8：轮询合成 Job

```http
GET /api/v1/workspaces/ws_87fae3579d67/compose/jobs/job_b90e677e7fd4
```

**2026-06-25 实测进度：**

```
准备合成 → 画面叠化 xfade（音轨硬切）0.30s（13 段）→ 上传 OSS… → done
```

**`status: done` 时完整 `result`：**

```json
{
  "output_video_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.mp4",
  "output_video_object_key": "prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.mp4",
  "output_audio_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.wav",
  "output_audio_object_key": "prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.wav",
  "log_url": "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.log",
  "log_object_key": "prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.log",
  "oss_prefix": "prod/ImagesVideosText2Video/e2e-noodle/2026-06-25",
  "total_seconds": 55.04,
  "durations_sec": [4.005488, 2.5078, 4.307324, 5.329478, 3.925488, 3.925488, 4.725283, 4.214444, 5.503175, 3.018639, 4.307324, 2.600658, 3.703605],
  "gaps_sec": [0.15, 0.35, 0.15, 0.35, 0.15, 0.35, 0.15, 0.35, 0.15, 0.15, 0.35, 0.15, 0.0],
  "intermediates_cleaned": true,
  "workspace_deleted": true
}
```

> compose 成功后 **`ws_87fae3579d67` 目录已删除**，`GET .../status` 与 `GET .../files/*` 返回 **404**。Job 归档于 `workspaces/_compose_results/job_b90e677e7fd4.json`，仍可用原 URL 轮询 compose Job。

---

### 步骤 9：获取成片（OSS）

```bash
curl -L -o output.mp4 \
  "https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.mp4"
```

浏览器直接打开上述 URL 即可播放（公网 Bucket）。私有 Bucket 须预签名或使用 `output_video_object_key` 自行签名。

**勿访问** `GET .../files/deliverables/output.mp4`（workspace 已不存在）。

---

### 附录：2026-06-24 手工联调参考（14 句 · 含 preview）

以下为早期 Swagger 逐步联调记录（workspace `ws_ff95a5f8547b`，本地保留 workspace，成片走 `/files/deliverables/output.mp4`）。步骤与上文相同，差异：

| 项目 | 2026-06-24 | 2026-06-25 |
|------|------------|------------|
| segment_count | 14 | 13 |
| text_digest | `40d67ef71c5e18648facd82d035d9e56` | `3732aa0fbf5cc813133b6664db384479` |
| visual/preview | 已调用（思源黑体 1.2 / y_offset -1） | 未调用 |
| 成片交付 | 本地 `/files/deliverables/output.mp4` | OSS `output_video_url` |
| compose 后 workspace | 保留 | **删除** |

详细 14 句 JSON 模板见 [`api-flow-b-web-test-examples.json`](./api-flow-b-web-test-examples.json)。

---

## 6. 工作区状态

```http
GET /api/v1/workspaces/ws_87fae3579d67/status
```

**media/bind 完成后、compose 提交前** 示例：

```json
{
  "code": 0,
  "data": {
    "workspace_id": "ws_87fae3579d67",
    "split_ready": true,
    "audio_ready": true,
    "audio_confirmed": true,
    "media_bound": true,
    "visual_preview_ready": false,
    "compose_ready": false,
    "digests": {
      "text_digest": "3732aa0fbf5cc813133b6664db384479"
    },
    "active_compose_job_id": null
  }
}
```

> **compose 成功后** 上述 URL 返回 **404**（workspace 已删）。进行中的 Job 出现在 `active_*_job_id` 字段。

---

## 7. 文件访问（过程预览，非成片交付）

**GET** `/api/v1/workspaces/{workspace_id}/files/{file_path}`

将各步骤 `result` 中的相对 `*_url` 拼到 Base URL 即可。**仅适用于 compose 完成前**的工作区。

| 允许前缀 | 典型文件 | 用途 |
|----------|----------|------|
| `audio/` | `1.wav` … `N.wav`、`master.wav` | 试听配音 |
| `subtitles/` | `subtitle.srt`、`subtitle.ass` | 字幕预览 |
| `source_media/` | 绑定后的素材 | 核对画面 |
| `previews/` | `seg_1.jpg` | 字幕样式预览图 |
| ~~`deliverables/`~~ | ~~`output.mp4`~~ | **compose 成功并上传 OSS 后 workspace 已删，此路径不可用** |

**三类 URL 的区别：**

| 类型 | 示例 | 说明 |
|------|------|------|
| **OSS 输入 URL** | `https://example-bucket…/audio/1.wav` | 业务上传；传给 `audio/process`、`media/bind` |
| **`GET .../files/...`** | `/api/v1/workspaces/ws_xxx/files/audio/…` | 流水线**过程**产物，供试听/预览 |
| **OSS 成片 URL** | `result.output_video_url` | compose **最终交付**；HTTPS 直链 |

**播放过程音频/预览图：** 浏览器新标签页打开 `files` URL。**成片**用步骤 9 的 OSS URL，或 `curl -L -O` 下载。

---

## 8. 流水线依赖

```mermaid
flowchart LR
  A[health] --> B[workspaces]
  B --> C[split/run]
  C --> C2[poll split job]
  C2 --> D[audio/process/run]
  D --> D2[poll audio job]
  D2 --> F[media/bind/run]
  F --> F2[poll media job]
  F2 --> G[visual/preview/run optional]
  G --> G2[poll preview job]
  G2 --> H[compose/run]
  H --> I[poll compose job]
  I --> J[OSS output_video_url]
```

- **硬前置（compose 必须）：** split → audio/process → media/bind（`media/bind` 可替代 `audio/confirm`）
- **软前置（可选）：** visual/preview（未调用则用默认字幕样式；**2026-06-25 E2E 未调用**）

---

## 9. 常见问题

| 现象 | 原因与处理 |
|------|------------|
| Swagger **`Failed to fetch`**（Undocumented） | **API 未运行或端口不通**（非 CORS）；先 `curl .../health`，用 `nohup` 或 Docker 保持进程常驻 |
| `/run` 只返回 `job_id`，没有 `segments` | 正常；须再调 `GET .../jobs/{job_id}` 轮询，`result` 里才有完整数据 |
| 调 `POST .../split` 返回 404 | 旧路径已废弃；改用 `POST .../split/run` |
| 步骤 5 轮询很久 | 后台下载 OSS 视频，本次约 5 分钟；查 `/status` 的 `media_bound` 与 `active_media_bind_job_id`，勿重复提交 |
| `40001 validation_error` `y_offset` | 档位超出 `-10`～`20`；勿传像素值（如 `200`） |
| 第二次 preview 字幕位置没变 | `y_offset` 是**相对默认位置的绝对档位**，不是在上次基础上累加；再下移应传 `-2` 而非重复 `-1` |
| 浏览器打不开成片 | compose 成功后 workspace 已删 | 使用 `result.output_video_url`（OSS），勿访问 `/files/deliverables/output.mp4` |
| compose 后 `GET .../status` 404 | workspace 已删除 | 正常；用原 `job_id` 继续调 `GET .../compose/jobs/{job_id}` |
| OSS 上传失败 / `oss2 is not installed` | 本地用系统 `python3` 启动 | 改用 `.venv/bin/python` 或 Docker（见 §2.2） |
| OSS 上传失败 | `.env` 未配或密钥错误 | 检查 `ALIYUN_OSS_*`；失败时 workspace **保留**，可修配置后重试 compose |
| `40901 text_digest mismatch` | split 后改了文案未重跑 split，或 audio/bind 中 `text` 与 split 不一致 |
| `40003 audio_not_confirmed` | 未 confirm 且 media/bind 未提升 revision | 正常 E2E 在 bind 后应为 true；否则补 `PUT .../audio/confirm` |
| `PYTHONPATH` 未设置导致 404 | 加载了旧版 API 包 | 启动时加 `PYTHONPATH=src`（见 §2.2） |
| `40902 *_job_active` | 同阶段有 Job 进行中；等待完成或 `POST .../jobs/{id}/cancel` |
| `status` 长期 `queued` 且含 `queue_position` | 全局并发已满，正常排队；勿重复提交，继续轮询 |
| 重启后 Job 变 `interrupted` | 进行中的任务不会自动续跑 | 对应该阶段重新 `POST .../run` |
| 422 JSON decode error | `text` 含换行；改为单行 |

**改稿重合成：** compose 成功后 workspace 已删，须 **新建 workspace** 从 split 重跑。compose **失败**时 workspace 仍在，可直接再次 `compose/run`。

**磁盘清理：**
- compose **成功**：workspace 自动删除；Job 归档于 `_compose_results/`。
- **未完成/失败**：占用磁盘；`purge-workspaces` 有 **24h grace**（当天测试不会自动删），可 `DELETE /workspaces/{id}` 立即清理。
- 运维：`POST /maintenance/purge-workspaces`；排查排队：`GET /maintenance/coord-status`。

---

## 10. 本次实测摘要

### 2026-06-25 端到端（OSS 成片，推荐参照）

| 项目 | 值 |
|------|-----|
| 日期 | 2026-06-25 |
| workspace_id | `ws_87fae3579d67`（compose 后已删除） |
| segment_count | 13 |
| text_digest | `3732aa0fbf5cc813133b6664db384479` |
| revision_id | `rev_953ccc793c2c` |
| compose job_id | `job_b90e677e7fd4` |
| compose code / id | `e2e` / `noodle` |
| 成片时长 | 55.04 s |
| 成片大小 | 27.6 MB |
| OSS 成片 | [`…/job_b90e677e7fd4.mp4`](https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/e2e-noodle/2026-06-25/job_b90e677e7fd4.mp4) |
| OSS 完整音频 | `…/job_b90e677e7fd4.wav` |
| OSS 任务日志 | `…/job_b90e677e7fd4.log` |
| 本地验证文件 | `output_e2e_noodle.mp4`（脚本下载） |
| split | ~10 s |
| audio/process | ~15 s |
| media/bind | ~5 min |
| compose + OSS | ~60 s |
| **全流程** | **~5.5 min** |
| visual/preview | 未调用（默认字幕） |
| 复现脚本 | `scripts/e2e_noodle_compose.py` |

### 2026-06-25 并发 compose + 逐步 curl 双任务

| 项目 | 值 |
|------|-----|
| 10 路 compose 压测 | `GLOBAL_JOB_WORKERS=6` → 6 running + 4 排队，10/10 OSS 成功 |
| 逐步 curl 双任务 | `api-step` / `test-a` · `test-b`；13/14 句；并行 compose ~65s |
| 成片 A | [`job_8b5a52507304.mp4`](https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/api-step-test-a/2026-06-25/job_8b5a52507304.mp4) |
| 成片 B | [`job_dfc11d65b0fc.mp4`](https://example-bucket.oss-cn-region.aliyuncs.com/prod/ImagesVideosText2Video/api-step-test-b/2026-06-25/job_dfc11d65b0fc.mp4) |

### 2026-06-24 参考（本地 workspace · 14 句）

| 项目 | 值 |
|------|-----|
| workspace_id | `ws_ff95a5f8547b` |
| segment_count | 14 |
| compose job_id | `job_dff9a459f1cd` |
| 成片时长 | 60.76 s |
| 成片交付 | 本地 `/files/deliverables/output.mp4` |
| 字幕 | 思源黑体 · font_scale 1.2 · y_offset -1 |
| media/bind | ~300 s（636 MB） |
| compose | ~45 s |

---

## 11. 相关文档

| 文档 | 用途 |
|------|------|
| [docs/README.md](./README.md) | 文档索引 |
| [api-flow-b-fastapi-service-api.md](./api-flow-b-fastapi-service-api.md) | 完整接口规范、错误码、Schema |
| [api-flow-b-production.md](./api-flow-b-production.md) | 设计契约与数据模型 |
| [DOCKER_OPS.md](../DOCKER_OPS.md) | Docker 部署运维 |
| [api-flow-b-web-test-examples.json](./api-flow-b-web-test-examples.json) | 16 句 JSON 模板（须按 split 句数裁剪） |
| [../scripts/e2e_noodle_compose.py](../scripts/e2e_noodle_compose.py) | 2026-06-25 一键端到端脚本 |
