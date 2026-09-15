# Changelog

本文件记录仓库主要变更，按时间倒序。

## [Unreleased]

### 检索模块精简（入库统一走 embedding_service）

- **picture/**：删除 MLLM 打标、`ingest.py`、`text_ingest.py`；保留 CLIP/BGE 检索与 `dual_search_service`
- **video_retrieval/**：删除 `pipeline.py`、豆包批量描述、`minimal_pipeline` / `rebuild_indexes` / `build_hybrid_index` 等本地入库；CLI 仅保留 `api`；保留 CLIP + Hybrid 检索
- 文档与启动检查改为：向量由 `embedding_service`（`write_vector=true`）写入 Milvus

### 交付与规范

- 新增 [`UPLOAD.md`](UPLOAD.md)、[`scripts/check_before_upload.sh`](scripts/check_before_upload.sh)
- 统一目录占位（`runtime/`、`models/.gitkeep`）、[`models/MODELS.md`](models/MODELS.md)
- 移除 Docker / Milvus 部署编排；Milvus 与容器化由运维负责
- `pyproject.toml` 版本对齐 `0.3.0`

### 向量检索

- 默认 `VECTOR_STORE=milvus`；移除 FAISS 索引与 `faiss-cpu` 依赖
- 视频 CLIP 召回、Hybrid BGE 稠密召回、图片 CLIP/BGE 检索均使用 Milvus
- Hybrid 稀疏检索仍为 SQLite FTS
- 新增/更新：`embed_core/milvus_store.py`、`video_retrieval/clip/milvus_index.py`、`video_retrieval/hybrid/milvus_dense.py`

## [0.3.0] - 2026-05-26

### 仓库整理

- 根目录补齐 `Dockerfile`、`docker-compose.yml`、`.env.example`、`tests/`、`CHANGELOG.md`
- 业务代码保持模块名：`embed_core/`、`embedding_service/`、`picture/`、`video_retrieval/`
- 依赖统一为 CPU 版 `requirements.txt`（`torch+cpu`、`pymilvus`）

### API（embedding_service v0.3.0）

- 文本同步：`POST /v1/query/{clip|bge}/embed`
- 图片/视频异步：`/v1/images|videos/{clip|bge}/embed-jobs` + 任务查询
- 媒体请求体使用 `object_key` + `content_type`（OSS 桶名走环境变量）
- 视频 CLIP 抽帧参数合并为 `configuration` 对象

### 架构

- `embedding_service` 与 `picture`/`video_retrieval` 解耦，共享 `embed_core`
- 启动时支持 `AUTO_DOWNLOAD_MODELS` 自动下载 CLIP/BGE
- 项目根 `.env` 由 `python-dotenv` 加载

### 部署

- Docker 构建上下文改为项目根目录
- 打包脚本：`python scripts/pack_embedding_service.py`
