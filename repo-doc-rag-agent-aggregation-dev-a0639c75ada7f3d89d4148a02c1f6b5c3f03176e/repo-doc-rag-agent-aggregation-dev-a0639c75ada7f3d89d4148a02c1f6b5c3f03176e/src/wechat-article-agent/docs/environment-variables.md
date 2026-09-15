# 微信公众号文章 Agent 环境变量

本文对应两份配置模板：

- `src/wechat-article-agent/.env.example`：本地开发容器初值，adapter、Agent Server 和开发面板分别使用
  `8240/8242/8245`，检索服务使用 `127.0.0.1:8220`。
- `env/wechat-article.env.example`：统一生产部署初值，公开 adapter 使用 `8140`，私有 Agent Server
  通过 Compose DNS `wechat-article-runtime:8141` 访问，检索服务使用
  `rag-retrieval-service:8120`。
- `env/wechat-article-db.env.example`：只供一次性数据库初始化容器使用，保存具备建 role/database 权限
  的管理员 DSN；不会注入长期运行的 adapter 或 Agent Server。

项目使用单个 adapter worker。运行准入、LLM/Seedream 并发池、熔断器和限流器都是进程内对象；
不要通过增加 Uvicorn worker 绕过这些限制。生产 Compose 启动一个公开 adapter 容器和一个私有
LangGraph Agent Server 容器，只有 adapter 的 8140 端口映射到宿主机。

## 服务与 Debug

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `APP_ENV` | `production` | 运行环境标识，可选 `development/test/production`。 |
| `APP_HOST` | `0.0.0.0` | adapter 监听地址。生产由 Compose 覆盖为 `0.0.0.0`。 |
| `APP_PORT` | `8140` | 公开 Responses-like API 端口；本地模板为 `8240`。 |
| `DEBUG_ENABLED` | `false` | 服务端 debug 总开关。关闭时不收集开发面板所需的原始 LLM/工具 trace。 |
| `DEBUG_TRACE_MAX_BYTES` | `8388608` | 单次响应累计 debug trace 的字节上限。 |
| `DEBUG_TRACE_VALUE_MAX_CHARS` | `1000000` | 单个 trace 字段保留的最大字符数。 |

生产必须保持 `DEBUG_ENABLED=false`。只有本地开发或受控排障时才开启；trace 可能包含系统提示词、
模型原始输入输出、工具参数和文档内容，不能返回给普通产品调用方。`/docs` 和 `/openapi.json` 不依赖
该开关，关闭 trace 后仍可供 Apifox 读取接口契约。非 debug 产品 SSE 会返回素材来源、必要时的素材冲突、
任务书、大纲、未排版文章、图片和最终 HTML，不返回 `session_memory`、`material_library` 或终态 `debug`。

## 工作流准入与外部请求限流

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `APP_MAX_ACTIVE_RUNS` | `30` | adapter 同时允许执行或恢复的工作流上限。 |
| `APP_MAX_QUEUED_RUNS` | `90` | 工作流公平等待队列上限；队列满时返回 503。 |
| `APP_ADMISSION_WAIT_TIMEOUT_SECONDS` | `120` | 等待工作流执行槽的最长时间。 |
| `REQUEST_RATE_LIMIT_PER_MINUTE` | `1200` | 按调用来源与用户/会话组合计算的防滥用分钟速率。 |
| `REQUEST_RATE_LIMIT_BURST` | `300` | 请求 token bucket 的 burst。 |
| `REQUEST_RATE_LIMIT_BUCKET_TTL_SECONDS` | `900` | 空闲限流 bucket 的回收时间。 |

限流目标是阻止脚本异常流量，不是限制正常用户交互。上述值是单 adapter 实例初值，多实例部署前需要
把进程内限流和 session 并发控制改成共享实现。

## PostgreSQL、版本产物与 TTL

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `DATABASE_URL` | Secret | 微信文章独立 PostgreSQL database；供 artifact repository 和 LangGraph checkpointer 共用。 |
| `DATABASE_ADMIN_URL` | Secret | 位于独立 `wechat-article-db.env`；仅部署 bootstrap 使用，连接已存在的维护库并具备 `CREATE ROLE/CREATE DATABASE` 权限。 |
| `DB_POOL_MIN` | `1` | adapter/图进程各自连接池的最小连接数。 |
| `DB_POOL_MAX` | `30` | 单进程连接池最大连接数。 |
| `DB_CONNECT_TIMEOUT_SECONDS` | `5` | PostgreSQL 建连 timeout。 |
| `DB_POOL_ACQUIRE_TIMEOUT_SECONDS` | `10` | 从连接池获取连接的 timeout。 |
| `DB_QUERY_TIMEOUT_SECONDS` | `60` | 单次业务 SQL timeout。 |
| `DB_MAX_RETRIES` | `2` | 瞬时数据库错误的额外重试次数。 |
| `ARTIFACT_TTL_HOURS` | `72` | checkpoint 与全部 artifact revision 的滑动 TTL。 |
| `TTL_CLEANUP_INTERVAL_SECONDS` | `300` | 过期 session 扫描周期。 |
| `TTL_CLEANUP_BATCH_SIZE` | `50` | 每轮最多清理的过期 session 数。 |

该数据库只保存 LangGraph checkpointer 内部表和一张 `article_artifacts` 业务表。完整对话历史由调用方
管理，不写入这里。`article_artifacts` 按 revision 保存素材库、任务书、大纲、Markdown 正文、图片
URL/标注/插入位置和最终 HTML。

生产 DSN 不能使用 `127.0.0.1` 或 `localhost`，因为它们指向容器自身。使用数据库的内网 DNS/IP，
并为微信文章使用独立 database 和最小权限账号。adapter 与 runtime 都会连接该 database，因此连接数
预算不能只按一个进程计算。

统一部署会先运行一次性 `wechat-article-db-init` 容器。该容器幂等创建 `DATABASE_URL` 中声明的应用
role 和 database，执行 `app/persistence/migrations/*.sql`，并调用 LangGraph PostgreSQL checkpointer
的 `setup()` 创建内部表。既有同名 database 的 owner 与应用账号不一致时初始化会失败，不会自动接管。
管理员 DSN 不得写入 `wechat-article.env`，避免长期服务获得建库权限。

## Agent Server 与取消

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `AGENT_SERVER_URL` | `http://wechat-article-runtime:8141` | 私有 LangGraph Agent Server 地址；本地为 `http://127.0.0.1:8242`。 |
| `AGENT_SERVER_ASSISTANT_ID` | `wechat_article` | `langgraph.json` 中对外运行的图 ID。 |
| `WECHAT_AGENT_THREAD_NAMESPACE` | 固定 UUID | 用 `session_id` 派生确定性 thread ID 的 UUIDv5 namespace。已上线后不得随意更换。 |
| `CANCEL_CONFIRM_TIMEOUT_SECONDS` | `10` | cancel 等待 Agent Server 确认终止的最长时间；超时返回 202 cancelling。 |
| `CANCEL_RECONCILE_INTERVAL_SECONDS` | `15` | 后台重新协调 cancelling 记录的周期。 |
| `CANCEL_RECONCILE_BATCH_SIZE` | `20` | 每轮最多处理的 cancelling artifact 数。 |

Agent Server 是内部运行时，不应映射宿主机端口或直接暴露给产品调用方。关闭产品 SSE 只停止接收，
不会触发 cancel；正式取消必须调用 adapter 的 `POST /v1/responses/{response_id}/cancel`。

`WECHAT_AGENT_THREAD_NAMESPACE` 是持久化标识的一部分。修改它会让相同 `session_id` 派生出不同 thread，
导致已有 checkpoint 无法按原会话恢复。

## Ark Responses LLM Gateway

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `ARK_API_KEY` | Secret | 火山方舟 API Key，语言模型和 Seedream 共用。 |
| `ARK_RESPONSES_URL` | `https://ark.cn-beijing.volces.com/api/v3/responses` | Ark Responses endpoint。 |
| `ARK_MODEL_FAST` | `doubao-seed-2-1-pro-260628` | intent、orchestrator、preflight、session memory 等轻任务模型 endpoint。 |
| `ARK_MODEL_MAIN` | `doubao-seed-2-1-pro-260628` | 文档研读和 artifact 生成模型 endpoint。 |
| `ARK_CONTEXT_WINDOW` | `262144` | 模型总上下文窗口，用于调用前预算检查。 |
| `ARK_MAX_CONCURRENCY` | `30` | 进程全局 LLM provider 并发上限。 |
| `ARK_MAX_QUEUED_CALLS` | `180` | LLM 调用等待队列上限。 |
| `ARK_PER_RUN_MAX_IN_FLIGHT` | `6` | 单个 workflow 最多并行占用的 LLM 调用数。 |
| `ARK_QUEUE_WAIT_TIMEOUT_SECONDS` | `120` | 等待 LLM 执行槽的最长时间。 |
| `ARK_CONNECT_TIMEOUT_SECONDS` | `10` | provider 建连 timeout。 |
| `ARK_FIRST_EVENT_TIMEOUT_SECONDS` | `60` | 流式调用等待首事件的 timeout。 |
| `ARK_STREAM_IDLE_TIMEOUT_SECONDS` | `90` | 流已经开始后相邻事件最大空闲时间。 |
| `ARK_CALL_MAX_SECONDS` | `600` | 单次模型调用的整体硬上限。 |
| `ARK_MAX_RETRIES` | `2` | 首次请求之外的最大重试次数；已经输出有意义内容后不重试。 |
| `ARK_RETRY_BASE_SECONDS` | `1` | 指数退避基数，实际会加入 jitter。 |
| `ARK_STRUCTURED_OUTPUT_MAX_REPAIRS` | `1` | 任务书、大纲、文章的 strict 输出未通过本地校验时，携带无效输出进行修复的最大次数。修复调用强制关闭推理。 |
| `ARK_STRUCTURED_OUTPUT_MAX_RETRIES` | `2` | 流程判断、联网搜索等其他 strict 输出未通过本地校验时，丢弃无效结果并从原始输入完整重跑的最大次数。重跑强制关闭推理。 |
| `ARK_CIRCUIT_BREAKER_ENABLED` | `true` | 是否启用轻量 provider 熔断。 |
| `ARK_CIRCUIT_FAILURE_THRESHOLD` | `3` | 连续失败多少次后打开熔断器。 |
| `ARK_CIRCUIT_RECOVERY_SECONDS` | `30` | 熔断后进入半开探测前的等待时间。 |
| `ARK_RATE_LIMIT_PER_MINUTE` | `3000` | 进程级 Ark 防滥用 RPM。 |
| `ARK_RATE_LIMIT_BURST` | `600` | Ark token bucket burst。 |
| `ARK_RATE_LIMIT_BUCKET_TTL_SECONDS` | `900` | Ark 限流 bucket 回收时间。 |

`ARK_MAX_CONCURRENCY=30` 是项目要求的单实例业务容量初值，不代表 Ark 账号一定具有相同 RPM/TPM 和
并发配额。真实上线前必须用目标模型 endpoint 压测；发生 429 时优先按实际配额降低并发，不要通过
增加重试放大流量。

## Ark 上下文能力与思考模式

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `ARK_CONTEXT_MANAGEMENT_ENABLED` | `false` | 是否向 Ark 请求发送 `context_management`。 |
| `ARK_CACHING_ENABLED` | `false` | 是否向 Ark 请求发送 `caching`。 |
| `ARK_THINKING_TASK_SPEC` | `true` | 生成任务书时是否开启模型思考。 |
| `ARK_THINKING_OUTLINE` | `true` | 生成 Markdown 大纲时是否开启模型思考。 |
| `ARK_THINKING_ARTICLE` | `true` | 生成未排版 Markdown 正文时是否开启模型思考。 |

第一版只有任务书、大纲和正文三个 artifact 生成调用允许按配置开启 thinking；intent、preflight、
session memory、文档研读判断、图片规划等其他调用始终由网关关闭 thinking。Ark 联网背景搜索和网络
素材搜索固定发送 `thinking.type=disabled`；任何 strict schema 校验失败后的修复调用也固定关闭 thinking，
即使原始任务书、大纲或正文调用开启了 thinking，也不会把推理配置继承到修复调用。

`context_management` 和 `caching` 默认关闭，只有目标 Ark endpoint 已完成真实 contract test 后才开启。
若 provider 明确返回不支持这些字段，网关会关闭两项能力并按不带扩展字段的 payload 重试；这不等于
所有模型或账号都已经支持该能力。

## 素材调研、联网搜索与检索服务

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `WEB_SEARCH_ENABLED` | `false` | 是否允许素材不足时调用 Ark 内置 `web_search`；关闭时不产生任何联网搜索请求。 |
| `RETRIEVAL_BASE_URL` | `http://rag-retrieval-service:8120` | 检索服务 meta、route、raw、raw status 和 retrieve API 地址。 |
| `RETRIEVAL_CONNECT_TIMEOUT_SECONDS` | `10` | 检索服务建连 timeout。 |
| `RETRIEVAL_TIMEOUT_SECONDS` | `180` | 普通检索请求 timeout。 |
| `RETRIEVAL_MAX_CONCURRENCY` | `30` | 微信文章进程内同时调用检索服务的上限。 |
| `RETRIEVAL_MAX_RETRIES` | `2` | 瞬时网络错误和可重试响应的额外重试次数。 |
| `RETRIEVAL_RETRY_BASE_SECONDS` | `1` | 检索重试指数退避基数。 |
| `RETRIEVAL_CIRCUIT_BREAKER_ENABLED` | `true` | 是否启用检索服务熔断。 |
| `RETRIEVAL_CIRCUIT_FAILURE_THRESHOLD` | `3` | 连续失败多少次后熔断。 |
| `RETRIEVAL_CIRCUIT_RECOVERY_SECONDS` | `30` | 检索熔断恢复等待时间。 |
| `RAW_REPAIR_MAX_WAIT_SECONDS` | `600` | `/raw` 返回 202 后轮询 MinerU raw 修复的最长时间。 |
| `SHORT_DOCUMENT_MAX_CHARS` | `80000` | 优先使用完整 raw 的短文档字符阈值。 |
| `MATERIAL_QUERY_GROUPS_PER_DOCUMENT` | `3` | 每篇文档生成的素材检索 query group 上限。 |
| `ARTICLE_MAX_RETRIEVAL_ROUNDS` | `3` | 正文生成前允许的补充检索最大轮数。 |
| `CLARIFICATION_MAX_ROUNDS` | `3` | 自定义素材搜集方向仍含糊时允许再次展示方向卡的最大轮数；耗尽后使用最后输入强制开始调研。 |

`WEB_SEARCH_ENABLED` 是服务端全局开关，不接受请求体越权开启。启用后，系统仍会先判断用户文档是否
足够；只有需要补充素材时才调用 Ark 内置搜索。网络搜索复用现有 Ark 并发、限流、超时、有限重试和
熔断配置，不新增另一套 API Key 或网络超时变量。本地真实烟测可临时开启，生产模板默认关闭，待业务
方确认联网合规和成本后再显式改为 `true`。

生产通过 Compose DNS 调用检索服务，不使用宿主 IP。检索服务不负责认证，adapter 只能接受可信上游
已经完成权限校验的 `doc_ids/temp_doc_ids`。

## Seedream 图片生成

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `SEEDREAM_RESPONSES_URL` | `https://ark.cn-beijing.volces.com/api/v3/images/generations` | Seedream 图片生成 endpoint。 |
| `SEEDREAM_MODEL` | `doubao-seedream-5-0-260128` | Seedream 5.0 Lite 图片模型；该模型支持联网检索生图。 |
| `SEEDREAM_SIZE` | `2K` | 请求图片尺寸。 |
| `SEEDREAM_MAX_CONCURRENCY` | `8` | 进程全局图片请求并发上限。 |
| `SEEDREAM_CONNECT_TIMEOUT_SECONDS` | `10` | 图片 provider 建连 timeout。 |
| `SEEDREAM_CALL_MAX_SECONDS` | `600` | 单张图片生成硬 timeout。 |
| `SEEDREAM_MAX_RETRIES` | `2` | 可重试图片错误的额外重试次数。 |
| `SEEDREAM_RETRY_BASE_SECONDS` | `1` | 图片重试指数退避基数。 |
| `SEEDREAM_CIRCUIT_BREAKER_ENABLED` | `true` | 是否启用图片 provider 熔断。 |
| `SEEDREAM_CIRCUIT_FAILURE_THRESHOLD` | `3` | 连续失败多少次后熔断。 |
| `SEEDREAM_CIRCUIT_RECOVERY_SECONDS` | `30` | 图片熔断恢复等待时间。 |
| `IMAGE_MAX_COUNT` | `6` | 单篇文章允许生成的图片硬上限；模型在 0 到该值之间规划实际数量。 |

业务表只保存 Seedream 返回的 URL、caption 和结构化插入位置，不下载或上传图片。所有图片失败时允许
降级交付无图 HTML；部分图片失败时使用成功图片继续排版。

当前应使用 `doubao-seedream-5-0-260128`。当代码检测到该模型名时，会在图片生成请求中显式添加
`tools: [{"type":"web_search"}]`，由 Seedream 按图片提示词自主决定是否联网。其他模型不会自动携带
该工具参数，避免向不支持联网生图的 endpoint 发送未知字段。

## HTML 排版

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `HTML_LAYOUT_RENDERER` | `skill_driven` | 排版后端模式：`deterministic` 使用默认主题的确定性代码排版；`llm_decide` 先用严格 schema 的 LLM 选择已注册主题，再由代码排版；`skill_driven` 使用带预算的 ReAct Agent 与迁移后的 GZH/Xiaowan 排版 Skill 资产，失败时按 `llm_decide -> deterministic -> legacy` 降级。 |
| `HTML_LAYOUT_DEFAULT_THEME` | `professional-clean` | 任务书未命中明确主题规则时使用的默认主题；必须是主题 registry 中的 allowlist ID。 |

### `skill_driven` Agent Engine

以下配置只影响 `HTML_LAYOUT_RENDERER=skill_driven` 时的节点内 ReAct Engine。Engine 不建立独立 checkpoint，
不写临时文件，所有中间候选只保存在单次 `render_html` 调用的内存中；取消、超时、预算耗尽或失败后由排版节点
按降级链重新执行下一模式。

| 配置项 | 生产初值 | 含义 |
|---|---:|---|
| `AGENT_ENGINE_MAX_STEPS` | `12` | 单次排版 Agent 最多进行多少轮模型决策。 |
| `AGENT_ENGINE_MAX_TOOL_CALLS` | `16` | 单次排版 Agent 最多执行多少个逻辑工具调用；重复 provider SSE 不重复计数。 |
| `AGENT_ENGINE_MAX_CONTEXT_TOKENS` | `131072` | Engine 请求前预检使用的最大上下文估算值，不能超过 `ARK_CONTEXT_WINDOW`。 |
| `AGENT_ENGINE_CONTEXT_RESERVE_TOKENS` | `8192` | 为模型下一次输出预留的上下文空间；必须小于最大上下文值。 |
| `AGENT_ENGINE_RECENT_FULL_ROUNDS` | `3` | 上下文压缩时保持完整的最近工具调用/结果轮数。 |
| `AGENT_ENGINE_MAX_COMPACTIONS` | `2` | 单次 Engine 最多进行几次确定性上下文压缩。 |
| `AGENT_ENGINE_CALL_TIMEOUT_SECONDS` | `120` | 单轮模型或工具调用的 wall-clock 超时，不能大于总超时。 |
| `AGENT_ENGINE_TOTAL_TIMEOUT_SECONDS` | `300` | 单次排版 Agent 的总 wall-clock 超时。 |
| `AGENT_ENGINE_MAX_SAME_TOOL_FAILURES` | `2` | 同一工具、同一参数和同一错误连续失败达到该值后停止循环并降级。 |
| `AGENT_ENGINE_MAX_TOOL_RESULT_CHARS` | `24000` | 工具结果发送给模型的最大字符数；完整结果只保留在本次内存对象引用中。 |

第一阶段 Engine 内部的模型调用固定关闭思考模式。`agent.activity` 仍会实时发送 Skill/tool 的小型活动摘要；
只有全局 `DEBUG_ENABLED=true` 且请求 `context.debug=true` 时，终态 debug trace 才包含原始模型输入输出、工具参数和结果。

`llm_decide` 模式只把已批准任务书发送给主题选择 LLM，不发送完整文章；可用主题目录由代码工具从
内置 registry 动态生成，并作为系统提示词的一部分。LLM 输出通过动态 `Literal` enum 和 strict
JSON Schema 校验，主题名不在 allowlist 或调用失败时使用 `HTML_LAYOUT_DEFAULT_THEME`。完整降级顺序为
`skill_driven -> llm_decide -> deterministic`；确定性模式直接使用配置的默认主题。配置的默认主题不存在时，
服务在初始化配置时明确失败，不会静默接受任意主题文件。

## 本地开发面板

以下变量只由 `dev.server` 使用，不进入生产镜像的正式业务链路：

| 配置项 | 本地初值 | 含义 |
|---|---:|---|
| `DEV_PANEL_HOST` | `127.0.0.1` | 开发面板监听地址。 |
| `DEV_PANEL_PORT` | `8245` | 开发面板端口。 |
| `DEV_RETRIEVAL_BASE_URL` | `http://127.0.0.1:8220` | 面板读取文档 meta 的检索地址。 |
| `DEV_DATASET_MANIFEST_FILE` | `/workspace/wechat-article-agent/wechat-article-documents.manifest.jsonl` | 本地测试数据 manifest。 |

面板输入框的默认 Agent 地址、`user_id`、`kb_id` 和 `session_id` 不使用环境变量，统一配置在
`dev/config.yaml`。面板启动时由后端校验并读取该文件，再通过 `/api/config` 提供给浏览器；修改后
需要重启开发面板进程并刷新页面。

开发面板要查看原始 LLM/工具 trace 时，还需要本地 adapter 配置 `DEBUG_ENABLED=true`，请求中同时设置
`context.debug=true`。生产不启动 `dev.server`。

## 必填配置

统一部署至少必须显式填写并通过启动预检：

```env
DATABASE_URL=postgresql://wechat_article_agent:<password>@<database-host>:5432/wechat_article_agent
ARK_API_KEY=<ark-api-key>
AGENT_SERVER_URL=http://wechat-article-runtime:8141
RETRIEVAL_BASE_URL=http://rag-retrieval-service:8120
```

另在 `env/wechat-article-db.env` 中填写：

```env
DATABASE_ADMIN_URL=postgresql://<admin-user>:<admin-password>@<database-host>:5432/postgres
```

模型和 endpoint 变量虽然在模板中已有生产初值，也必须在升级模型前逐项核对，不能假设未来 endpoint
保持不变。真实 `.env` 权限应为 `0600`，不得提交到 Git、烟测报告或 debug trace。

## 上线检查

1. 对照根目录 `env/wechat-article.env.example` 和 `env/wechat-article-db.env.example` 创建两份生产 env，
   不从历史遗留 env 复制 Redis 等无关项。
2. `APP_PORT=8140`，`AGENT_SERVER_URL=http://wechat-article-runtime:8141`，
   `RETRIEVAL_BASE_URL=http://rag-retrieval-service:8120`。
3. runtime 只有 Compose `expose: 8141`，没有宿主机 `ports`；公网和办公网只能访问受控的 adapter 8140。
4. adapter `/health/live` 和 `/health/ready` 正常，runtime `/ok` 只能从容器共享网络访问。
5. `wechat-article-db-init` 成功退出；`DATABASE_URL` 使用独立 database，业务 migration、checkpointer
   表、checkpoint 和 artifact 读写均正常。
6. `APP_MAX_ACTIVE_RUNS=30`、Ark/检索/Seedream 并发、数据库连接池与实际外部配额完成压测。
7. 生产 `DEBUG_ENABLED=false`，三个 thinking 开关和上下文扩展开关按真实 contract test 决定。
8. 执行一次完整生成、四类 HITL、revise、断线后继续、cancel 和 TTL 清理验收。
