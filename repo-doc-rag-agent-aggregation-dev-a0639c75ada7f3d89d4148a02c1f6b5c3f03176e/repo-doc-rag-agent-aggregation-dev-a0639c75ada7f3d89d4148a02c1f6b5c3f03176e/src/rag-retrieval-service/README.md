# RAG Retrieval Service

独立、无状态、page-first 的检索服务。项目从零重构，不依赖
`rag-retrieval-service-legacy` 的主流程；legacy 只用于参考数据库契约、token/LPT
思想和本地 Ark 配置。服务使用 LangGraph 编排、单进程全局 LLM 并发池、PostgreSQL
只读查询和代码规则 merge，不直接回答用户问题，也不改写正文。

## 检索模型

请求 `query` 支持尚未拆分的字符串，也支持调用方利用对话历史完成指代消解和拆分后的
字符串数组。默认 `robust` 分类策略先识别 scope 问题，再并行运行 direct、focused 和
统一目标文档分组；`RAG_QUERY_CLASSIFICATION_STRATEGY=fast` 可切换到原单次低延迟分类。
两种策略最终都把 query 物化为四类 group：

| category | 处理方式 |
|---|---|
| `routed_focused` | 路由目标文档，每篇文档独立扫描 node tree，最终只返回命中的 page 原文 |
| `routed_broad` | 路由目标文档，在 token 预算内用纯规则尽量覆盖正文 |
| `routed_direct` | 路由目标文档后直接访问指定页、章节、目录或文档摘要 |
| `scope_direct` | 不路由文档，直接生成请求范围元信息或简要概览 chunk |

node 只用于内部导航，不作为公开 chunk 与 page 竞争。Focused Search 可以逐级调用
`scan_node_tree`，树不可用时遍历候选 pages；page 检查和多文档路由使用 token 预算与
LPT 分组，批次进入全局并发队列，不使用固定 SQL `LIMIT` 截断全文。

多文档路由在 LLM 前由独立 LangGraph 节点根据文档名和 `doc_description` 做 keyword 硬预筛。
`RAG_DOCUMENT_PREFILTER_THRESHOLD=0` 会让所有文档通过预筛，适合做召回率对照实验；
默认值 `0.05` 启用硬筛。单个 group 超过 `RAG_DOCUMENT_PREFILTER_MAX_CANDIDATES=32`
时，节点使用 group 身份锚点和 scope IDF 做二级规则预筛。二级结果仍超限时不调用文档
路由 LLM，而是在返回预算内列出相似文档名并提示调用方补充目标文档信息。其余预筛结果
由 LLM 只挑出 `accept/possible`；未输出文档由代码视为 `reject`。

## API

### `POST /rag/v1/retrieve`

主要字段：

- `user_id`、`kb_id`：兼容字段；`user_id` 仍用于用户级 RPM 身份，但两者不再作为 SQL 文档隔离条件。
- `session_id`：可选的上游会话记录字段；为兼容调用链保留，不参与文档可见性判断。
- `doc_ids`、`temp_doc_ids`：调用方指定的请求范围，服务不得越界检索。
- `top_k`：全局软目标，不截断 direct 结果或 broad 完整性。
- `max_return_tokens`：最终 chunk 内容和 hint 的硬预算。
- `search_mode`：`semantic` 与 `hybrid` 使用新图；`keyword` 预留为未来全规则模式，
  当前返回 `MODE_NOT_IMPLEMENTED`。
- `options.include_debug`：请求检索 trace；只有进程
  `RAG_DEBUG_ENABLED=true` 时才真正收集并返回。

成功响应包含 `chunks`、`warnings`、`coverage`、`usage` 和可选 `debug`。正文结果始终
是数据库 page 原文；预算不足时可以返回带 `chunk_meta.content_truncated=true` 的连续
原文片段，同时必须返回 warning。内部结构化 hint 会在响应序列化前渲染为每个
chunk 的 `hint` 字符串。

Scope Access 的 `get_docs_metainfo` 工具允许模型通过 `enumeration_limit` 指定列举篇数，
用户未指定时默认 4 篇。实际数量取请求值与当前 scope 文档数的较小者；请求数量超过
实际文档数时，hint 会说明已列举全部文档。

### 文档辅助接口

- `POST /rag/v1/documents/meta`：批量返回指定正式/临时文档的 scoped 元信息；临时文档通过
  独立 `temp_doc_ids` 指定，不要求匹配上传 session。
- `POST /rag/v1/documents/route`：独立的多文档分组路由接口。调用方直接提供一组或多组
  `criteria`，服务复用检索图中的摘要窗口检查、token/LPT 分组和并行 LLM 判断，返回每组的
  `accept/possible/reject` 文档 ID。请求级 `keyword_prefilter` 默认 `false`；开启后才复用
  检索图既有的关键词硬预筛和候选规模门控。
- `POST /rag/v1/documents/raw`：返回 MinerU raw；缺失时异步触发原文档修复并返回 202。
- `GET /rag/v1/documents/raw/status`：只读查询 MinerU raw 修复状态。
- `GET /healthz`：进程存活，不访问数据库。
- `GET /readyz`：检查 PostgreSQL 就绪状态。

服务本身不验证 API Key。它必须位于可信内网或认证网关之后；调用方负责完成 doc_id
权限隔离。SQL 只按请求中的 doc_id 查询；`user_id`、`kb_id` 和 `session_id` 不作为文档
读取过滤条件。

## Debug 与可靠性

本项目不启用 LangSmith，使用进程内 `TraceCollector`。只有
`RAG_DEBUG_ENABLED=true` 和请求 `options.include_debug=true` 同时满足时才构造详细
trace；任一关闭都不收集 LLM 原始输出、工具历史或节点摘要，也不返回 `debug`。

`/retrieve` 和 `/documents/route` 在执行 LLM 工作前经过进程级公平准入：默认 30 个请求
执行、90 个请求按用户轮转排队；健康、就绪、meta 和 raw 接口不受该队列影响。用户级
防滥用限流默认允许 1200 RPM、burst 300，正常交互不会触发。所有模型调用统一经过全局 LLM
gateway，提供并发排队、单请求并发上限、timeout、轻量
熔断和阶段 usage。模型请求显式关闭 thinking。LPT 某一批失败时保留其他批次；关键
LLM 节点优先降级到规则实现，已有部分结果的超时返回 HTTP 200 + warning。

数据库池保留 `DB_POOL_MIN` 条常驻连接，并按配置周期回收超过最小规模且空闲到期的连接；
正在使用的连接不会被回收。

## 本地运行

```bash
cp .env.example .env
uv sync --dev
uv run uvicorn app.api.app:app --host 0.0.0.0 --port 8120 --workers 1
```

本地联调可以临时改用其他端口，例如 `8220`；生产默认端口仍为 `8120`。单实例只运行
一个 worker，因为 LLM 并发池、熔断器、数据库 executor 和用户 limiter 都是进程内对象。

## 验证

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest -m 'not live_db and not live_llm' -q
RAG_TEST_ENV_FILE=/path/to/retrieval.env uv run pytest tests/integration/test_live_db.py -q
RAG_TEST_ENV_FILE=/path/to/retrieval.env uv run pytest tests/live/test_live_llm.py tests/live/test_live_retrieval.py -q
```

真实 LLM 烟测使用未跟踪的 `.env`，不得打印 API Key、完整 prompt 或 page 正文。

```bash
uv run python -m scripts.run_live_smoke_report \
  --env-file /path/to/retrieval.env \
  --repeat 2 \
  --output docs/smoke-test-YYYY-MM-DD.md
```

Fast/robust 分类策略的真实豆包对照验收使用：

```bash
uv run python scripts/run_query_classification_acceptance.py \
  --repeats-per-mode 10 \
  --concurrency 4 \
  --output docs/robust-query-classification-acceptance.md
```

下游调用方式、字段、响应、warning 和错误处理见 [API 文档](docs/api.md)。详细架构、
state、fallback、预算和验收边界见 [实现设计](docs/implement.md)。
