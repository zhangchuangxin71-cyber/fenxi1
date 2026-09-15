# chinese-clip

面向中文多模态场景的工程化项目：**向量化 HTTP API**（`embedding_service`）负责编码与入库；`picture/`、`video_retrieval/` **仅做检索**（不再含 MLLM 打标与本地入库脚本）。向量默认写入 **Milvus**。

---

## 快速开始（Linux）

完整复现步骤见 [`QUICKSTART.md`](QUICKSTART.md)。启动 **embedding_service** 如下。

### 1. 配置端口与环境变量

在项目根目录（含 `start_embedding_service.py`）：

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

```bash
EMBEDDING_SERVICE_PORT=<端口>         # 必填
ALIYUN_OSS_ENDPOINT=...              # 图片/视频 embed-jobs 必填
ALIYUN_OSS_ACCESS_KEY_ID=...
ALIYUN_OSS_ACCESS_KEY_SECRET=...
ALIYUN_OSS_BUCKET=...
# MILVUS_URI=...                     # write_vector=true 时必填
```

也可临时指定端口（不推荐长期使用）：

```bash
export EMBEDDING_SERVICE_PORT=8030
```

### 2. 初始化 Milvus（首次、写库前）

```bash
python -m embedding_service.milvus_init
```

### 3. 启动服务

在项目根目录执行（**先确保端口已配置**）：

```bash
# 方式 A：已在 .env 中设置 EMBEDDING_SERVICE_PORT=8030
python start_embedding_service.py

# 方式 B：未使用 .env 时，启动前 export 端口
export EMBEDDING_SERVICE_PORT=8030
python start_embedding_service.py
```

等价命令：

```bash
python -m embedding_service.api
```

首次启动会自动下载模型（数分钟）。启动成功后：

- 健康检查：`curl http://127.0.0.1:8030/health`
- 接口文档：`http://127.0.0.1:8030/docs`

### 4. 验收

```bash
python scripts/verify_reproduction.py --skip-api   # 不需启动服务
python scripts/verify_reproduction.py              # 需服务已启动
```

更多部署项见 [`DEPLOY.md`](DEPLOY.md)（含 Docker）；API 字段见 [`embedding_service/README.md`](embedding_service/README.md)。

### 可选：Docker 部署（不含 Milvus）

已安装 Docker 时，可在项目根目录一键启动，无需本地 Python 虚拟环境：

```bash
cp .env.example .env
# 编辑 .env：EMBEDDING_SERVICE_PORT=8030，按需填写 OSS

bash scripts/deploy.sh
curl http://127.0.0.1:8030/health
```

首次部署需下载/加载模型，可能耗时数分钟；`models/` 需对容器用户（UID 1000）可写，详见 [`DEPLOY.md` 第四节](DEPLOY.md#四docker-部署不含-milvus)。

---

## 目录结构

```
chinese-clip/
├── embed_core/              # 共享编码库（CLIP / BGE / 视频分段 / Milvus）
├── embedding_service/       # FastAPI 向量化 API（v0.3.0）
├── picture/                 # 图片本地检索（CLIP + BGE 双路）
├── video_retrieval/         # 视频本地检索（CLIP + 混合检索）
├── scripts/                 # 模型下载、Docker 部署、验证脚本等
├── tests/                   # 单元测试
├── runtime/                 # 日志、任务库（运行时生成）
├── models/                  # 模型权重（首次启动下载，见 models/MODELS.md）
├── start_embedding_service.py
├── requirements.txt
├── Dockerfile               # Docker 镜像（embedding API）
├── docker-compose.yml       # Docker 编排
├── .dockerignore
├── .env.example             # 配置模板，复制为 .env 后填写
├── QUICKSTART.md            # Linux 复现指南
├── DEPLOY.md                # 裸机与 Docker 部署
└── README.md
```

| 模块 | 说明 | 跑 embedding API 是否必需 |
|------|------|---------------------------|
| `embed_core/` | CLIP/BGE/视频分段/Milvus 共享库 | 是 |
| `embedding_service/` | FastAPI 向量化服务 | 是 |
| `picture/` | 图片检索（CLIP + BGE） | 否 |
| `video_retrieval/` | 视频检索（CLIP + Hybrid） | 否 |

---

## 本地生成内容

以下内容**在本机安装或运行后生成**，不要从别处拷贝：

| 路径 | 说明 |
|------|------|
| `.env` | 从 `.env.example` 复制后填写（含端口、OSS、Milvus 等） |
| `.venv/` | `python3 -m venv .venv` 后 `pip install -r requirements.txt` |
| `models/` | 启动时自动下载，或 `python scripts/download_models.py` |
| `runtime/` | 日志、`tasks.db` 等（Docker 部署时使用卷 `embedding_runtime`） |
| `picture/profiles/`、`video_retrieval/profiles/` | 检索用 metadata（向量在 Milvus） |

---

## 测试

不加载模型权重的单元测试：

```bash
python -m unittest discover -s tests -v
```

---

## API 概览（v0.3.0）

**同步文本**

- `POST /v1/query/clip/embed`
- `POST /v1/query/bge/embed`

**异步媒体**（POST 创建任务 + GET 状态 + GET 结果）

- 图片：`/v1/images/{clip|bge}/embed-jobs`
- 视频：`/v1/videos/{clip|bge}/embed-jobs`

详细接口与字段见 [`embedding_service/README.md`](embedding_service/README.md)、[`embedding_service/PARAMS.md`](embedding_service/PARAMS.md)。

---

## Milvus 向量库（默认）

默认 `VECTOR_STORE=milvus`（见 `.env.example`）。向量库使用**外部 Milvus 实例**（由运维提供 `MILVUS_URI`），本仓库不含 Milvus 部署。

```bash
# .env 示例
VECTOR_STORE=milvus
MILVUS_URI=http://<milvus-host>:19530
```

```bash
python -m embedding_service.milvus_init   # Milvus 可用后执行一次
python start_embedding_service.py         # 见上文「启动 embedding_service」
```

调用 embedding API 时设 `write_vector=true` 写入 Milvus。仅生成向量、不写库时可设 `VECTOR_STORE=none`。

**本地视频检索**在 Milvus 已有向量后启动：

```bash
python -m video_retrieval.api --profile default
```

Milvus 集合命名（`{MILVUS_COLLECTION_PREFIX}_…_{profile}`）：

| 用途 | kind |
|------|------|
| 视频 CLIP 整片（embedding_service 写入；`video_retrieval` CLIP 模式查询） | `media_video_clip` |
| 视频 Hybrid BGE 稠密 | `hybrid_video_bge` |
| 图片 CLIP / BGE | `picture_clip` / `picture_bge` |

---

## 本地检索（可选）

与 `embedding_service` 无运行时依赖，可分机部署：

- `picture/`：图片 CLIP + BGE 检索（见 `picture/dual_search_service.py`）
- `video_retrieval/`：视频 CLIP + Hybrid（BGE 稠密 + SQLite FTS 稀疏，FTS 无数据时退化为稠密）

入库统一走 **embedding_service**（`write_vector=true`）。详见 [`video_retrieval/README.md`](video_retrieval/README.md)。

---

## 文档索引

| 文档 | 内容 |
|------|------|
| [QUICKSTART.md](QUICKSTART.md) | Linux 安装与验收 |
| [DEPLOY.md](DEPLOY.md) | 裸机与 Docker 部署、环境变量 |
| [embedding_service/README.md](embedding_service/README.md) | API 接口说明 |
| [embedding_service/PARAMS.md](embedding_service/PARAMS.md) | 环境变量与请求字段 |
| [scripts/README.md](scripts/README.md) | 脚本说明 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更 |
| [UPLOAD.md](UPLOAD.md) | 打包上传规范（交付方） |
