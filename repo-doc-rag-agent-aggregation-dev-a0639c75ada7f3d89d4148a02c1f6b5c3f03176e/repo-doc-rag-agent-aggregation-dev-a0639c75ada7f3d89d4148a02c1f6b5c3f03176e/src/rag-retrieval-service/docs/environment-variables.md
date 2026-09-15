# 检索服务环境变量

本文对应新 `rag-retrieval-service/.env.example`。服务使用单 Uvicorn worker；全局 LLM
队列、熔断器、数据库 executor 和用户 RPM limiter 均为进程内对象。生产默认端口为
8120，本地联调可通过启动参数临时使用其他端口。

## 服务与数据库

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `APP_NAME` | `rag-retrieval-service` | 服务名。 |
| `APP_ENV` | `production` | 运行环境标识。 |
| `APP_HOST` | `0.0.0.0` | 监听地址。 |
| `APP_PORT` | `8120` | 默认监听端口。 |
| `APP_MAX_CONCURRENCY` | `30` | `/retrieve` 同时执行的请求上限。 |
| `APP_MAX_QUEUED_REQUESTS` | `90` | `/retrieve` 公平等待队列上限；队列满时返回 503。 |
| `APP_ADMISSION_WAIT_TIMEOUT_SECONDS` | `120` | 检索请求等待执行槽的最长时间。 |
| `LOG_LEVEL` | `INFO` | 日志等级。 |
| `POSTGRES_DSN` | Secret | 入库服务共用的 PostgreSQL，只授予必要读权限。 |
| `DB_POOL_MIN` | `1` | 最小连接数。 |
| `DB_POOL_MAX` | `30` | 最大连接数。 |
| `DB_CONNECT_TIMEOUT_SECONDS` | `5` | 建连 timeout。 |
| `DB_POOL_ACQUIRE_TIMEOUT_SECONDS` | `10` | 获取连接 timeout。 |
| `DB_QUERY_TIMEOUT_SECONDS` | `60` | 单次只读 SQL timeout。 |
| `DB_POOL_IDLE_TTL_SECONDS` | `120` | 超出最小池规模的连接允许空闲多久。 |
| `DB_POOL_REAPER_INTERVAL_SECONDS` | `30` | 空闲连接回收检查周期。 |

## 检索与返回预算

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `RAG_API_PREFIX` | `/rag/v1` | API 前缀。 |
| `RAG_DEFAULT_TOP_K` | `5` | 未传 `top_k` 时的全局软目标。 |
| `RAG_MAX_TOP_K` | `100` | 接口允许的最大软目标。 |
| `RAG_MAX_DOC_IDS` | `100` | 单请求文档 ID 总上限。 |
| `RAG_DEFAULT_RETURN_TOKENS` | `8192` | 未传预算时的返回 token 预算。 |
| `RAG_MIN_RETURN_TOKENS` | `128` | 检索接口内部允许的最小返回预算。 |
| `RAG_MAX_RETURN_TOKENS` | `180000` | 请求 `max_return_tokens` 的服务端绝对上限。当前默认值针对 doubao-2.0-lite 的 224K 最大输入预留提示词、历史消息和封装空间；更换模型后可调整。 |
| `RAG_DEFAULT_INCLUDE_DOCUMENT_META` | `true` | 默认是否附带文档元信息。 |
| `RAG_DOCUMENT_PREFILTER_THRESHOLD` | `0.05` | 文档名 + description keyword 硬预筛阈值；`0` 表示全部通过。 |
| `RAG_DOCUMENT_PREFILTER_MAX_CANDIDATES` | `32` | 单个 group 允许进入 LLM 文档路由的最大预筛候选数。 |
| `RAG_TREE_MAX_STEPS` | `4` | 单文档目录树最大导航轮次。 |
| `RAG_TREE_SCAN_MAX_TOKENS` | `4096` | 单次目录树工具结果预算。 |
| `RAG_PAGE_ACCEPT_SCORE` | `20` | 规则 page accept 阈值。 |
| `RAG_PAGE_POSSIBLE_SCORE` | `1` | 规则 page possible 阈值。 |
| `RAG_MINIMUM_PAGE_TOKENS` | `64` | 预算检查时可装入 page 的最小估算。 |
| `RAG_QUERY_CLASSIFICATION_STRATEGY` | `robust` | Query 分类策略：`robust` 使用 scope-first 多阶段分类，`fast` 使用原单次低延迟分类。仅进程启动时读取。 |

`top_k` 不是 direct/broad 的硬截断；`max_return_tokens` 才是最终 chunk 载荷硬预算。
发生丢弃或连续原文截断时响应必须返回 warning 和 coverage。

`POST /rag/v1/documents/route` 的 `keyword_prefilter` 默认为 `false`。只有请求显式开启它时，
独立路由接口才使用上述 `RAG_DOCUMENT_PREFILTER_THRESHOLD` 和
`RAG_DOCUMENT_PREFILTER_MAX_CANDIDATES`；主 `/retrieve` 图内的预筛行为不受这个请求字段影响。

## LLM Gateway

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `ARK_API_KEY` | Secret | OpenAI-compatible 模型平台 API Key。 |
| `ARK_BASE_URL` | Ark API v3 | OpenAI-compatible base URL，可切换已探测兼容的平台。 |
| `RAG_LLM_MODEL` | 实际 endpoint | 分类、路由和 tree search 使用的模型。 |
| `RAG_LLM_CONTEXT_WINDOW` | 按模型设置 | 总上下文窗口。 |
| `RAG_LLM_MAX_OUTPUT_TOKENS` | `2048` | 单次结构化输出上限，不应误设成上下文窗口大小。 |
| `RAG_LLM_SAFETY_MARGIN_TOKENS` | `1024` | LPT/上下文预检安全余量。 |
| `RAG_LLM_TIMEOUT_SECONDS` | `90` | 单次 provider timeout。 |
| `RAG_LLM_MAX_RETRIES` | `0` | gateway 重试次数；首版避免放大尾延迟。 |
| `RAG_LLM_MAX_CONCURRENCY` | `16` | 进程全局 provider 并发数。 |
| `RAG_LLM_MAX_QUEUED_CALLS` | `1024` | 全局等待队列上限。 |
| `RAG_LLM_PER_REQUEST_MAX_IN_FLIGHT` | `6` | 一个外部请求占用的最大并发调用数。 |
| `RAG_LLM_CIRCUIT_BREAKER_ENABLED` | `true` | 是否启用轻量熔断。 |
| `RAG_LLM_CIRCUIT_FAILURE_THRESHOLD` | `3` | 连续失败多少次后熔断。 |
| `RAG_LLM_CIRCUIT_RECOVERY_SECONDS` | `30` | 熔断恢复等待时间。 |

所有模型请求显式关闭 thinking。LPT 分组数量不会被并发上限截断；多出的调用进入队列。

## Deadline、Debug 与限流

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `REQUEST_SOFT_DEADLINE_SECONDS` | `600` | 到达后停止尚未开始的可选 possible/broad 工作。 |
| `REQUEST_HARD_DEADLINE_SECONDS` | `900` | 整体硬 deadline。 |
| `REQUEST_FINALIZATION_RESERVE_SECONDS` | `30` | 为 merge 和序列化保留时间。 |
| `RAG_DEBUG_ENABLED` | `false` | 服务端 debug 总开关。关闭时请求不能强制开启 trace。 |
| `RAG_RATE_LIMIT_ENABLED` | `true` | 用户级外部检索请求 RPM 开关。 |
| `RAG_RATE_LIMIT_PER_MINUTE` | `1200` | 每个 `user_id` 的防滥用分钟速率。 |
| `RAG_RATE_LIMIT_BURST` | `300` | token bucket burst；正常交互流量不会触发。 |
| `RAG_RATE_LIMIT_BUCKET_TTL_SECONDS` | `900` | 空闲用户 bucket 清理时间。 |

debug 只有 `RAG_DEBUG_ENABLED=true` 且请求 `options.include_debug=true` 时启用。本项目不
使用 LangSmith；关闭时不创建 TraceCollector，不保存 LLM 原始输出、工具历史或节点摘要。

## 安全边界

新服务没有 `RAG_AUTH_ENABLED` 或 `RAG_SERVICE_API_KEYS` 运行配置。`user_id`、`kb_id`
和 `session_id` 是可信上游提供的兼容/限流字段，不是身份认证。8120 必须位于可信内网或
认证网关之后；调用方负责 doc_id 权限隔离，SQL 只按 doc_id 查询。

## 上线检查

1. `.env` 权限为 `0600`，真实 DSN/API Key 不进入 Git、日志或 debug。
2. `/healthz` 返回进程状态，`/readyz` 能实际访问生产 PostgreSQL。
3. `RAG_LLM_CONTEXT_WINDOW`、输出上限和安全余量满足
   `context > output + safety_margin`。
4. `/retrieve` 活跃数、等待队列、LLM 并发和数据库池按真实配额压测，不用多 worker 绕过限制。
5. 生产保持 `RAG_DEBUG_ENABLED=false`；临时排障开启后及时关闭。
