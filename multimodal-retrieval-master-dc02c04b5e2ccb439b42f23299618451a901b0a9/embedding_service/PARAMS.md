# embedding_service 参数说明

本文档按 **代码定义顺序** 整理 `embedding_service` 中的环境变量、API 请求/响应字段。默认值以 `config.py` / `schemas.py` 为准。

---

## 一、环境变量（启动配置）

### 1.1 服务

| 序号 | 环境变量 | 默认值 | 中文说明 |
|------|----------|--------|----------|
| 1 | `EMBEDDING_SERVICE_PORT` | （必填，无默认） | HTTP 服务监听端口，启动前必须设置 |
| 2 | `EMBEDDING_SERVICE_HOST` | `0.0.0.0` | 监听地址；`0.0.0.0` 表示允许局域网访问 |
| 3 | `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| 4 | `WORKERS` | `3` | Uvicorn worker 进程数（按当前部署建议默认 3） |
| 5 | `EMBEDDING_WORKERS` | `4` | 异步 embedding 任务线程池大小（按当前部署建议默认 4） |

### 1.2 模型

| 序号 | 环境变量 | 默认值 | 中文说明 |
|------|----------|--------|----------|
| 8 | `CHINESE_CLIP_MODEL_PATH` | 项目根 `models/` | Chinese-CLIP 模型目录路径 |
| 9 | `CLIP_DEVICE` | `cpu` | CLIP 推理设备：`cpu` 或 `cuda` |
| 10 | `CLIP_BATCH_SIZE` | `16` | CLIP 批处理大小 |
| 11 | `PICTURE_BGE_MODEL` | `BAAI/bge-large-zh-v1.5` | BGE 模型名（HuggingFace） |
| 12 | `BGE_MODEL_PATH` | 自动检测 `models/bge-large-zh-v1.5` | BGE 本地模型目录，优先于在线下载 |
| 13 | `BGE_DEVICE` | `cpu` | BGE 推理设备：`cpu` 或 `cuda` |
| 14 | `BGE_BATCH_SIZE` | `16` | BGE 批处理大小 |
| 15 | `HF_LOCAL_FILES_ONLY` | 未设置 | 设任意非空值时，BGE 仅使用本地文件、不访问 HuggingFace |

### 1.3 任务管理

| 序号 | 环境变量 | 默认值 | 中文说明 |
|------|----------|--------|----------|
| 14 | `TASK_DB_PATH` | `runtime/tasks.db` | 异步任务 SQLite 数据库路径 |
| 15 | `MAX_TASKS` | `10000` | 最大任务数，超限拒绝新任务 |
| 16 | `TASK_TTL_HOURS` | `24` | 任务记录在库中的保留时间（小时） |

### 1.4 媒体处理

| 序号 | 环境变量 | 默认值 | 中文说明 |
|------|----------|--------|----------|
| 17 | `MEDIA_DOWNLOAD_TIMEOUT` | `300` | OSS 下载超时（秒），传给 oss2 `connect_timeout`；超大文件/慢网络可设 `600`–`1800` |
| 18 | `MEDIA_MAX_IMAGE_MB` | `50` | 单张图片最大体积（MB） |
| 19 | `MEDIA_MAX_VIDEO_MB` | `2048` | 单个视频最大体积（MB） |
| 20 | `MEDIA_TMP_DIR` | `runtime/tmp` | 下载与视频处理的临时目录 |
| 21 | `MAX_VIDEO_RESOLUTION` | `1920` | 视频分段前最长边上限（像素），用于控内存 |
| 22 | `MAX_FRAME_RESOLUTION` | `1280` | 抽帧后最长边上限（像素） |

### 1.5 阿里云 OSS（可选，私有桶拉取时用）

| 序号 | 环境变量 | 默认值 | 中文说明 |
|------|----------|--------|----------|
| 23 | `ALIYUN_OSS_ENDPOINT` | 无 | OSS Endpoint，如 `oss-cn-shenzhen.aliyuncs.com` |
| 24 | `ALIYUN_OSS_ACCESS_KEY_ID` | 无 | OSS AccessKey ID |
| 25 | `ALIYUN_OSS_ACCESS_KEY_SECRET` | 无 | OSS AccessKey Secret |
| 26 | `ALIYUN_OSS_BUCKET` | 无 | 默认桶名；请求未传 `bucket` 时使用 |

### 1.6 向量库 Milvus（可选）

| 序号 | 环境变量 | 默认值 | 中文说明 |
|------|----------|--------|----------|
| 27 | `VECTOR_STORE` | `milvus` | 向量存储：`milvus`（默认）写/检索 Milvus；`none` 仅生成向量不落库 |
| 28 | `MILVUS_URI` | `http://127.0.0.1:19530` | Milvus 连接地址（建议用 `127.0.0.1` 而非 `localhost`） |
| 29 | `MILVUS_TOKEN` | 无 | Milvus 认证 Token |
| 30 | `MILVUS_DB_NAME` | `default` | Milvus 数据库名 |
| 31 | `MILVUS_COLLECTION_PREFIX` | `chinese_clip` | Collection 名称前缀 |
| 32 | `MILVUS_METRIC_TYPE` | `COSINE` | 距离度量类型 |
| 33 | `IMAGE_CLIP_DIM` | `1024` | 图片 CLIP 向量维度（初始化 Milvus 用） |
| 34 | `IMAGE_BGE_DIM` | `1024` | 图片 BGE 向量维度 |
| 35 | `VIDEO_CLIP_DIM` | `1024` | 视频 CLIP 向量维度 |
| 36 | `VIDEO_BGE_DIM` | `1024` | 视频 BGE 向量维度 |

---

## 二、公共请求字段（`OssMediaRef`）

图片 CLIP、视频 CLIP 接口继承：

| 序号 | 字段 | 类型 | 必填 | 中文说明 |
|------|------|------|------|----------|
| 1 | `object_key` | string | 是 | OSS 对象键路径 |
| 2 | `content_type` | string | 是 | 传输数据 MIME 类型（如 `image/jpeg`、`video/mp4`） |

桶名、Endpoint、AK/SK 通过环境变量配置（见下文 `ALIYUN_OSS_*`），**不在请求体传 `bucket` / `media_url`**。

**常用 `content_type`**

| 模态 | 允许值 |
|------|--------|
| 图片 | `image/jpeg`、`image/png`、`image/webp`、`image/gif`、`image/bmp` |
| 视频 | `video/mp4`、`video/quicktime`、`video/x-msvideo`、`video/webm`、`video/x-matroska` |
| 文本 query | `text/plain`（默认） |

---

## 三、API 请求体（按接口）

### 3.1 图片 CLIP — `POST /v1/images/clip/embed-jobs`

继承 `OssMediaRef`：`object_key`、`media_id`、`return_vector`、`write_vector`。  
查询：`GET /v1/images/clip/embed-jobs/{job_id}`、`GET .../result`

### 3.2 图片 BGE — `POST /v1/images/bge/embed-jobs`

`media_id`、`content_type`（图片 MIME）、`description`（必填）、`return_vector`、`write_vector`。  
查询：`GET /v1/images/bge/embed-jobs/{job_id}`、`GET .../result`

### 3.3 视频 CLIP — `POST /v1/videos/clip/embed-jobs`

在 3.1 基础上增加 `configuration`（抽帧与分段，可省略则用默认值）：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `configuration.sample_fps` | float | `1.0` | 片段内每秒抽候选帧数 |
| `configuration.max_frames` | int | `16` | 每片段最多候选帧数 |
| `configuration.top_k_frames` | int | `4` | K-means 多样性筛选后每段保留帧数 |
| `configuration.segment_seconds` | int | `5` | 分段秒数 |

查询：`GET /v1/videos/clip/embed-jobs/{job_id}`、`GET .../result`

取结果：`GET /v1/videos/clip/embed-jobs/{job_id}/result`，响应仅含整片 `embedding` 向量（与图片 CLIP 结构一致）。

提交时设 `return_vector=false` 可减小 DB 中结果体积（仍会在内部计算向量；响应中 `embedding` 为 null）。

### 3.4 视频 BGE — `POST /v1/videos/bge/embed-jobs`

同 3.2，且 `content_type` 为视频 MIME（如 `video/mp4`）。  
查询：`GET /v1/videos/bge/embed-jobs/{job_id}`、`GET .../result`

---

### 3.5 文本查询 CLIP — `POST /v1/query/clip/embed`（同步）

| 序号 | 字段 | 类型 | 必填 | 默认值 | 中文说明 |
|------|------|------|------|--------|----------|
| 1 | `query_id` | string \| null | 否 | `null` | 可选查询 ID，便于业务追踪 |
| 2 | `text` | string | 是 | — | 待编码的中文查询文本 |
| 3 | `return_vector` | bool | 否 | `true` | 是否返回 CLIP 文本向量 |

---

### 3.6 文本查询 BGE — `POST /v1/query/bge/embed`（同步）

字段与 3.5 相同，模型为 BGE。

---

## 四、异步任务相关

### 4.1 提交任务统一响应

`POST .../embed-jobs` 立即返回：

| 序号 | 字段 | 类型 | 中文说明 |
|------|------|------|----------|
| 1 | `job_id` | string | 任务 ID，用于查询状态与结果 |
| 2 | `status` | string | 初始为 `queued` |

### 4.2 查询任务状态

**路径参数**：`job_id`（须由**同前缀**的 POST 返回）

| 类型 | 方法 | 路径 |
|------|------|------|
| 图片 CLIP | GET | `/v1/images/clip/embed-jobs/{job_id}` |
| 图片 BGE | GET | `/v1/images/bge/embed-jobs/{job_id}` |
| 视频 CLIP | GET | `/v1/videos/clip/embed-jobs/{job_id}` |
| 视频 BGE | GET | `/v1/videos/bge/embed-jobs/{job_id}` |

**响应字段（顺序）**

| 序号 | 字段 | 类型 | 中文说明 |
|------|------|------|----------|
| 1 | `job_id` | string | 任务 ID |
| 2 | `job_kind` | string | 如 `image/clip`、`video/bge` |
| 3 | `status` | string | `queued` / `running` / `succeeded` / `failed` |
| 4 | `created_at` | float | 创建时间（Unix 时间戳） |
| 5 | `updated_at` | float | 最后更新时间 |
| 6 | `progress` | object \| null | 进度信息 |
| 7 | `error` | string \| null | 失败时的错误消息 |
| 8 | `error_type` | string \| null | 失败时的错误码 |
| 9 | `result_size_bytes` | int | 仅成功时：结果 JSON 字节大小 |
| 10 | `result_url` | string | 仅成功时：同类型下的 `/result` 路径 |

### 4.3 获取任务结果

| 类型 | 方法 | 路径 |
|------|------|------|
| 图片 CLIP | GET | `/v1/images/clip/embed-jobs/{job_id}/result` |
| 图片 BGE | GET | `/v1/images/bge/embed-jobs/{job_id}/result` |
| 视频 CLIP | GET | `/v1/videos/clip/embed-jobs/{job_id}/result` |
| 视频 BGE | GET | `/v1/videos/bge/embed-jobs/{job_id}/result` |

- 仅当 `status=succeeded` 时返回完整结果；须与提交时同前缀的 GET  
- 否则 HTTP 409；路径或类型不匹配会 404

---

## 五、API 响应体字段（按 schema 顺序）

### 5.1 公共 `MediaMetadata`

| 序号 | 字段 | 类型 | 中文说明 |
|------|------|------|----------|
| 1 | `width` | int \| null | 图片宽度（视频常为 null） |
| 2 | `height` | int \| null | 图片高度 |
| 3 | `source` | `"oss"` | 固定从 OSS 读取 |
| 4 | `content_type` | string \| null | 请求中的 MIME 类型 |
| 5 | `bucket` | string \| null | 实际使用的 OSS 桶名（来自环境变量） |
| 6 | `object_key` | string \| null | OSS 对象键 |

### 5.2 图片 CLIP 响应 `ImageClipEmbedResponse`

| 序号 | 字段 | 中文说明 |
|------|------|----------|
| 1 | `media_id` | 媒体 ID |
| 2 | `modality` | 固定 `"image"` |
| 3 | `scheme` | 固定 `"clip"` |
| 4 | `vector_dim` | 向量维度 |
| 5 | `embedding_model` | 模型名称 |
| 6 | `embedding` | 图片向量（`return_vector=false` 时为 null） |
| 7 | `metadata` | 媒体元数据 |
| 8 | `milvus` | Milvus 写入信息（未落库时为 null） |

### 5.3 图片 BGE 响应 `ImageBgeEmbedResponse`

在 5.2 基础上：`scheme` 为 `"bge"`，增加 `caption`（使用的描述文本），无单独 `modality` 差异。

| 序号 | 字段 | 中文说明 |
|------|------|----------|
| 1~5 | 同 5.2 | — |
| 6 | `caption` | 输入的描述文本 |
| 7 | `embedding` | BGE 文本向量 |
| 8 | `metadata` | 元数据 |
| 9 | `milvus` | Milvus 信息 |

### 5.4 视频 CLIP 响应 `VideoClipEmbedResponse`

| 序号 | 字段 | 中文说明 |
|------|------|----------|
| 1 | `media_id` | 视频 ID |
| 2 | `modality` | 固定 `"video"` |
| 3 | `scheme` | 固定 `"clip"` |
| 4 | `vector_dim` | 向量维度 |
| 5 | `embedding_model` | 模型名 |
| 6 | `embedding` | 整片 CLIP 向量（段内 K-means 选帧 → 段内/全片 softmax 池化） |
| 7 | `metadata` | 元数据 |
| 8 | `milvus` | Milvus 信息 |

### 5.5 文本查询响应 `QueryVectorPayload`

| 序号 | 字段 | 中文说明 |
|------|------|------|----------|
| 1 | `embedding_model` | 模型名 |
| 2 | `vector_dim` | 维度 |
| 3 | `embedding` | 查询向量 |

### 5.9 健康检查 `GET /health` — `HealthResponse`

| 序号 | 字段 | 中文说明 |
|------|------|----------|
| 1 | `status` | 固定 `"ok"` |
| 2 | `service` | 固定 `"embedding_service"` |
| 3 | `vector_store` | 当前向量存储模式 |
| 4 | `clip_model_path` | CLIP 模型路径 |
| 5 | `clip_device` | CLIP 设备 |
| 6 | `bge_model_name` | BGE 模型名 |
| 7 | `bge_device` | BGE 设备 |

### 5.10 统计 `GET /stats`

| 序号 | 字段 | 中文说明 |
|------|------|----------|
| 1 | `service` | 服务名 |
| 2 | `version` | 服务版本 |
| 3 | `tasks` | 任务统计：`total` / `pending` / `running` / `succeeded` / `failed` |
| 4 | `models.clip_loaded` | CLIP 是否已加载 |
| 5 | `models.bge_loaded` | BGE 是否已加载 |

---

## 六、错误码

| 序号 | 错误码 | 中文说明 |
|------|--------|----------|
| 1 | `CONFIGURATION_ERROR` | 配置错误（如模型路径不存在） |
| 2 | `VALIDATION_ERROR` | 请求参数校验失败 |
| 3 | `MEDIA_DOWNLOAD_ERROR` | 媒体下载失败（URL/OSS） |
| 4 | `MEDIA_PROCESSING_ERROR` | 媒体处理失败（解码、分段等） |
| 5 | `MODEL_ERROR` | 模型加载或推理失败 |
| 6 | `VECTOR_STORE_ERROR` | Milvus 读写失败 |
| 7 | `TASK_LIMIT_ERROR` | 任务数超过 `MAX_TASKS` |
| 8 | `INTERNAL_ERROR` | 未预期内部错误 |

---

## 七、接口与参数对照速查

| 接口 | 方法 | 请求体 schema | 同步/异步 |
|------|------|---------------|-----------|
| `/v1/images/clip/embed-jobs` | POST | `ImageClipEmbedRequest` | 异步 |
| `/v1/images/clip/embed-jobs/{job_id}` | GET | `job_id` | 查状态 |
| `/v1/images/clip/embed-jobs/{job_id}/result` | GET | `job_id` | 取结果 |
| `/v1/images/bge/embed-jobs` | POST | `ImageBgeEmbedRequest` | 异步 |
| `/v1/images/bge/embed-jobs/{job_id}` | GET | `job_id` | 查状态 |
| `/v1/images/bge/embed-jobs/{job_id}/result` | GET | `job_id` | 取结果 |
| `/v1/videos/clip/embed-jobs` | POST | `VideoClipEmbedRequest` | 异步 |
| `/v1/videos/clip/embed-jobs/{job_id}` | GET | `job_id` | 查状态 |
| `/v1/videos/clip/embed-jobs/{job_id}/result` | GET | `job_id` | 取结果 |
| `/v1/videos/bge/embed-jobs` | POST | `VideoBgeEmbedRequest` | 异步 |
| `/v1/videos/bge/embed-jobs/{job_id}` | GET | `job_id` | 查状态 |
| `/v1/videos/bge/embed-jobs/{job_id}/result` | GET | `job_id` | 取结果 |
| `/v1/query/clip/embed` | POST | `DualQueryEmbedRequest` | **同步** |
| `/v1/query/bge/embed` | POST | `DualQueryEmbedRequest` | **同步** |
| `/health` | GET | 无 | — |
| `/stats` | GET | 无 | — |

---

## 八、项目脚本与入口

| 路径 | 说明 |
|------|------|
| `start_embedding_service.py` | 推荐启动入口；终端打印 `http://127.0.0.1:<port>/docs` |
| `python -m embedding_service.api` | 与上等价（需在项目根目录） |
| `scripts/download_models.py` | 下载模型（全平台） |
| `scripts/download_models.ps1` | 同上，**仅 Windows** |
| `python -m embedding_service.milvus_init` | 创建 Milvus collection（需 `MILVUS_URI` 可用） |
| `python -m embedding_service.monitor --port 8030` | 轮询 `/health`、`/stats` |

以下路径**不在仓库中**（旧文档勿引用）：`scripts/start_embedding_service.ps1`、`scripts/init_milvus.ps1`、`scripts/test_embedding_service.ps1`。

详见 [`scripts/README.md`](../scripts/README.md)。
