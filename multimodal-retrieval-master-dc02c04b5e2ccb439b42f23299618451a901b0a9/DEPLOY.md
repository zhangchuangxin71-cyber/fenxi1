# 部署指南

本文说明如何将 **embedding 向量化服务** 部署到其他服务器。`picture/`、`video_retrieval/` 为可选**检索**组件（已移除本地 MLLM 与入库脚本），不必与 API 同机部署。

---

## 一、打包上传

```bash
bash scripts/package_for_upload.sh
```

生成 `release/multimodal-retrieval-*.tar.gz`，详见 [`UPLOAD.md`](UPLOAD.md)。

## 二、最小部署包（仅 API 服务）

上传以下目录/文件即可运行 `embedding_service`：

```
项目根目录/
├── embed_core/                 # 共享编码逻辑（必选）
├── embedding_service/          # FastAPI 服务（必选）
├── scripts/
│   ├── download_models.py      # 首次下载模型（推荐）
│   ├── deploy.sh               # Docker 一键部署
│   └── README.md
├── start_embedding_service.py  # 启动入口
├── requirements.txt            # Python 依赖（CPU 版）
├── Dockerfile                  # Docker 镜像构建
├── docker-compose.yml          # Docker 编排
├── .dockerignore               # Docker 构建排除项
├── .env.example                # 环境变量模板
├── tests/                      # 单元测试
├── CHANGELOG.md
├── DEPLOY.md                   # 本文件
└── README.md
```

**不要上传**（在目标机生成或挂载）：

| 路径 | 说明 |
|------|------|
| `runtime/` | 日志、`tasks.db`、视频临时文件 |
| `models/` 大权重 | 首次启动可自动下载，或事先 `download_models.py` |
| `picture/profiles/` | 本地 Milvus 向量 + metadata |
| `video_retrieval/` | 本地检索管线（API 不依赖） |
| `picture/` | 本地检索管线（API 不依赖） |
| `.env` | 含密钥，在服务器单独创建 |
| `__pycache__/`、`*.log` | 缓存与日志 |

---

## 三、服务器环境

**裸机部署**需要：

- Python **3.10+**
- 建议 **8GB+ 内存**（CLIP + BGE 同时加载）
- 视频 CLIP 需要系统安装 **ffmpeg**
- 默认 CPU 依赖；需 GPU 时请自行安装 CUDA 版 `torch`，并设置 `CLIP_DEVICE=cuda`、`BGE_DEVICE=cuda`

**Docker 部署**需要：

- Docker 与 Docker Compose 插件
- 建议宿主机 **8GB+ 内存**（compose 默认 `mem_limit=12g`）
- 无需在宿主机安装 Python / ffmpeg（已包含在镜像内）

---

## 四、Docker 部署（不含 Milvus）

项目根目录提供 `Dockerfile`、`docker-compose.yml` 与 `scripts/deploy.sh`，用于在容器内运行 **embedding 向量化 API**。

本方案**不依赖 Milvus**：compose 强制 `VECTOR_STORE=none`，仅生成向量、不写库。API 请求请保持 `write_vector=false`（默认值）。

### 4.1 涉及文件

| 文件 | 说明 |
|------|------|
| `Dockerfile` | 基于 `python:3.12-slim`，安装 CPU 版 PyTorch、ffmpeg、OpenCV 依赖 |
| `docker-compose.yml` | 构建并启动 `multimodal-embedding-service` 容器 |
| `scripts/deploy.sh` | 检查 `.env`、端口与 Docker 环境后执行 `docker compose up -d --build` |
| `.dockerignore` | 排除 `.venv`、`runtime/`、大模型权重等，减小构建上下文 |

### 4.2 前置条件

1. 已安装 Docker 与 Docker Compose 插件
2. 项目根目录存在 `.env`（由 `.env.example` 复制）
3. `.env` 中已设置 **`EMBEDDING_SERVICE_PORT`**（如 `8030`）
4. 若需图片/视频异步任务，配置 **阿里云 OSS** 四项（`ALIYUN_OSS_*`）

### 4.3 模型与持久化

| 挂载 | 容器路径 | 说明 |
|------|----------|------|
| `./models` | `/app/models` | CLIP + BGE 权重；宿主机已有模型时直接复用 |
| Docker 卷 `embedding_runtime` | `/app/runtime` | 日志、`tasks.db`、视频临时文件 |

**`models/` 目录权限**：容器内以 `appuser`（UID/GID **1000**）运行。若开启 `AUTO_DOWNLOAD_MODELS=1` 且宿主机 `models/` 由 root 创建或不可写，首次自动下载会失败。部署前可执行：

```bash
mkdir -p models
sudo chown -R 1000:1000 models
```

模型加载策略（二选一）：

- **A）宿主机已有 `models/`**（推荐）：直接 bind mount，无需重新下载
- **B）首次无模型**：保持 `.env` 中 `AUTO_DOWNLOAD_MODELS=1`（默认），首次启动自动下载；国内建议 `HF_USE_MIRROR=1`

手动预下载（可选）：

```bash
python scripts/download_models.py --use-mirror
```

### 4.4 一键部署

在项目根目录执行：

```bash
cp .env.example .env
# 编辑 .env，至少设置：
#   EMBEDDING_SERVICE_PORT=8030
#   ALIYUN_OSS_*（图片/视频 embed-jobs 需要）

bash scripts/deploy.sh
```

脚本会：

1. 检查 `.env` 与 `EMBEDDING_SERVICE_PORT`
2. 提示 `models/` 是否完整（缺失且关闭自动下载时给出警告）
3. 检查 `models/` 是否对容器用户（UID 1000）可写
4. 构建镜像并后台启动容器
5. 打印健康检查与 Swagger 地址（含首次部署可能 `unhealthy` 的说明）

### 4.5 验证

```bash
curl http://127.0.0.1:8030/health
```

浏览器打开：`http://127.0.0.1:8030/docs`

### 4.6 常用运维命令

```bash
# 查看状态
docker compose ps

# 跟踪日志
docker compose logs -f app

# 重启
docker compose restart app

# 停止并删除容器（保留 runtime 卷与 models/）
docker compose down

# 重新构建并启动
docker compose up -d --build
```

### 4.7 环境变量说明（Docker 特有）

compose 会覆盖或补充以下项（无需在 `.env` 重复设置，除非要改默认值）：

| 变量 | Docker 默认值 | 说明 |
|------|---------------|------|
| `VECTOR_STORE` | `none` | 不写 Milvus |
| `CHINESE_CLIP_MODEL_PATH` | `/app/models` | 容器内模型目录 |
| `EMBEDDING_SERVICE_HOST` | `0.0.0.0` | 监听所有网卡 |
| `HF_USE_MIRROR` | `1` | 国内 HF 镜像 |
| `AUTO_DOWNLOAD_MODELS` | `1` | 缺模型时自动下载 |
| `EMBEDDING_MEM_LIMIT` | `12g` | compose 内存上限（宿主机可调） |

其余变量（OSS、设备、批大小等）仍从 `.env` 读取，完整列表见 [`embedding_service/PARAMS.md`](embedding_service/PARAMS.md)。

### 4.8 注意事项

- **首次启动较慢**：需下载（若无模型）并加载 CLIP + BGE；健康检查 `start-period=600s`（约 10 分钟）。在此之前 `docker compose ps` 可能显示 `unhealthy`，属正常现象，请用 `docker compose logs -f app` 查看进度，待 `curl .../health` 返回 200 即就绪
- **仅文本向量试算**：只需配置端口，OSS 可不填
- **图片/视频任务**：必须配置 OSS；容器内已含 ffmpeg
- **GPU**：当前 Dockerfile 为 CPU 版；需 GPU 时请自行改基础镜像与 `torch` 依赖，并在 compose 中配置 NVIDIA runtime
- **Milvus**：本 Docker 方案不涉及；若后续需要写库，请改用裸机部署并配置 `VECTOR_STORE=milvus`

---

## 五、裸机部署步骤

```bash
# 1. 解压代码到目标目录
cd /opt/chinese-clip

# 2. 虚拟环境
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. 依赖
pip install -r requirements.txt

# 4. 模型（二选一）
# A) 启动时自动下载（默认 AUTO_DOWNLOAD_MODELS=1）
export HF_USE_MIRROR=1        # 国内建议

# B) 手动预下载
python scripts/download_models.py --use-mirror

# 5. 环境变量
export EMBEDDING_SERVICE_PORT=8030
export EMBEDDING_SERVICE_HOST=0.0.0.0

# 6. 启动
python start_embedding_service.py
```

验证：

```bash
curl http://127.0.0.1:8030/health
```

文档：`http://127.0.0.1:8030/docs`

---

## 六、Milvus（默认）

项目默认 `VECTOR_STORE=milvus`（见 `.env.example`）。Milvus **不在本仓库部署**，由运维提供 `MILVUS_URI`。以下场景需要 Milvus 可用：

- **embedding API** 请求里 `write_vector=true` 写在线向量库
- **本地检索**（`picture/`、`video_retrieval/`）的 CLIP / BGE 稠密召回

仅做 embedding 试算、且 `write_vector=false`、且不跑本地检索时，可设 `VECTOR_STORE=none` 跳过 Milvus。

```bash
# .env 中配置 MILVUS_URI 后
python -m embedding_service.milvus_init
python start_embedding_service.py
```

本地检索需 Milvus 中已有向量（经 `embedding_service` 的 `write_vector=true` 写入）；`video_retrieval` / `picture` 仅负责检索。

---

## 七、环境变量速查

复制 `.env.example` 为项目根 `.env` 后修改。关键项：

| 变量 | 说明 |
|------|------|
| `EMBEDDING_SERVICE_PORT` | **必填**，如 `8030` |
| `CHINESE_CLIP_MODEL_PATH` | 默认项目根 `models/` |
| `AUTO_DOWNLOAD_MODELS` | `1` 缺模型时自动下载 |
| `HF_USE_MIRROR` | `1` 使用 HF 镜像 |
| `CLIP_DEVICE` / `BGE_DEVICE` | `cpu` 或 `cuda` |
| `VECTOR_STORE` / `MILVUS_URI` | 默认 `milvus`；URI 由运维提供 |

完整列表见 [`embedding_service/PARAMS.md`](embedding_service/PARAMS.md)。

---

## 八、API 结构（v0.3.0）

**同步文本**

- `POST /v1/query/clip/embed`
- `POST /v1/query/bge/embed`

**异步媒体**（每种：POST + GET 状态 + GET 结果）

- 图片 CLIP/BGE：`/v1/images/{clip|bge}/embed-jobs`
- 视频 CLIP/BGE：`/v1/videos/{clip|bge}/embed-jobs`

---

## 九、打包示例

**推荐**（跨平台）：

```bash
python scripts/pack_embedding_service.py -o embedding-service.tar.gz
```

或手动：

```bash
tar czvf embedding-service.tar.gz \
  embed_core embedding_service scripts \
  start_embedding_service.py requirements.txt \
  Dockerfile docker-compose.yml .dockerignore .env.example \
  DEPLOY.md README.md CHANGELOG.md \
  --exclude='__pycache__' --exclude='*.pyc'
```

上传到服务器后解压，按第五节（裸机）或第四节（Docker）启动。

---

## 十、完整仓库（含本地检索）

若还需 **离线索引与检索**，额外上传：

- `picture/`、`video_retrieval/`（依赖已包含在 `requirements.txt`）

与 `embedding_service` 相互独立，可分机部署。
