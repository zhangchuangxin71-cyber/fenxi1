# rag-knowledge-chat

无状态的知识库问答服务，对外提供 OpenAI Chat Completions 兼容的
`POST /v1/chat/completions`，并在标准 chunk 顶层增加 `rag` 扩展字段。

完整请求、SSE 事件和错误契约见 [API 文档](docs/api.md)。

## 工作方式

每个请求最多调用 Ark 两次：

1. 第一次调用使用 `response_format=json_schema` 和 `strict=true` 进行受控路由，
   固定关闭 thinking，输出 `needs_retrieval`、独立检索问题组成的 `query` 数组和 `reason_code`。该步骤
   必须根据完整历史消解指代、把跨文档比较拆成独立事实子问题并保留所有元信息子问题；
   无法唯一消解指代时返回 `clarification`，不调用检索服务。
2. 若需要知识库，代码把请求中的
   `user_id/kb_id/session_id/doc_ids/temp_doc_ids` 注入检索请求，固定调用
   `search_mode=hybrid`。模型不能修改权限范围。`session_id` 只用于临时文档归属，
   不用于聊天历史；`temp_doc_ids` 非空时必须提供。
3. 第二次调用流式生成最终回答。检索成功时使用编号证据和引用；不检索、无结果或检索错误时，
   代码先输出明确的通识回答声明。该调用是否启用 thinking 由 `DOUBAO_THINKING_TYPE` 控制。

服务不保存会话历史、用户记忆或 usage。调用者必须通过 `messages` 传入所需上下文。
`rag.session_id` 是可选的临时文档访问作用域，不改变服务的无状态性质。

## 启动

```bash
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8130
```

完整字段含义、生产推荐值和上线检查见 [环境变量说明](docs/environment-variables.md)。
面向下游调用方的请求、历史消息、SSE 和错误处理见 [API 文档](docs/api.md)。

复制 `.env.example` 为本地 `.env` 并配置：

- `ARK_API_KEY`、`ARK_BASE_URL`、`MODEL_SMART`、`DOUBAO_THINKING_TYPE`
- `RAG_RETRIEVAL_SERVICE_URL`
- `RAG_RETRIEVAL_TIMEOUT_SECONDS`：调用独立检索服务的 HTTP 超时秒数，默认 `180`
- 可选 `KNOWLEDGE_CHAT_INBOUND_API_KEYS=caller:key,...`

默认 `AUTH_ENABLED=false`，前端无需携带 API Key。若设置 `AUTH_ENABLED=true`，必须显式配置
`KNOWLEDGE_CHAT_INBOUND_API_KEYS`，格式为逗号分隔的 `caller:key`。启动与统一部署预检都会拒绝
缺失或格式错误的 key。知识库问答不会把入站 key 转发给检索服务；新检索服务没有内部
API Key 鉴权，生产隔离依赖可信内网或网关以及检索 SQL 中的 `user_id + kb_id` 范围。

生产模板按单容器、最多 30 个并发请求配置：

```env
REQUEST_RPM_LIMIT=600
ARK_RPM_LIMIT=1800
ARK_MAX_CONCURRENCY=30
```

应用限制是进程内保护，Ark 账号的实际并发/RPM/TPM 仍是外部上限。容器部署：

```bash
docker build -t rag-knowledge-chat:0.1.0 .
docker run --rm --env-file .env -p 127.0.0.1:8130:8130 rag-knowledge-chat:0.1.0
docker compose up -d --build
```

Compose 会加入外部 `pageindex` 网络，以解析 `rag-retrieval-service`。请先启动
入库/PostgreSQL 栈，或先执行 `docker network create pageindex`。

## 请求

```bash
curl -N http://127.0.0.1:8130/v1/chat/completions \
  -H "Authorization: Bearer <KNOWLEDGE_CHAT_INBOUND_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "rag-knowledge-chat",
    "messages": [{"role": "user", "content": "请总结这份制度"}],
    "stream": true,
    "rag": {
      "user_id": "user-1",
      "kb_id": "kb-1",
      "session_id": null,
      "doc_ids": ["doc-1"],
      "incremental_doc_ids": [],
      "temp_doc_ids": [],
      "include_debug": false
    }
  }'
```

除 `messages` 和 `rag` 业务范围外，接口默认使用 `model=rag-knowledge-chat`、`stream=true`、
`stream_options.include_usage=false`、`temperature=0.2`、`max_tokens=1200`。省略 `top_k` 和
`max_return_tokens` 时，服务分别在 `4–20` 和 `4096–RAG_MAX_RETURN_TOKENS` 范围内动态计算
检索预算；显式传入的 `max_return_tokens` 必须在 `128–RAG_MAX_RETURN_TOKENS` 内。当前
doubao-2.0-lite 部署默认上限为 `180000`。本期只支持文本 `user/assistant` 消息和
`stream=true`。调用者提供
`system/developer/tool/function` 消息、`stream=false` 或
`stream_options.include_usage=true` 时，会在 SSE 开始前返回 `400`。

调用方可通过 `rag.incremental_doc_ids` 声明本轮新增的正式或临时文档。该列表必须是
`doc_ids ∪ temp_doc_ids` 的子集，两类 ID 可以混合传入；知识库问答按所属范围自动分区并
批量获取真实文档名，用于消解“另外几篇”“刚新增的文档”等指代。服务保持无状态，
不自行保存或比较上一轮文档范围。

临时文档请求必须同时提供归属 session：

```json
{
  "rag": {
    "user_id": "user-1",
    "kb_id": "kb-1",
    "session_id": "upload-session-1",
    "doc_ids": [],
    "temp_doc_ids": ["temp-doc-1"]
  }
}
```

`session_id` 不会恢复历史；缺少它的临时文档请求在 SSE 开始前返回 `400`。

## SSE 扩展

正文仍位于标准 `choices[0].delta.content`。状态事件示例：

```json
{
  "id": "chatcmpl_xxx",
  "object": "chat.completion.chunk",
  "choices": [{"index": 0, "delta": {}, "finish_reason": null}],
  "rag": {
    "event": {
      "type": "status",
      "stage": "retrieval",
      "message": "正在检索知识库"
    }
  }
}
```

回答生成启用 thinking 后，收到第一个 reasoning 分片时会发送一次
`stage=thinking`、`message=正在思考` 的状态事件，随后以
`choices[0].delta.reasoning_content` 增量转发 reasoning；正文仍从
`choices[0].delta.content` 输出。意图路由不受该配置影响，始终关闭 thinking。

终止 chunk 的 `rag` 包含：

- `answer_basis`: `knowledge_base`、`general_no_retrieval`、
  `general_no_result` 或 `general_retrieval_error`
- `retrieval`: 检索状态和 request ID
- `references`: 回答实际使用的引用
- `chunks`: 编号后的候选证据
- `debug`: 问答 trace 由 `DEBUG_ENABLED` 控制；检索 trace 由检索服务
  `RAG_DEBUG_ENABLED` 与本次 `rag.include_debug` 共同控制

正文中的 `[N]` 通过 `citation_index` 与 `rag.references`、`rag.chunks` 关联。
`references` 只包含回答实际使用的来源元数据；`chunks` 还包含对应原文。后端不输出展示用
HTML/Markdown，文档名、页码、节点和原文预览由前端渲染。

SSE 开始后的模型错误通过顶层 `rag.error` 返回，HTTP 状态保持 200，随后发送
`data: [DONE]`。

## Debug 面板

开发面板是独立进程，正式应用不会导入 `dev`：

```bash
uv run uvicorn dev.server:app --host 127.0.0.1 --port 8501
```

浏览器打开 `http://127.0.0.1:8501`。面板可以从
`rag-offline-eval/dataset/manifests/query_labels.jsonl` 生成 gold doc 加随机噪声文档的请求，
中栏展示状态与回答，右栏展示 trace 和原始 SSE。RAG API Key 只保存在 dev backend。
中栏会显示 thinking 状态，并在回答下方按实际引用渲染可展开的文档、页码/节点和原文预览。

## 验证

```bash
uv run ruff check .
uv run ruff format --check .
LOAD_CHAT_API_KEY=<API_KEY> uv run python -m scripts.load_chat \
  --base-url http://127.0.0.1:8130 \
  --requests 30 --concurrency 30
uv run pytest -q
uv run python -m scripts.smoke_fake
uv run python -m scripts.smoke_real
uv run python -m scripts.smoke_live_pipeline
SMOKE_CHAT_URL=http://127.0.0.1:8130 uv run python -m scripts.smoke_http
SMOKE_CHAT_URL=http://127.0.0.1:8130 uv run python -m scripts.smoke_openai_sdk
```

真实烟测使用本地 `.env` 中的 Ark 配置。输出和烟测报告不得包含任何 API Key。
压测若只出现 `ark_upstream_error` 而没有本地 429，应先核对单个 Ark Key 的配额。
