# 文案自动成片 — Flow B FastAPI

口播文案 → LLM 语义分句 → 外部 TTS（wav URL）→ 素材绑定 → FFmpeg 裁切/拼接 → 硬/软字幕 → `workspaces/{id}/deliverables/output.mp4`。

## 环境

```bash
sudo apt install ffmpeg   # Linux
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # 仅运行 API
# .venv/bin/pip install -r requirements-dev.txt   # 含 pytest，跑测试时用
cp .env.example .env      # 填写 ARK_API_KEY 等
```

## Docker 一键启动

需已安装 **Docker + Compose 插件**。完整运维见 [`DOCKER_OPS.md`](DOCKER_OPS.md)。

```bash
# 首次
cp .env.example .env    # 填写 ARK_API_KEY 等（见 .env.example）
mkdir -p workspaces fonts
sudo chown -R 1000:1000 workspaces   # 字体见 fonts/README.md

# 一键构建并启动
bash scripts/deploy.sh
```

启动后：

- Swagger：http://127.0.0.1:8787/docs  
- 健康检查：`curl http://127.0.0.1:8787/api/v1/health`  
- 日志：`docker compose logs -f app`  
- 发版/改代码后：`docker compose up -d --build`（不要只用 `restart`）

## 启动 API（本地 Python，非 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 含 oss2（compose 上传 OSS 必需）

export WORKSPACES_ROOT="$PWD/workspaces"
set -a && source .env && set +a            # ARK_API_KEY、ALIYUN_OSS_* 等

PYTHONPATH=src .venv/bin/python -m videoaudiotext.api
# 或: PYTHONPATH=src .venv/bin/videoaudiotext-api
```

> **勿用系统裸 `python3` 启动**：未装 `oss2` 时 compose 会在 upload 阶段失败（`50002 upload video to OSS failed`）。Docker 镜像已内置 oss2。

# 交互式文档: http://127.0.0.1:8787/docs

## 测试

```bash
.venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=src python3 -m pytest tests/test_api_*.py -q
```

## 目录

| 路径 | 说明 |
|------|------|
| `src/videoaudiotext/api/` | FastAPI 服务（路由、Job、工作区） |
| `src/videoaudiotext/` | 共享核心（分句 / 字幕 / 合成） |
| `workspaces/` | 工作区持久化（运行时数据） |
| `fonts/` | 硬字幕字体（见 [`fonts/README.md`](fonts/README.md)，建议思源黑体） |
| `docs/` | **文档索引** [`docs/README.md`](docs/README.md) · 联调 / 接口 / 设计 |

## 配置（`.env`）

```env
# 分句
ARK_API_KEY=...
DOUBAO_MODEL=doubao-seed-2-0-lite-260428
TEXT_SPLIT_MODE=llm          # llm | rule | auto

# API
# API_PORT=8787
# WORKSPACES_ROOT=./workspaces
# UVICORN_WORKERS=1
# GLOBAL_JOB_WORKERS=4
# CLIP_BUILD_WORKERS=4
# MAINTENANCE_API_TOKEN=...   # 维护接口鉴权
```

完整配置见 `src/videoaudiotext/config/` 与 `.env.example`。

## 文档

**入口：** [`docs/README.md`](docs/README.md)（按角色选读）

| 场景 | 文档 |
|------|------|
| Docker 部署运维 | [`DOCKER_OPS.md`](DOCKER_OPS.md) |
| Swagger 联调（推荐） | [`docs/api-flow-b-fastapi-walkthrough.md`](docs/api-flow-b-fastapi-walkthrough.md) |
| 接口规范 / 错误码 | [`docs/api-flow-b-fastapi-service-api.md`](docs/api-flow-b-fastapi-service-api.md) |
| 字体 | [`fonts/README.md`](fonts/README.md) |

## API 流程（摘要）

1. `POST /api/v1/split/run` → 轮询 — LLM 分句（可选）
2. `POST /api/v1/audio/process/run` → 轮询 — 拉取每句 OSS wav URL、混音与字幕（可选）
3. `POST /api/v1/visual/preview/run` — 字幕/封面预览（可选）
4. `POST /api/v1/compose/run` → 轮询 — 异步合成（内部拉取素材）→ **OSS 成片与最终 ASS**（`result.output_video_url`、`result.output_subtitle_ass_url`）

逐步操作与 JSON 示例见 [`docs/api-flow-b-fastapi-service-api.md`](docs/api-flow-b-fastapi-service-api.md)。
