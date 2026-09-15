# embedding_service

`embedding_service` 是一个基于 FastAPI 的媒体与文本向量编码服务。

当前版本 **0.3.0**：

- 图片 / 视频：**异步任务**（`embed-jobs` + `job_id` 查询）
- 文本 query：**同步接口**（`POST /v1/query/clip/embed`、`/v1/query/bge/embed` 立即返回）
- 启动时预加载模型并预热文本编码

完整参数说明见 [`PARAMS.md`](PARAMS.md)。**HTTP 接口文档**见 [`API.md`](API.md)。部署到其他服务器见项目根目录 [`DEPLOY.md`](../DEPLOY.md)（含 **Docker 部署**，不含 Milvus）。

---

## 依赖关系

本服务仅依赖：

- `embedding_service/`（本目录）
- `embed_core/`（共享编码库）

**不依赖** `picture/`、`video_retrieval/`。

## 快速开始

### 1) 安装依赖

```powershell
pip install -r requirements.txt
```

复制项目根目录 `.env.example` 为 `.env` 并按需修改。

### 2) 启动服务

端口 **必填**（未设置 `EMBEDDING_SERVICE_PORT` 会启动失败）：

```powershell
cd <项目根目录>
$env:EMBEDDING_SERVICE_PORT="8030"
python start_embedding_service.py
```

等价命令：`python -m embedding_service.api`（需在项目根目录执行）。

### 3) 打开接口文档

```text
http://127.0.0.1:8030/docs
```

建议使用 `127.0.0.1`，避免 Windows 上 `localhost` 解析到 IPv6 导致变慢。启动后终端会打印文档地址，请自行复制到浏览器打开。

---

## Docker 部署（不含 Milvus）

在项目根目录使用 Docker 运行本服务，详见 [`DEPLOY.md` 第四节](../DEPLOY.md#四docker-部署不含-milvus)。

```bash
cd <项目根目录>
cp .env.example .env
# 编辑 .env：EMBEDDING_SERVICE_PORT=8030，按需填写 OSS

bash scripts/deploy.sh
```

要点：

- 构建上下文为**项目根目录**（需 `embed_core/`、`models/` 等）
- compose 强制 `VECTOR_STORE=none`，仅生成向量、不写 Milvus
- `./models` 挂载到容器（容器用户 UID 1000，自动下载前需 `chown -R 1000:1000 models`）；`runtime/` 使用 Docker 卷持久化
- 首次启动可能较慢，`docker compose ps` 在健康检查通过前可能显示 `unhealthy`
- 验证：`curl http://127.0.0.1:8030/health`，文档 `http://127.0.0.1:8030/docs`

---

## 配置说明

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `EMBEDDING_SERVICE_PORT` | （必填） | 服务端口，例如 `8030` |
| `EMBEDDING_SERVICE_HOST` | 0.0.0.0 | 服务地址 |
| `LOG_LEVEL` | INFO | 日志级别 (DEBUG/INFO/WARNING/ERROR) |
| `CHINESE_CLIP_MODEL_PATH` | `<项目根>/models` | CLIP 模型目录 |
| `AUTO_DOWNLOAD_MODELS` | 1 | 缺模型时启动自动下载 |
| `HF_USE_MIRROR` | （空） | 设为 `1` 使用 HF 镜像 |
| `CLIP_DEVICE` | cpu | CLIP 设备 (cpu/cuda) |
| `CLIP_BATCH_SIZE` | 16 | CLIP 批处理大小 |
| `PICTURE_BGE_MODEL` | BAAI/bge-large-zh-v1.5 | BGE 模型名称 |
| `BGE_DEVICE` | cpu | BGE 设备 (cpu/cuda) |
| `BGE_BATCH_SIZE` | 16 | BGE 批处理大小 |
| `TASK_DB_PATH` | runtime/tasks.db | 任务数据库路径 |
| `MAX_TASKS` | 10000 | 最大任务数 |
| `TASK_TTL_HOURS` | 24 | 任务保留时间（小时） |
| `MEDIA_DOWNLOAD_TIMEOUT` | 60 | 媒体下载超时（秒） |
| `MEDIA_MAX_IMAGE_MB` | 50 | 最大图片大小（MB） |
| `MEDIA_MAX_VIDEO_MB` | 2048 | 最大视频大小（MB） |
| `VECTOR_STORE` | milvus | 向量存储 (milvus/none) |

---

## 接口总览

### 健康检查

```http
GET /health
GET /stats
```

### 文本 query（同步，立即返回）

```http
POST /v1/query/clip/embed
POST /v1/query/bge/embed
```

### 图片 / 视频向量（异步，CLIP 与 BGE 分路径）

每种组合各 **1 个 POST + 1 个 GET 状态 + 1 个 GET 结果**：

```http
POST /v1/images/clip/embed-jobs   GET .../{job_id}   GET .../{job_id}/result
POST /v1/images/bge/embed-jobs    GET .../{job_id}   GET .../{job_id}/result
POST /v1/videos/clip/embed-jobs   GET .../{job_id}   GET .../{job_id}/result
POST /v1/videos/bge/embed-jobs    GET .../{job_id}   GET .../{job_id}/result
```

---

## 提交任务请求示例

### 1) 图片 CLIP — `POST /v1/images/clip/embed-jobs`

```json
{
  "media_id": "img_001",
  "object_key": "images/demo.jpg",
  "content_type": "image/jpeg",
  "return_vector": true,
  "write_vector": false
}
```

### 2) 图片 BGE — `POST /v1/images/bge/embed-jobs`

```json
{
  "media_id": "img_002",
  "content_type": "image/jpeg",
  "description": "一张红色连衣裙商品图，背景简洁",
  "return_vector": true,
  "write_vector": false
}
```

### 3) 视频 CLIP — `POST /v1/videos/clip/embed-jobs`

```json
{
  "media_id": "video_001",
  "object_key": "videos/demo.mp4",
  "content_type": "video/mp4",
  "return_vector": true,
  "write_vector": false,
  "configuration": {
    "sample_fps": 1,
    "max_frames": 16,
    "top_k_frames": 4,
    "segment_seconds": 5
  }
}
```

### 4) 视频 BGE — `POST /v1/videos/bge/embed-jobs`

```json
{
  "media_id": "video_002",
  "content_type": "video/mp4",
  "description": "一个人在厨房里做饭",
  "return_vector": true,
  "write_vector": false
}
```

### 5) query + clip / bge（同步，非 job）

```json
{
  "text": "一辆红色跑车在城市道路上行驶",
  "return_vector": true
}
```

调用 `POST /v1/query/clip/embed` 或 `POST /v1/query/bge/embed`，响应中直接包含 `embedding`。

图片 / 视频异步任务提交成功返回：

```json
{
  "job_id": "8c8f7d3f1f444ef1b3f4d9c6ddfdbf1b",
  "status": "queued"
}
```

---

## 任务查询

`job_id` 须用**与提交时相同前缀**的 GET（例如 CLIP 任务走 `/v1/images/clip/embed-jobs/...`）。

状态响应含：`job_kind`（如 `image/clip`）、`status`、`progress`、`result_url`（成功时）。

视频 CLIP 取结果仅返回整片 `embedding` 向量（与图片 CLIP 结构一致）。

---

## 错误码说明

| 错误码 | 说明 |
|--------|------|
| `CONFIGURATION_ERROR` | 配置错误 |
| `VALIDATION_ERROR` | 输入验证错误 |
| `MEDIA_DOWNLOAD_ERROR` | 媒体下载失败 |
| `MEDIA_PROCESSING_ERROR` | 媒体处理失败 |
| `MODEL_ERROR` | 模型加载或推理错误 |
| `VECTOR_STORE_ERROR` | 向量存储错误 |
| `TASK_LIMIT_ERROR` | 任务数量超限 |
| `INTERNAL_ERROR` | 内部错误 |

---

## 关于 Milvus

默认 `VECTOR_STORE=milvus`（见 `.env.example`）。**需要 Milvus** 的情况：

- API 请求 `write_vector=true`（写入 `media_*` 在线集合）
- 本地 `picture/`、`video_retrieval/` 检索（写入/查询 `picture_*`、`video_clip_*`、`hybrid_video_bge` 等集合）

**可不启 Milvus** 的情况：仅调用 embedding 接口且 `write_vector=false`，且不在本机跑检索服务。

无论哪种场景，使用前都应：

- 由运维提供可用的 Milvus，并在 `.env` 中配置 `MILVUS_URI`
- 执行 `python -m embedding_service.milvus_init` 创建 collection（每个环境一次）

### 1) 配置连接

```bash
# .env 示例
VECTOR_STORE=milvus
MILVUS_URI=http://<milvus-host>:19530
```

### 2) 初始化 collection

```powershell
python -m embedding_service.milvus_init
```

### 3) 启动 embedding 服务并写库

```powershell
$env:EMBEDDING_SERVICE_PORT="8030"
$env:VECTOR_STORE="milvus"
python start_embedding_service.py
```

---

## 辅助脚本与工具

说明见项目根 [`scripts/README.md`](../scripts/README.md)。

| 用途 | 命令 |
|------|------|
| 下载模型（全平台） | `python scripts/download_models.py` |
| 下载模型（仅 Windows） | `.\scripts\download_models.ps1` |
| 启动本服务 | `python start_embedding_service.py`（根目录） |
| Milvus 建表 | `python -m embedding_service.milvus_init` |
| 轮询健康状态 | `python -m embedding_service.monitor --port 8030` |

**不存在**以下历史路径，文档与旧笔记中若出现请忽略：`scripts/start_embedding_service.ps1`、`scripts/init_milvus.ps1`、`scripts/test_embedding_service.ps1`。

---

## 日志

日志文件位置：`runtime/logs/embedding_service.log`

可通过 `LOG_LEVEL` 环境变量调整日志级别。

---

## 性能优化建议

1. **GPU 加速**：设置 `CLIP_DEVICE=cuda` 和 `BGE_DEVICE=cuda`
2. **批处理大小**：根据 GPU 显存调整 `CLIP_BATCH_SIZE` 和 `BGE_BATCH_SIZE`
3. **任务清理**：定期清理过期任务，或调整 `TASK_TTL_HOURS`
4. **并发控制**：通过 `MAX_TASKS` 限制并发任务数

---

## 故障排查

### 问题：服务启动失败

- 检查端口是否被占用
- 检查模型路径是否正确
- 查看日志文件获取详细错误信息

### 问题：任务一直处于 queued 状态

- 检查线程池是否已满
- 查看服务日志是否有异常
- 检查模型是否加载成功

### 问题：内存占用过高

- 减少 `MAX_TASKS` 限制并发任务数
- 减少 `TASK_TTL_HOURS` 加快任务清理
- 检查是否有大量失败任务堆积

---

## 相关文档

- [`PARAMS.md`](PARAMS.md) — 环境变量与 API 字段说明（中文）
- [`scripts/README.md`](../scripts/README.md) — `scripts/` 目录与命令对照
- 项目根目录 [`README.md`](../README.md) — 仓库总览
