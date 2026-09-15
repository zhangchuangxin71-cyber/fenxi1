# Linux 复现指南

> 解压后进入**含 `start_embedding_service.py` 的目录**，在 Linux 上按本文操作。

## 项目说明

**中文多模态检索**：Chinese-CLIP / BGE 将文本、图片、视频编码为向量，可写入 **Milvus** 做相似度检索。

| 模块 | 说明 |
|------|------|
| `embedding_service/` | 向量化 HTTP API（核心，端口在 `.env` 配置） |
| `embed_core/` | 编码与 Milvus 封装（API 依赖） |
| `picture/`、`video_retrieval/` | **仅检索**（可选；本地 MLLM/入库脚本已移除） |

**入库**：`embedding_service` + OSS + `write_vector=true` → Milvus。Milvus 地址由运维提供 `MILVUS_URI`。

---

## 环境要求

- Linux，Python **3.10+**
- 内存建议 **8GB+**
- 视频任务需 **ffmpeg**：`sudo apt install ffmpeg`（或等价包管理器）
- 模型在首次启动时自动下载（国内建议在 `.env` 设 `HF_USE_MIRROR=1`）

---

## 复现步骤

### 1. 安装

```bash
cd /path/to/multimodal-retrieval-*

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip && pip install -r requirements.txt

cp .env.example .env
# 编辑 .env：填写端口；图片/视频任务填 OSS；写 Milvus 时再填 MILVUS_URI
```

### 2. 单元测试（不需启动服务、不需模型）

```bash
python scripts/verify_reproduction.py --skip-api
# 预期：13 passed
```

### 3. 启动 API 并验证（文本）

```bash
python start_embedding_service.py   # 首次启动会下载模型，需数分钟
```

**新开终端**（文本向量，不需 OSS/Milvus）：

```bash
source .venv/bin/activate
python scripts/verify_reproduction.py
```

**预期**：`/health` 返回 `"status":"ok"`；CLIP 文本接口返回 1024 维 `embedding`。

接口文档：`http://127.0.0.1:<端口>/docs`（与 `EMBEDDING_SERVICE_PORT` 一致）

### 4. 图片/视频 + Milvus（完整验收）

`.env` 中按需填写 OSS、`MILVUS_URI`（写库验收时两者都需要）。

```bash
python -m embedding_service.milvus_init
python scripts/oss_milvus_ingest_verify.py \
  --image-object-key "桶内/图片.jpg" \
  --video-object-key "桶内/视频.mp4"
```

**预期**：检索 Top-1 命中刚入库的媒体。

### 5. 导出视频 CLIP 整片向量（可选）

将任务结果（顶层 `embedding`）保存为本地 JSON（不写 Milvus 也可）：

```bash
python scripts/fetch_video_clip_full_result.py \
  --base-url http://127.0.0.1:<端口> \
  --object-key "桶内/视频.mp4" \
  --media-id my_video_001
```

默认保存到 `runtime/outputs/video_clip_<media_id>_<job_id>_<时间>.json`。说明见 [`scripts/README.md`](scripts/README.md)。

---

## Docker 部署（可选，不含 Milvus）

若已安装 Docker，可跳过 venv / pip / 宿主机 ffmpeg，直接容器化运行 embedding API：

```bash
cp .env.example .env
# 编辑 .env：EMBEDDING_SERVICE_PORT=8030，按需填写 OSS

bash scripts/deploy.sh
curl http://127.0.0.1:8030/health
```

- compose 强制 `VECTOR_STORE=none`，不写 Milvus
- `./models` 挂载进容器；无模型时首次启动自动下载（建议 `HF_USE_MIRROR=1`）
- 详细说明见 [DEPLOY.md 第四节](DEPLOY.md#四docker-部署不含-milvus)

---

## 验收清单

- [ ] `pip install -r requirements.txt` 成功
- [ ] `python scripts/verify_reproduction.py --skip-api` → 13 passed
- [ ] 服务启动无报错
- [ ] `python scripts/verify_reproduction.py` → 验收通过
- [ ] `.env` 已填写 OSS 四项与 `MILVUS_URI`
- [ ] Milvus 入库与检索成功

---

## 常见问题

| 问题 | 处理 |
|------|------|
| 缺端口 | `.env` 中设置 `EMBEDDING_SERVICE_PORT=<可用端口>` |
| 模型下载失败 | `.env` 设 `HF_USE_MIRROR=1` |
| `write_vector` 失败 | 检查 `MILVUS_URI`，先执行 `milvus_init` |
| 视频任务失败 | 安装 ffmpeg，检查 OSS 配置与 `object_key` |

---

更多细节：[README.md](README.md) · [DEPLOY.md](DEPLOY.md) · [embedding_service/README.md](embedding_service/README.md)
