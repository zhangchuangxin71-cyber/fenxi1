# 报告生成服务环境变量说明

本文对应当前 `.env.example`。推荐值按“统一 Compose 内调用独立检索服务”的生产形态给出。报告生成需要高质量长文本，默认允许 thinking；服务应立即向 SSE 前端发送安全的“正在思考”状态，但不能返回真实 reasoning 内容。

## 服务

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `APP_NAME` | `rag-agent` | 服务日志标识。 |
| `APP_ENV` | `prod` | 运行环境。 |
| `HOST` | `0.0.0.0` | 容器内部监听地址。宿主暴露范围由 Compose 或网关控制。 |
| `PORT` | `8115` | Uvicorn 容器监听端口。 |
| `APP_PORT` | `8115` | Compose 宿主映射和部署脚本使用的端口。 |
| `LOG_LEVEL` | `INFO` | 生产日志等级。 |
| `DOCKER_NETWORK` | `pageindex` | 单仓库 Compose 的共享网络；统一 Compose 可使用默认网络。 |

## PostgreSQL 与检索

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `PG_DSN` | `postgresql://<user>:<secret>@postgres:5432/pageindex` | PageIndex 数据库连接串。即使 chunk 检索改为 remote，现有报告流程仍可能读取文档元数据；使用最小权限账号。 |
| `RAG_TOP_K` | `5` | 默认检索结果数。 |
| `RAG_SEARCH_MODE` | `hybrid` | 默认检索模式。 |
| `RAG_MAX_CANDIDATES` | `500` | 兼容本地 PostgreSQL retriever 的候选上限。 |
| `RAG_RETRIEVAL_BACKEND` | `remote` | 生产统一调用独立 `rag-retrieval-service`；`postgres` 只用于兼容/本地调试。 |
| `RAG_RETRIEVAL_SERVICE_URL` | `http://rag-retrieval-service:8120` | 检索服务容器 DNS。 |
| `RAG_RETRIEVAL_TIMEOUT_SECONDS` | `180` | 独立检索服务 HTTP timeout；为较慢模型和多文档检索预留时间。 |
| `DOC_ROUTER_MAX_DOCS` | `50` | 报告 agent 自身文档路由上限；不替代检索服务 100 个 doc ID 的接口上限。 |

remote 请求会携带接口中的 `session_id`，从而让独立检索服务验证临时文档归属；永久
文档不依赖该字段。报告服务会在代码中按 query 和文档数量动态计算
`max_return_tokens=4096–32768`，无需增加额外环境变量。

## Ark / LLM

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `ARK_API_KEY` | Secret 注入 | 报告规划和生成使用的 Ark key。 |
| `ARK_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | Ark OpenAI-compatible 地址。 |
| `MODEL_MINI` | `doubao-seed-2-0-lite-260215` | 轻量任务模型。 |
| `MODEL_FAST` | `doubao-seed-2-0-lite-260215` | 快速任务模型。 |
| `MODEL_SMART` | `doubao-seed-2-0-lite-260215` | 报告正文等高质量任务模型。上线前核对实际 endpoint。 |
| `DOUBAO_THINKING_TYPE` | `enabled` | 报告质量优先，允许 thinking；前端只接收状态事件。若更看重延迟，可压测 `auto` 或 `disabled`。 |
| `DOUBAO_REASONING_EFFORT` | `medium` | thinking 强度，可选 minimal/low/medium/high。 |
| `LLM_TIMEOUT_SECONDS` | `60` | 单次模型请求 timeout。长报告仍受更上层任务 deadline 约束。 |
| `LLM_MAX_RETRIES` | `2` | 可重试上游错误次数。上线后监控重试对 P95 的放大，必要时降为 1。 |

## 报告配图

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `REPORT_IMAGE_ENABLED` | `false` | 全局默认关闭；接口可按请求开启。 |
| `REPORT_IMAGE_MAX_SECTIONS` | `3` | 单报告最多配图章节数。 |
| `REPORT_IMAGE_TOP_K` | `3` | 每章节图片候选数。 |
| `IMAGE_MCP_URL` | 实际内网/HTTPS 地址 | 图片媒资服务地址，启用配图时必填。 |
| `IMAGE_MCP_API_TYPE` | `media_resources`（正式媒资接口） | 接口适配类型；`generic` 仅用于通用兼容接口。 |
| `IMAGE_MCP_API_KEY` | 留空或 Secret | 服务级兜底 key；优先由受信任的服务端请求配置传入。 |
| `IMAGE_MCP_TENANT_CODE` | 留空或租户标识 | 服务级兜底 tenant code。 |
| `IMAGE_MCP_TIMEOUT_SECONDS` | `20` | 图片检索 timeout。 |

## OSS 日志

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `OSS_ENABLED` | `false`，完成凭据和归档验证后再开启 | OSS 日志开关。 |
| `OSS_ENDPOINT` | 实际 endpoint | OSS 地址。 |
| `OSS_ACCESS_KEY` | Secret 注入 | 最小权限访问 ID。 |
| `OSS_SECRET_KEY` | Secret 注入 | OSS 密钥。 |
| `OSS_BUCKET` | 实际 bucket | 日志 bucket。 |
| `OSS_LOG_PREFIX` | `rag-agent/logs/` | OSS 日志前缀。 |

## 上线检查

1. `RAG_RETRIEVAL_BACKEND=remote`，URL 使用容器 DNS；生产默认只调用独立检索服务，内置 PostgreSQL 检索仅保留兼容用途。
2. 8120 只允许可信内部调用方访问；兼容 Bearer 不构成鉴权。
3. 网关/SSE 代理关闭缓冲，thinking 只暴露状态，不暴露 reasoning。
4. 数据库与 OSS 使用最小权限凭据；所有 secret 不进入 Git。
5. 模型 timeout、重试和 thinking 设置经过长报告压测，重点观察首状态、首正文和总 P95。
