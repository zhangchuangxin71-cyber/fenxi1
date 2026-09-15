# RAG Agent Codex

`rag_agent_codex` 是一个面向私有知识库问答和报告生成的 FastAPI + LangGraph 服务。
生产配置通过独立 `rag-retrieval-service` 读取 PageIndex / PostgreSQL 中已经入库的文档，
再由豆包 Ark 模型完成意图识别、问题改写、文档路由、RAG 问答、大纲生成、报告撰写
和报告编辑。内置 PostgreSQL retriever 仅保留 legacy 兼容，不属于当前生产主链路。

该服务是 `rag-platform` 的报告 Agent 子项目，复用统一的 Docker 部署、环境配置和 PageIndex PostgreSQL。

## 1. 功能概览

- `POST /agent/v1/chat/completions`：统一聊天入口，支持 SSE 流式输出
- 知识问答：从 PostgreSQL 的 PageIndex 表中召回 chunk 并回答
- 多问题处理：支持 query rewrite，将复合问题拆成多个检索子问题
- 多意图处理：同一次请求可同时触发闲聊、知识问答、报告大纲、报告撰写或报告编辑
- 报告生成：`outline_delta`、`report_text_delta` 与普通 `text_delta` 分流，便于前端左右区域分别渲染
- 引用返回：输出 `references`，包含使用过的文档与 chunk id
- 思考流：输出可读的 `thinking_delta`，避免泄露内部技术细节和 UUID

## 2. 环境要求

- Linux 或 macOS
- Python `3.11+`
- 可访问的 PostgreSQL / PageIndex 数据库
- 可访问的豆包 Ark API
- Docker 和 Docker Compose 插件，仅 Docker 部署时需要

所有命令默认在项目根目录执行：

```bash
cd /path/to/rag-platform/src/rag-report-agent
```

## 3. 环境变量

完整字段含义、生产推荐值和上线检查见 [环境变量说明](docs/environment-variables.md)。

大项目统一使用 `env/report-agent.env`，当前目录的 `.env` 是指向该文件的本地软链接。确认并编辑统一配置：

```bash
test -f ../../env/report-agent.env
vim ../../env/report-agent.env
```

本项目核心配置如下：

- `ARK_API_KEY`：豆包 Ark API Key，真实模型调用必填
- `ARK_BASE_URL`：豆包兼容接口地址，默认 `https://ark.cn-beijing.volces.com/api/v3`
- `MODEL_FAST`：意图识别、文档路由、查询改写等较轻任务使用
- `MODEL_SMART`：问答、报告写作、报告编辑等生成任务使用
- `DOUBAO_THINKING_TYPE`：豆包思考开关，默认 `enabled`
- `PG_DSN`：PageIndex PostgreSQL 连接串
- `RAG_TOP_K`：默认召回数量
- `RAG_RETRIEVAL_BACKEND`：检索后端，`postgres` 表示沿用本进程直连 PostgreSQL，`remote` 表示调用独立 `rag-retrieval-service`
- `RAG_RETRIEVAL_SERVICE_URL`：独立检索服务地址，默认 `http://127.0.0.1:8120`
- `RAG_RETRIEVAL_TIMEOUT_SECONDS`：调用独立检索服务的超时时间
- `DOC_ROUTER_MAX_DOCS`：文档路由最多读取的候选文档数
- `REPORT_IMAGE_ENABLED`：是否在报告正文中启用自动配图，默认 `false`
- `IMAGE_MCP_URL`：图片媒资检索接口地址，启用配图时必填
- `IMAGE_MCP_API_TYPE`：图片接口类型，正式媒资接口使用 `media_resources`

本地直接连接 PostgreSQL 时：

```env
PG_DSN=postgresql://postgres:change_me@127.0.0.1:5432/pageindex
```

Docker 中复用 `rag-platform` 外部 `pageindex` 网络中的 PostgreSQL 时，推荐改成：

```env
PG_DSN=postgresql://postgres:change_me@pageindex-postgres:5432/pageindex
DOCKER_NETWORK=pageindex
```

如果 PostgreSQL 是宿主机进程而不是 Docker 容器，可以使用：

```env
PG_DSN=postgresql://postgres:change_me@host.docker.internal:5432/pageindex
```

注意：容器里的 `127.0.0.1` 指的是 agent 容器自己，不是宿主机。

独立 RAG 检索服务接入配置：

```env
RAG_RETRIEVAL_BACKEND=remote
RAG_RETRIEVAL_SERVICE_URL=http://rag-retrieval-service:8120
RAG_RETRIEVAL_TIMEOUT_SECONDS=180
```

`remote` 模式只替换 chunk 检索来源，query rewrite、文档路由、相关性判定、问答生成和报告生成仍保留在 agent 侧。请求仍必须携带 `user_id`、`kb_id`，并在需要进入知识库检索时传入 `doc_ids` 或 `temp_doc_ids`；当前 graph 不会仅凭 `user_id + kb_id` 自动检索整个知识库。

`session_id` 是现有接口的必填业务标识，但服务不会据此恢复历史。历史、大纲和报告状态
仍由请求中的 `history`、`report_context` 显式传入。remote 检索时 `session_id` 会原样
传给检索服务，用于校验 `temp_doc_ids` 的上传 session 归属，并在 `stream_start` 中回显。

独立检索服务只返回 page 原文和工具生成的元信息 chunk，不再返回 node chunk。报告服务
按统一的 `chunk_id/document_name/path/content` 消费这些结果。每次 remote 调用会根据 query
长度、文档数量和总结/比较等 broad 标记，在 `4096–32768` 范围内动态计算
`max_return_tokens`；`top_k` 仍沿用原请求配置。

报告自动配图可选配置：

```env
REPORT_IMAGE_ENABLED=true
REPORT_IMAGE_MAX_SECTIONS=3
REPORT_IMAGE_TOP_K=3
IMAGE_MCP_URL=https://api.example.com/api/media-resources/search/image
IMAGE_MCP_API_TYPE=media_resources
IMAGE_MCP_API_KEY=
IMAGE_MCP_TENANT_CODE=
IMAGE_MCP_TIMEOUT_SECONDS=20
```

`REPORT_IMAGE_ENABLED` 是服务默认值。接口请求中也可以通过 `generation_config.enable_images` 单独控制本次报告是否配图；当该字段为 `true` 或 `false` 时，以接口字段为准，未传时使用 `.env` 默认值。

正式图片媒资接口的鉴权信息建议通过请求体传入，而不是写死在 `.env`：

```json
"generation_config": {
  "enable_images": true,
  "image_api_key": "图片接口 token，不需要带 Bearer",
  "image_tenant_code": "租户编码"
}
```

规则：

- `enable_images=true` 时，`image_api_key` 和 `image_tenant_code` 必传
- `enable_images=false` 或未传时，可以不传 `image_api_key` 和 `image_tenant_code`
- `.env` 中的 `IMAGE_MCP_API_KEY`、`IMAGE_MCP_TENANT_CODE` 仅作为兜底配置，不建议提交真实值

启用后，报告正文生成会先根据大纲和参考资料判断适合插图的章节，再调用图片媒资接口搜索候选图片，并把真实图片 URL 提供给模型。最终正文通过 Markdown 图片语法插入图片：

```md
![图1：图片标题](https://example.com/image.jpg)
```

## 4. 本地启动

创建虚拟环境并安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

启动服务：

```bash
python main.py
```

等价命令：

```bash
uvicorn main:app --host 0.0.0.0 --port 8115
```

健康检查：

```bash
curl http://127.0.0.1:8115/health
```

正常返回：

```json
{"status":"ok"}
```

常用地址：

- Swagger：`http://127.0.0.1:8115/docs`
- 健康检查：`http://127.0.0.1:8115/health`
- 聊天接口：`http://127.0.0.1:8115/agent/v1/chat/completions`

## 5. Docker 部署

当前项目提供：

- `Dockerfile`：构建 agent 服务镜像，默认端口 `8115`
- `docker-compose.yml`：只启动 agent 服务，不重复启动 PostgreSQL
- `.dockerignore`：排除 `.env`、虚拟环境、日志、测试等内容
- `scripts/deploy.sh`：参考 `repo-agent-qa` 的部署脚本，封装网络检查、DSN 检查、构建和启动

### 5.1 前置条件

确认 Docker 和 Docker Compose 插件可用：

```bash
docker --version
docker compose version
```

`rag-platform` 不启动 PostgreSQL，只加入外部 `pageindex` 网络复用已有数据库。先确认外部 PostgreSQL 和网络已就绪，再在大项目根目录检查入库服务：

```bash
docker network inspect pageindex >/dev/null
cd /path/to/rag-platform
docker compose ps repo-doc-ingestion
```

如网络不存在，可在确认 PostgreSQL 也会加入该网络后手动创建：

```bash
docker network create pageindex
```

### 5.2 准备 `.env`

```bash
cd /path/to/rag-platform/src/rag-report-agent
test -f ../../env/report-agent.env
vim ../../env/report-agent.env
```

Docker 部署时，重点确认：

```env
APP_PORT=8115
DOCKER_NETWORK=pageindex
PG_DSN=postgresql://postgres:change_me@pageindex-postgres:5432/pageindex
ARK_API_KEY=你的豆包APIKey
```

`scripts/deploy.sh` 会拒绝容器环境下的 `PG_DSN=...@127.0.0.1...` 或 `PG_DSN=...@localhost...`，避免服务启动后连错数据库。

### 5.3 构建并启动

推荐方式：

```bash
bash scripts/deploy.sh
```

脚本会执行：

- 检查 `.env`
- 检查 Docker / Docker Compose
- 检查 `PG_DSN` 是否适合容器网络
- 创建 `DOCKER_NETWORK`
- 执行 `docker compose up -d --build`

手动等价命令：

```bash
docker network create pageindex
docker compose up -d --build
```

查看状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f app
```

健康检查：

```bash
curl http://127.0.0.1:8115/health
```

如果要换宿主机端口，例如改成 `8110`：

```env
APP_PORT=8110
```

然后重新启动：

```bash
docker compose up -d
```

### 5.4 停止与更新

停止服务：

```bash
docker compose down
```

拉取新代码后更新：

```bash
git pull
docker compose build --no-cache
docker compose up -d
```

日志使用 Docker volume 持久化。需要彻底清理运行期 volume 时再执行：

```bash
docker compose down -v
```

## 6. API 示例

`user_id` 为必传字段。服务按 `user_id + kb_id` 隔离文档检索范围，未传 `user_id` 时接口会返回参数校验错误。

### 6.1 知识问答

```bash
curl -N -X POST http://127.0.0.1:8115/agent/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "sess-qa-001",
    "user_id": "user-demo-001",
    "kb_id": "kb-demo",
    "query": "马飞是谁",
    "stream": true,
    "doc_ids": [
      "c4a32e97-2733-5dd9-8f1e-49deb2175251",
      "8b739733-dca5-5696-8c39-f997dfc5cd8b"
    ],
    "retrieval_config": {
      "top_k": 5,
      "search_mode": "hybrid"
    },
    "generation_config": {
      "temperature": 0.2,
      "max_tokens": 4096
    }
  }'
```

### 6.2 确认大纲后生成报告

```bash
curl -N -X POST http://127.0.0.1:8115/agent/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "sess-report-001",
    "user_id": "user-demo-001",
    "kb_id": "kb-demo",
    "query": "OK，开始写吧",
    "stream": true,
    "doc_ids": ["c4a32e97-2733-5dd9-8f1e-49deb2175251"],
    "generation_config": {
      "temperature": 0.2,
      "max_tokens": 8192,
      "enable_images": true,
      "image_api_key": "图片接口 token，不需要带 Bearer",
      "image_tenant_code": "租户编码"
    },
    "report_context": {
      "outline_confirmed": true,
      "current_outline": {
        "title": "《马飞研究员个人介绍报告》",
        "sections": [
          {"index": 1, "title": "一、报告摘要", "subsections": []},
          {
            "index": 2,
            "title": "二、个人基本概况",
            "subsections": [
              {"index": "2.1", "title": "基础身份信息"},
              {"index": "2.2", "title": "教育背景"},
              {"index": "2.3", "title": "工作经历"}
            ]
          }
        ]
      }
    }
  }'
```

说明：如果前端传的是 `{"current_outline": {"outline": {...}}}`，服务也会自动兼容并展开。

## 7. SSE 事件

`POST /agent/v1/chat/completions` 是 SSE 流式接口。请求进入流式响应后，即使业务错误来自远程检索服务，HTTP transport 通常仍是 `200`；前端必须读取 `event: error` 里的 `code`、`type`、`message` 和 `fatal`。

远程检索相关错误映射：

- 检索服务限流 `429` -> SSE `error`，`code=429`，`type=rag_retrieval_rate_limited`
- 检索服务不可达、超时或其它 HTTP 错误 -> SSE `error`，`code=503`，`type=rag_retrieval_unavailable`
- 开发者排查时看 agent 服务日志；日志会包含上游状态码、错误 code 和 `request_id`，但不会输出 API Key

常见事件：

- `stream_start`：请求开始
- `step`：当前流程阶段
- `thinking_delta`：可读思考过程
- `intent`：识别到的一个或多个意图
- `text_delta`：左侧普通对话内容
- `outline_delta`：大纲生成内容
- `outline_complete`：结构化大纲
- `report_start`：报告开始
- `report_text_delta`：右侧报告正文内容
- `references`：引用文档和 chunk
- `report_end`：报告结束
- `stream_end`：请求结束
- `error`：异常信息

## 8. 测试

自动化测试不依赖真实豆包 API Key，默认使用 mock provider：

```bash
source .venv/bin/activate
python -m pytest tests -q
```

真实 PostgreSQL 检索相关测试会读取数据库配置。若本机数据库不可用，可先只跑 API 和图测试：

```bash
python -m pytest tests/test_api_sse.py tests/test_graph_qa.py tests/test_graph_report.py -q
```

## 9. 故障排查

### 9.1 remote RAG 检索失败

先确认 agent 是否启用了 remote 后端：

```env
RAG_RETRIEVAL_BACKEND=remote
RAG_RETRIEVAL_SERVICE_URL=http://127.0.0.1:8120
```

再检查检索服务状态：

```bash
curl http://127.0.0.1:8120/healthz
curl http://127.0.0.1:8120/readyz
```

如果前端收到 SSE `error`：

- `code=429,type=rag_retrieval_rate_limited`：检索服务限流，稍后重试或调整检索服务限流配置
- `code=503,type=rag_retrieval_unavailable`：检查 `RAG_RETRIEVAL_SERVICE_URL`、API Key、网络连通性和检索服务日志

### 9.2 Docker 中连不上 PostgreSQL

优先检查 `.env`：

```env
PG_DSN=postgresql://postgres:change_me@pageindex-postgres:5432/pageindex
```

再检查网络：

```bash
docker network inspect pageindex
docker compose logs -f app
```

如果数据库不在 Docker 网络中，而是在宿主机运行，把 DSN host 改成 `host.docker.internal`。

### 9.3 报告生成被截断

提高 `generation_config.max_tokens`，完整报告建议 `4096` 到 `8192`：

```json
"generation_config": {
  "temperature": 0.2,
  "max_tokens": 8192
}
```

### 9.4 没有思考输出

确认 `.env`：

```env
DOUBAO_THINKING_TYPE=enabled
DOUBAO_REASONING_EFFORT=medium
```

### 9.5 容器启动后没有读到最新代码

重新构建镜像：

```bash
docker compose build --no-cache
docker compose up -d
```
