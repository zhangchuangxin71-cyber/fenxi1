# Changelog

## 2026-08-20

### WeChat Article Agent

- 文档研读升级为素材调研：在用户确认 `target + about` 后并行处理用户文档和可选 Ark 内置联网搜索，
  支持事实、创作范例、资源扩展槽和流行文化四类网络素材。
- 新增素材归一化、一一对应压缩、上下文预算裁剪、事实冲突检查与一次性冲突 HITL；素材和 checkpoint
  仍沿用现有 artifact revision 与 PostgreSQL 持久化边界。
- Responses-like 产品流新增 `material_sources` 和 `material_conflicts` 两类 `agent.artifact`，开发面板
  同步增加来源列表、冲突决策、联网工具活动和 debug 调用展示。
- 新增 `WEB_SEARCH_ENABLED` 全局开关，默认关闭；联网调用复用现有 Ark 并发、重试、限流和熔断配置。

## 2026-08-13

### WeChat Article Agent

- 统一部署新增微信公众号文章 Agent：公开 Responses-like adapter 使用 8140，私有 LangGraph Agent
  Server 只在 Compose 网络暴露 8141，并通过服务 DNS 与 adapter 通信。
- 增加 PostgreSQL checkpoint、版本化 article artifact、四类 HITL、断点继续、正式 cancel、三天 TTL、
  Ark/Seedream/检索网关以及本地开发面板。
- 增加跨 Agent Responses-like 扩展协议、前端接入指南和完整环境变量说明；统一环境变量清单补充
  `wechat-article.env` 的数据库、Ark 和内部服务地址要求。
- 统一部署现在包含六个业务服务、七个容器；开发面板仅用于本地开发，不进入生产 Compose。

## 2026-08-03

### Deployment storage

- MinerU 镜像将大型依赖安装与业务源码复制拆成稳定构建层，普通源码变化不再重复生成
  数 GB 的 PyTorch/pipeline 依赖缓存。
- 顶层部署脚本默认复用现有 MinerU 镜像；镜像不存在时自动首建，修改 MinerU 后可通过
  `--rebuild-mineru` 显式重建。
- 五个生产服务统一启用 Docker JSON 日志轮转，单文件上限 `50MB`，最多保留 `3` 个文件。

## 2026-07-30

### Retrieval classification

- 检索接口 query 支持 `string | string[]`；知识库问答利用完整对话历史生成已指代消解、拆分且
  不丢子问题的数组，其他调用方可继续传字符串。
- 新增可配置的 `robust` scope-first 分类策略，并保留原单次 `fast` 策略用于低延迟场景和对照。
- robust 内部使用三个专职二分类器和统一目标文档分组，仍只占用现有一个 LangGraph
  `classify_query` node，不改变 keyword、路由、检索和 merge 链路。
- 增加真实豆包 fast/robust 分类对照验收脚本与报告；400 次运行中 robust 的子问题保留、
  分类、文档分组和路由参数指标均为 100%，strict schema 与 fallback 均无失败。

## 2026-07-24

### Retrieval

- `rag-retrieval-service` 从 legacy keyword 主链路重构为 page-first LangGraph 检索，
  支持 focused、broad、direct 和 scope 四类复合 query 工作组。
- node 只作为内部目录树导航单位，最终正文统一返回 page 原文；新增全局软 `top_k`、
  `max_return_tokens` 硬预算、coverage、warning、连续原文截断和统一 chunk hint。
- 增加文档名 + description keyword 预筛、LPT 分组、全局 LLM 并发池、熔断、规则
  fallback，以及不依赖 LangSmith 的详细 trace。
- 文档路由和 page inspection 的 LLM 输出改为稀疏 `accept/possible` 选择，遗漏项由代码
  补为 `reject`，减少无关负例的输出 token；非法或重复引用仍触发批次 fallback。
- Scope 元信息工具支持模型指定列举篇数，默认 4，并对实际请求文档数取最小值。

### Knowledge Chat

- 自定义 `rag` 对象增加可选 `session_id`；使用临时文档时条件必填，并透传给检索服务。
- 保持 OpenAI Chat Completions 风格的无状态语义，聊天历史仍由 `messages` 管理。
- 增加动态 `max_return_tokens`、可配置检索 timeout、复合问题保真改写和独立的检索
  trace 透传；query 改写强制消解指代、拆解跨文档事实问题并保留元信息子问题，无法
  唯一消解时先向用户澄清。
- 开发面板增加检索专用 trace、原始检索问题、预算字段和 hint 优先的引用详情。
- 知识库问答请求补齐 OpenAI 风格默认值；省略 `top_k` 时按 query 与文档范围在 `4–20`
  内动态计算，显式值作为兼容上限。

### Report Agent

- 保持原 Agent 主链路不变，生产 chunk 来源切换为独立远程检索服务；内置 PostgreSQL
  retriever 只保留 legacy 兼容。
- 兼容 page-only chunk 和动态 `max_return_tokens`，并将 `session_id` 透传用于临时文档。
- 增加可配置的远程检索 timeout 和稳定的上游错误映射。

### Contract And Operations

- 明确 `session_id` 不是模型历史或 LangGraph checkpoint：检索服务用它隔离临时文档，
  报告服务还会在 SSE 中回显，知识库问答仅在自定义 `rag` 范围内使用。
- 检索服务健康接口保持 `/healthz`、`/readyz`；生产默认端口保持 8120，知识库问答
  默认端口保持 8130。
- 新检索服务没有服务级 API Key 鉴权，必须部署在可信内网或认证网关之后；统一部署
  已删除废弃的 retrieval allowlist/caller key 模板、强制预检和一致性比较。
- 知识库问答入站 key 更名为 `KNOWLEDGE_CHAT_INBOUND_API_KEYS`；仅在
  `AUTH_ENABLED=true` 时由应用启动校验和部署脚本条件预检，且不会转发给检索服务。
- 五个服务的 Docker 构建统一覆盖国内基础镜像、Debian、pip/uv 和 PyTorch 下载源；
  新检索与知识库问答不再从 GHCR 拉取 uv，并支持通过 `RAG_BUILD_*` 使用现场内网镜像。
