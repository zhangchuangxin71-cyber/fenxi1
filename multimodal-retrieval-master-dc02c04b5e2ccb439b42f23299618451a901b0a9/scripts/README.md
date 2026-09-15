# scripts/

辅助脚本目录（不参与服务运行时加载）。

## 脚本一览

| 文件 | 平台 | 说明 |
|------|------|------|
| [`deploy.sh`](deploy.sh) | Linux/macOS | **Docker 一键部署** embedding API（不含 Milvus） |
| [`check_before_upload.sh`](check_before_upload.sh) | Linux/macOS | **上传前推荐**：测试 + 清理 + 打 tar 包 |
| [`package_for_upload.sh`](package_for_upload.sh) | Linux/macOS | 整仓源码发布包 |
| [`pack_embedding_service.py`](pack_embedding_service.py) | 全平台 | 仅 embedding API 精简包 |
| [`clean_runtime.sh`](clean_runtime.sh) | Linux/macOS | 清理日志与运行时数据 |
| [`download_models.py`](download_models.py) | 全平台 | 下载 Chinese-CLIP 与 BGE |
| [`download_models.ps1`](download_models.ps1) | Windows | 同上（PowerShell） |
| [`verify_reproduction.py`](verify_reproduction.py) | 全平台 | **验收复现**：单元测试 + API 健康 + CLIP 文本向量 |
| [`verify_milvus_retrieval.py`](verify_milvus_retrieval.py) | 全平台 | 检查本地检索是否接 Milvus |
| [`oss_milvus_ingest_verify.py`](oss_milvus_ingest_verify.py) | 全平台 | OSS 入库 Milvus + 检索验证 |
| [`benchmark_video_clip_jobs.py`](benchmark_video_clip_jobs.py) | 全平台 | 视频 CLIP `/result` 耗时与结构检查 |
| [`fetch_video_clip_full_result.py`](fetch_video_clip_full_result.py) | 全平台 | 提交视频 CLIP 任务并保存整片 embedding JSON |

## 常用命令

```bash
# Docker 部署（项目根目录，不含 Milvus）
bash scripts/deploy.sh

# 模型
python scripts/download_models.py --use-mirror

# 上传前
bash scripts/check_before_upload.sh

# 验证
python scripts/verify_reproduction.py          # 见 QUICKSTART.md
python scripts/verify_reproduction.py --skip-api
python scripts/verify_milvus_retrieval.py
python -m unittest discover -s tests -v
```

## `fetch_video_clip_full_result.py` — 保存视频 CLIP 结果

提交 embedding API 异步视频 CLIP 任务，轮询完成后拉取 **`GET .../result`**（仅顶层 `embedding` 整片向量），保存为本地 JSON。

**前置**：服务已启动；`.env` 已配置 OSS。提交时需 **`return_vector=true`**（默认），否则 `embedding` 为 null。

```bash
python scripts/fetch_video_clip_full_result.py \
  --base-url http://127.0.0.1:8030 \
  --object-key "桶内/你的视频.mp4" \
  --media-id my_video_001

python scripts/fetch_video_clip_full_result.py \
  --object-key "桶内/你的视频.mp4" \
  -o runtime/outputs/my_video.json

python scripts/fetch_video_clip_full_result.py --job-id <job_id> -o runtime/outputs/out.json
```

未指定 `-o` 时，默认保存到 `runtime/outputs/video_clip_<media_id>_<job_id>_<时间>.json`。

| 参数 | 说明 |
|------|------|
| `--base-url` | API 地址 |
| `--object-key` | OSS 对象键（新任务必填） |
| `--media-id` | 业务 ID |
| `--job-id` | 已有任务 ID |
| `--return-vector` / `--no-return-vector` | 是否返回 embedding（默认 true） |
| `--write-vector` | 是否写入 Milvus `media_video_clip` |
| `--sample-fps` / `--max-frames` / `--top-k-frames` / `--segment-seconds` | 对应 `configuration` |

## `benchmark_video_clip_jobs.py`

测量 `GET /v1/videos/clip/embed-jobs/{job_id}/result` 耗时，并校验响应**仅含**顶层 `embedding`（无 `frame_level` / `segment_level`）。

```bash
python scripts/benchmark_video_clip_jobs.py --object-key "桶内/视频.mp4"
python scripts/benchmark_video_clip_jobs.py --job-id <job_id> --save-vector
```

## 不在本目录的入口

| 目的 | 命令 |
|------|------|
| Docker 部署 embedding API | `bash scripts/deploy.sh`（见 [`DEPLOY.md`](../DEPLOY.md) 第四节） |
| 启动 embedding API（裸机） | `python start_embedding_service.py` |
| 初始化 Milvus | `python -m embedding_service.milvus_init` |
| 健康检查 | `curl http://127.0.0.1:8030/health` |

部署说明见 [`DEPLOY.md`](../DEPLOY.md)、[`UPLOAD.md`](../UPLOAD.md)。
