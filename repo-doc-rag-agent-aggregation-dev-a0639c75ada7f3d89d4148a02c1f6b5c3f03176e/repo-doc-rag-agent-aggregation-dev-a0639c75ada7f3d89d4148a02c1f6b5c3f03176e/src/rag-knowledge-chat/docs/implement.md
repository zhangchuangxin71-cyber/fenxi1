# rag-knowledge-chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现一个无状态、OpenAI Chat Completions 兼容的知识库问答服务。模型自行判断是否调用内部知识库检索工具；需要检索时调用现有 `rag-retrieval-service`，最多使用两次 LLM 调用，并通过 SSE 返回状态、正文、引用和可选 debug 信息。

**Architecture:** 使用精简的 FastAPI 异步服务和显式编排，不引入 LangGraph。第一次 Ark 调用通过 `response_format=json_schema`、`strict=true` 的受控解码完成意图判断并生成独立检索 query；代码根据判断决定是否调用检索服务，并始终用第二次 Ark 调用流式生成最终回答。外部协议保持标准 Chat Completions chunk，并在顶层增加 `rag` 扩展字段。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic v2、pydantic-settings、OpenAI Python SDK 的 `AsyncOpenAI`、httpx、sse-starlette、pytest、pytest-asyncio、respx、uv；开发面板使用独立 FastAPI 后端和原生 HTML/CSS/JavaScript。

---

## 0. 实施状态

本计划已于 2026-07-15 完成实现和验收。最终代码、自动化测试、真实 Ark/检索/HTTP SSE
结果分别见 `README.md`、`tests/`、`docs/ark-compatibility.md` 和
`docs/smoke-report-2026-07-15.md`。实施期间根据新增要求把原生 `tool_choice` 方案替换为
`response_format=json_schema + strict=true` 的受控路由，本文档已同步为最终方案。

## 1. 已确认的产品边界

### 1.1 本期必须实现

- 正式接口为 `POST /v1/chat/completions`。
- 接口无状态，不保存会话、聊天记录、用户记忆或摘要；调用者通过 `messages` 传入全部上下文。
- 请求必须携带 RAG API Key：`Authorization: Bearer <RAG_API_KEY>`。
- 请求通过顶层 `rag` 字段传入 `user_id`、`kb_id`、可选 `session_id`、
  `doc_ids`、`temp_doc_ids`、`top_k`、`max_return_tokens` 和 `include_debug`。
  `temp_doc_ids` 非空时 `session_id` 条件必填；它只用于临时文档归属，不用于历史管理。
- 只实现 `stream=true` 的完整链路；`stream=false` 本期明确返回不支持错误，不能静默改变行为。
- 第一轮 LLM 调用使用 strict JSON Schema 输出 `needs_retrieval/query/reason_code`，不得自由生成 JSON。
- 检索工具固定向 `rag-retrieval-service` 发送 `search_mode=hybrid`，模型无权修改检索模式和权限范围。
- 一次问答最多两次 LLM 调用：第一次进行受控路由，第二次流式生成最终回答。
- 检索为空或检索发生任何错误（包括 `401/403`）时，降级为通识回答，并由代码注入明确免责声明。
- SSE 在通过格式校验、API Key 校验、明显的上下文大小预检和首个 LLM 限流许可后才开始。
- SSE 开始后的错误使用 `HTTP 200 + rag.error` 表达。
- 实现单进程请求限流、Ark RPM 限流和并发限制，目标是防止打满上游 Ark API。
- 实现独立的三栏开发面板，支持请求生成、请求编辑、SSE 对话展示和 debug 链路回放。

### 1.2 本期明确不做

- 不使用 LangGraph，不实现循环 agent 或多工具自治规划。
- 不管理历史，不持久化用户 usage，不保存运行历史。
- 不实现 Redis 限流和多实例一致性限流。
- 不实现本地 SQLite、请求模板 CRUD 或开发面板历史记录。
- 不实现严格的零扩展 OpenAI 协议；允许顶层 `rag` 扩展。
- 不实现完整的 `stream=false` 响应。
- 不实现跨知识库路由，不允许模型改变 `user_id/kb_id/doc_ids`。
- 本期不汇总或持久化 token usage。若请求设置 `stream_options.include_usage=true`，在 SSE 开始前返回 `400 unsupported_parameter`，避免返回含义不准确的 usage。

## 2. 通俗流程说明

### 2.1 请求进入服务

1. 前端把历史消息、当前问题和知识库范围发送到 `/v1/chat/completions`。
2. 服务检查 JSON 格式、消息角色、最后一条用户消息、RAG API Key、请求大小、消息数量、文本总长度、文档 ID 数量以及 `stream=true`。本期只接受文本类型的 `user/assistant` 消息；调用者不能传入 `system/developer/tool/function` 消息覆盖服务身份和工具策略。
3. 服务执行单进程请求限流，并为必然发生的第一次 Ark 调用取得 RPM 许可。
4. 只有上述步骤全部成功后才打开 SSE，先发送“正在判断是否需要检索知识库”的状态。

### 2.2 第一次 LLM 调用：判断是否检索

第一次调用同时收到完整 `messages`、系统提示词和以下受控输出 schema：

```json
{
  "type": "json_schema",
  "json_schema": {
    "name": "knowledge_chat_route",
    "strict": true,
    "schema": {
      "type": "object",
      "properties": {
        "needs_retrieval": {
          "type": "boolean",
          "description": "事实性知识问答是否需要调用知识库检索。"
        },
        "query": {
          "type": "string",
          "description": "结合对话历史改写出的、语义完整且可独立检索的问题。"
        },
        "reason_code": {
          "type": "string",
          "enum": ["identity", "chitchat", "general_knowledge", "knowledge_base"]
        }
      },
      "required": ["needs_retrieval", "query", "reason_code"],
      "additionalProperties": false
    }
  }
}
```

模型只有两种合法路由结果：`needs_retrieval=false` 时不调用检索，`needs_retrieval=true` 时使用受控输出中的 query 调用检索。第一次调用不直接输出用户可见答案，最终答案统一由第二次调用流式生成。

系统提示词必须明确：

- 助手身份是“由广州日报与光明实验室联合开发的知识库问答助手”。
- 身份询问、打招呼、感谢、闲聊和纯创作不检索。
- 事实性、解释性、总结、比较、制度和报告问题默认优先检索，即使模型知道通识答案。
- 对“它呢”“上述制度是什么”等依赖历史的问题，query 必须结合 `messages` 改写为独立问题。
- `needs_retrieval=false` 时 query 必须为空；需要检索但 query 为空时，代码使用最后一条用户文本兜底。

Ark 与 OpenAI 协议差异集中封装在 `app/integrations/ark.py`，业务编排只消费校验后的结构化路由结果。

### 2.3 代码调用检索工具

模型只提供 query。服务端按以下方式构造检索请求：

```json
{
  "user_id": "来自 request.rag.user_id",
  "kb_id": "来自 request.rag.kb_id",
  "session_id": "来自 request.rag.session_id；没有临时文档时可为 null",
  "query": "来自工具调用的 query",
  "doc_ids": ["来自 request.rag.doc_ids"],
  "temp_doc_ids": ["来自 request.rag.temp_doc_ids"],
  "top_k": 8,
  "search_mode": "hybrid",
  "options": {
    "include_document_meta": true,
    "include_debug": false
  }
}
```

检索请求的 `include_debug` 直接使用 `request.rag.include_debug`；是否生成检索 trace 由
检索服务自己的 `RAG_DEBUG_ENABLED` 再次控制。问答服务的 `DEBUG_ENABLED` 只控制问答
路由/生成 trace，不能屏蔽检索端已经返回的 trace。请求使用服务端配置的兼容 Bearer；
当前新检索服务不做 API Key 校验，必须由可信网络边界保护。

### 2.4 第二次 LLM 调用：生成最终回答

检索完成后只允许再调用一次 Ark：

- **有 chunks**：给每个 chunk 分配稳定编号 `[1]`、`[2]`，把规范化后的证据交给模型。模型只能根据证据回答，并在相关结论后标注引用。
- **`chunks=[]`**：代码先输出“知识库中未检索到相关内容，以下内容由模型基于通识生成：”，再让模型基于通识回答。
- **检索超时、连接失败、`400/401/403/429/5xx` 或响应格式错误**：代码先输出“知识库检索暂时不可用，以下内容由模型基于通识生成：”，再让模型基于通识回答。错误摘要记录在 `rag` 元数据和 debug trace 中，但 API Key、原始提示词和敏感堆栈不得返回。

免责声明由代码输出而不是只依赖提示词，从而保证每次降级都明确告知用户。提示词仍需再次约束模型，避免它声称已经查询知识库。

## 3. API 契约

### 3.1 请求示例

```http
POST /v1/chat/completions
Authorization: Bearer <RAG_API_KEY>
Content-Type: application/json
```

```json
{
  "model": "rag-knowledge-chat",
  "messages": [
    {"role": "user", "content": "这份制度的审批流程是什么？"}
  ],
  "stream": true,
  "temperature": 0.2,
  "max_tokens": 1200,
  "rag": {
    "user_id": "user-1",
    "kb_id": "kb-1",
    "session_id": null,
    "doc_ids": ["doc-1", "doc-2"],
    "temp_doc_ids": [],
    "top_k": 8,
    "max_return_tokens": 8192,
    "include_debug": false
  }
}
```

本期只接受配置中的虚拟模型名 `rag-knowledge-chat`。Ark endpoint/model 由服务端环境变量控制，调用者不能选择任意 Ark 模型。

### 3.2 SSE chunk

所有事件使用 data-only SSE：`data: <json>\n\n`，最后发送 `data: [DONE]\n\n`。

状态 chunk 保留标准外壳，并在顶层增加 `rag.event`：

```json
{
  "id": "chatcmpl_01H...",
  "object": "chat.completion.chunk",
  "created": 1780000000,
  "model": "rag-knowledge-chat",
  "choices": [
    {"index": 0, "delta": {}, "finish_reason": null}
  ],
  "rag": {
    "event": {
      "type": "status",
      "stage": "retrieval",
      "message": "正在检索知识库"
    }
  }
}
```

正文使用标准 `choices[0].delta.content`。成功终止 chunk 的 `finish_reason` 为标准值 `stop` 或 `length`，并携带最终 RAG 元数据：

```json
{
  "id": "chatcmpl_01H...",
  "object": "chat.completion.chunk",
  "created": 1780000000,
  "model": "rag-knowledge-chat",
  "choices": [
    {"index": 0, "delta": {}, "finish_reason": "stop"}
  ],
  "rag": {
    "answer_basis": "knowledge_base",
    "retrieval": {
      "status": "success",
      "request_id": "retrieval-request-id"
    },
    "references": [],
    "chunks": []
  }
}
```

`answer_basis` 只有以下四种：

- `knowledge_base`
- `general_no_retrieval`
- `general_no_result`
- `general_retrieval_error`

### 3.3 流内错误

SSE 已开始后，错误用一个带 `rag.error` 的标准外壳返回，然后发送 `[DONE]`：

```json
{
  "id": "chatcmpl_01H...",
  "object": "chat.completion.chunk",
  "created": 1780000000,
  "model": "rag-knowledge-chat",
  "choices": [
    {"index": 0, "delta": {}, "finish_reason": null}
  ],
  "rag": {
    "error": {
      "code": "ark_upstream_error",
      "message": "模型服务暂时不可用",
      "retryable": true,
      "request_id": "chat-request-id"
    }
  }
}
```

### 3.4 SSE 开始前的错误

使用稳定 JSON 结构并返回正确 HTTP 状态：

```json
{
  "error": {
    "code": "invalid_request",
    "message": "最后一条消息必须是 user 消息",
    "retryable": false,
    "request_id": "chat-request-id"
  }
}
```

至少覆盖 `400 invalid_request`、`400 stream_required`、`400 unsupported_parameter`、`401 invalid_api_key`、`413 request_too_large`、`429 rate_limit_exceeded` 和 `503 service_unavailable`。

## 4. 项目目录结构

```text
rag-knowledge-chat/
├── app/
│   ├── __init__.py
│   ├── main.py                         # FastAPI 生命周期、路由挂载、共享依赖
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py                   # /v1/chat/completions 和 /health
│   │   ├── contracts.py                # OpenAI 请求、chunk、rag 扩展和错误模型
│   │   └── dependencies.py             # settings、鉴权、编排器依赖
│   ├── chat/
│   │   ├── __init__.py
│   │   ├── orchestrator.py             # 最多两次 LLM 调用的唯一流程编排入口
│   │   ├── models.py                   # 内部 RouteDecision、ToolResult、AnswerBasis
│   │   ├── prompts.py                  # 身份、工具选择、证据回答、降级回答提示词
│   │   ├── evidence.py                 # chunk 规范化、编号、引用校验和 references
│   │   └── events.py                   # 与 HTTP/SSE 无关的内部事件定义
│   ├── integrations/
│   │   ├── __init__.py
│   │   ├── ark.py                      # AsyncOpenAI/Ark 协议适配、流和异常映射
│   │   └── retrieval.py                # /rag/v1/retrieve 客户端和响应规范化
│   ├── streaming/
│   │   ├── __init__.py
│   │   └── openai_sse.py               # 内部事件转换为 ChatCompletionChunk
│   └── platform/
│       ├── __init__.py
│       ├── settings.py                 # 环境变量和启动校验
│       ├── auth.py                     # RAG API Key 校验和安全指纹
│       ├── rate_limit.py               # RateLimiter 协议和单进程实现
│       ├── trace.py                    # DebugTraceCollector 与 no-op 实现
│       └── errors.py                   # 稳定业务异常和上游错误分类
├── dev/
│   ├── __init__.py
│   ├── server.py                       # 独立 dev FastAPI，不被 app.main 导入
│   ├── dataset.py                      # query_labels.jsonl 与噪声 doc 采样
│   └── static/
│       ├── index.html                  # 左配置、中对话、右 trace 三栏页面
│       ├── app.js                      # JSON 编辑、SSE 解析、链路回放
│       └── styles.css                  # 开发工具所需的紧凑布局
├── tests/
│   ├── unit/
│   │   ├── test_contracts.py
│   │   ├── test_auth.py
│   │   ├── test_rate_limit.py
│   │   ├── test_prompts.py
│   │   ├── test_ark.py
│   │   ├── test_evidence.py
│   │   └── test_dataset.py
│   ├── integration/
│   │   ├── test_chat_direct.py
│   │   ├── test_chat_retrieval.py
│   │   ├── test_chat_fallback.py
│   │   ├── test_chat_errors.py
│   │   └── test_debug_disabled.py
│   └── contract/
│       └── test_openai_sse.py
├── scripts/                            # fake、真实 Ark、live pipeline、HTTP/SDK smoke
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
└── uv.lock
```

### 4.1 关键可维护性边界

- `orchestrator.py` 只编排，不拼 HTTP JSON、不直接读环境变量、不实现限流算法。
- `ark.py` 隔离 Ark 与 OpenAI 协议差异；未来更换模型供应商时不修改编排器。
- `retrieval.py` 只负责请求/响应映射；模型永远接触不到权限范围参数。
- `events.py` 定义业务事件，`openai_sse.py` 才负责把它们变成线上的 chunk，避免业务逻辑绑定 SSE。
- `RateLimiter` 使用小型协议接口，当前实现为 `InMemoryRateLimiter`；未来新增 Redis 实现时只替换依赖注入，不修改路由和编排器。
- `DebugTraceCollector` 提供 enabled 和 no-op 两种实现。debug 关闭时不构造大对象、不复制检索原始响应。
- `dev/server.py` 是完全独立的进程入口，正式服务不导入 `dev`，从根本上避免开发面板带来线上性能损失。

## 5. 限流、超时和取消

### 5.1 单进程限流

实现两个独立限制：

- **调用方请求限制**：按 `(api_key_fingerprint, user_id)` 计算 RPM，防止单个调用者持续占用服务。
- **Ark 调用限制**：按实际 Ark 调用次数计数，而不是按 HTTP 请求计数，因为检索路径会调用 Ark 两次。使用全局 RPM 窗口并保留安全余量。

第一次 Ark 许可在 SSE 开始前取得；第二次许可在检索完成后取得。若第二次许可在配置等待时间内不可用，通过 `rag.error` 终止流。另使用 `asyncio.Semaphore` 限制同时进行的 Ark 调用数。

接口建议保持为：

```python
class RateLimiter(Protocol):
    async def acquire(self, key: str, *, cost: int = 1) -> None: ...
```

未来 Redis 实现沿用同一接口，但本期不创建 Redis 依赖、配置或空壳代码。

### 5.2 超时预算

- 整体请求有总 deadline。
- 第一次 Ark、检索 HTTP、第二次 Ark 和第二次限流等待分别有阶段超时。
- 阶段超时不得超过剩余总 deadline。
- 浏览器断开时取消正在进行的 Ark stream、检索请求和后续生成。
- 检索超时属于可降级错误；Ark 超时属于问答失败，使用流前 HTTP 错误或流内 `rag.error`。

## 6. Debug 与开发面板

### 6.1 Debug 双开关

问答服务自身的完整 debug 只有在以下条件同时满足时产生：

```text
DEBUG_ENABLED=true AND request.rag.include_debug=true
```

debug 打开时，终止 chunk 的 `rag.debug` 至少包含：

- 第一轮结果是直接回答还是工具调用。
- 模型给出的独立检索 query。
- 检索请求的非敏感字段。
- 检索服务状态、耗时、响应中的 debug trace 和规范化错误。
- 各阶段开始/结束时间、耗时和最终 answer basis。

debug 不得包含 RAG API Key、Ark API Key、完整系统提示词、模型隐藏推理、Python 堆栈或未脱敏环境变量。问答 debug 关闭时不记录问答 trace；但请求显式设置
`rag.include_debug=true` 且检索端允许 debug 时，仍应透传 `rag.debug.retrieval`。两种
debug 均关闭时不得产生额外 trace 开销，终止事件中不得出现 `rag.debug`。

### 6.2 开发面板

开发面板通过单独命令启动，不注册到正式 FastAPI 应用。dev backend 从服务端环境变量读取 API Key，浏览器不保存或展示密钥。

左栏：

- Chat 服务 URL、manifest 目录、`user_id`、`kb_id`、噪声文档数量和随机种子。
- 默认 manifest 目录为相邻仓库 `rag-offline-eval/dataset/manifests`，允许在页面修改。
- “生成请求体”读取 `query_labels.jsonl`，选择一条 `query_text`，把该行 `doc_id` 作为 gold doc，再从全文件去重 doc 池中按固定种子抽取噪声 doc。
- 可编辑 JSON textarea、“格式化 JSON”和“发送请求”按钮。

中栏：

- 展示用户消息、助手状态和标准 `delta.content` 吐字。
- 状态来自 `rag.event`，不把“正在检索”混入最终回答正文。
- 收到终止 chunk 或 `rag.error` 后正确结束 loading 状态。

右栏：

- 按时间顺序显示 `request_validated`、`route_started`、`tool_selected`、`retrieval_started`、`retrieval_completed`、`generation_started`、`first_token` 和 `generation_completed`。
- 节点可展开查看 query、耗时、retrieval request ID、chunks、references 和 retrieval debug。
- 提供折叠的原始 SSE 事件区，便于排查协议问题。

## 7. 分阶段实施任务

### Task 0: Ark strict 受控解码能力验证

**Files:**

- Create: `scripts/smoke_real.py`
- Create: `docs/ark-compatibility.md`

- [ ] **Step 1:** 用与 `rag-report-agent` 相同的 Ark base URL、endpoint/model 和 `AsyncOpenAI` 调用方式编写一次性验证脚本，不接入正式应用。
- [ ] **Step 2:** 分别发送一个身份问题和一个知识库问题，验证 `response_format=json_schema`、`strict=true`、结构化路由字段以及第二次调用的正文流。
- [ ] **Step 3:** 在 `docs/ark-compatibility.md` 记录 Ark 实际接受的参数、返回字段和与 OpenAI 协议的差异，不记录 API Key 或完整敏感响应。
- [ ] **Step 4:** 若当前 Ark endpoint 无法稳定执行 strict 结构化输出，暂停后续实现并修订本计划；不得退回自由 JSON 或增加第三次 LLM 调用。
- [ ] **Step 5:** 验证通过后提交能力记录，提交信息建议为 `docs: verify ark streaming tool compatibility`。

### Task 1: 初始化项目与配置

**Files:**

- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `app/__init__.py`
- Create: `app/main.py`
- Create: `app/platform/settings.py`
- Create: `tests/unit/test_settings.py`

- [ ] **Step 1:** 用测试定义必需配置、默认虚拟模型名、超时、请求上限、debug 开关、请求 RPM、Ark RPM 和并发上限。
- [ ] **Step 2:** 运行 `uv run pytest tests/unit/test_settings.py -v`，确认因 settings 尚未实现而失败。
- [ ] **Step 3:** 创建最小 FastAPI 应用和 Pydantic Settings；启动时检查 Ark endpoint/model/API key、检索 URL 和允许的 RAG API Keys。
- [ ] **Step 4:** 运行 settings 测试和 `uv run python -c "from app.main import app; print(app.title)"`，期望成功输出应用名。
- [ ] **Step 5:** 提交项目骨架，提交信息建议为 `chore: scaffold knowledge chat service`。

### Task 2: 请求契约、预检和鉴权

**Files:**

- Create: `app/api/contracts.py`
- Create: `app/api/routes.py`
- Create: `app/api/dependencies.py`
- Create: `app/platform/auth.py`
- Create: `app/platform/errors.py`
- Create: `tests/unit/test_contracts.py`
- Create: `tests/unit/test_auth.py`
- Create: `tests/integration/test_chat_errors.py`

- [ ] **Step 1:** 为合法请求、缺少最后一条 user message、`stream=false`、`include_usage=true`、过大 messages、非法 role、非法 top_k 和缺少 Bearer key 编写失败测试。
- [ ] **Step 2:** 运行上述测试，确认契约和路由尚未实现导致失败。
- [ ] **Step 3:** 实现请求模型、稳定错误模型、常量时间 API Key 比较和上下文大小预检。
- [ ] **Step 4:** 实现 `/health` 和 `/v1/chat/completions` 的流前错误路径；此阶段成功请求可返回明确的 `501 orchestration_not_ready`。
- [ ] **Step 5:** 运行 `uv run pytest tests/unit/test_contracts.py tests/unit/test_auth.py tests/integration/test_chat_errors.py -v`，期望全部通过。
- [ ] **Step 6:** 提交协议与鉴权，提交信息建议为 `feat: define chat completion contract and auth`。

### Task 3: Ark 适配器和单进程限流

**Files:**

- Create: `app/integrations/ark.py`
- Create: `app/platform/rate_limit.py`
- Create: `tests/unit/test_ark.py`
- Create: `tests/unit/test_rate_limit.py`

- [ ] **Step 1:** 为 strict 路由结果、文本 delta、结束原因、Ark 认证错误、上下文错误、超时和取消传播编写适配器测试。
- [ ] **Step 2:** 为请求维度和 Ark 调用维度的窗口限流、并发信号量、等待超时编写可控时钟测试。
- [ ] **Step 3:** 运行测试，确认 Ark provider 和 limiter 尚不存在。
- [ ] **Step 4:** 参考 `rag-report-agent` 的 `AsyncOpenAI` Ark 调用方式实现适配层，将供应商异常映射为内部稳定异常。
- [ ] **Step 5:** 实现 `RateLimiter` 协议和单进程实现；Ark limiter 按实际调用次数扣减。
- [ ] **Step 6:** 运行 `uv run pytest tests/unit/test_ark.py tests/unit/test_rate_limit.py -v`，期望全部通过。
- [ ] **Step 7:** 提交 Ark 与限流，提交信息建议为 `feat: add ark streaming adapter and local limits`。

### Task 4: 受控路由、身份提示词和独立 query

**Files:**

- Create: `app/chat/models.py`
- Create: `app/chat/prompts.py`
- Create: `tests/unit/test_prompts.py`
- Create: `tests/unit/test_ark.py`
- Create: `tests/unit/test_prompts.py`

- [ ] **Step 1:** 测试系统提示词包含广州日报与光明实验室身份、工具调用边界、独立 query 要求和单工具限制。
- [ ] **Step 2:** 测试 strict schema、结构化结果校验、空 query 使用最后一条用户消息兜底以及超长 query 被拒绝。
- [ ] **Step 3:** 运行测试，确认失败。
- [ ] **Step 4:** 实现集中管理的提示词、strict response format、内部路由类型和独立 query 校验。
- [ ] **Step 5:** 运行 `uv run pytest tests/unit/test_prompts.py tests/unit/test_ark.py -v`，期望全部通过。
- [ ] **Step 6:** 提交工具选择基础能力，提交信息建议为 `feat: add retrieval tool selection contract`。

### Task 5: 检索服务客户端和降级分类

**Files:**

- Create: `app/integrations/retrieval.py`
- Create: `tests/unit/test_retrieval.py`

- [ ] **Step 1:** 使用 respx 测试固定 `search_mode=hybrid`、请求范围来自 `rag`、Bearer key 转发、双 debug 开关以及 `chunks` 规范化。
- [ ] **Step 2:** 测试空结果、超时、连接失败、`400/401/403/429/5xx`、非 JSON 响应和缺失字段都返回可降级的 `ToolResult`，而不是直接抛到路由。
- [ ] **Step 3:** 运行测试，确认失败。
- [ ] **Step 4:** 实现异步 retrieval client、阶段超时、剩余 deadline 和脱敏错误摘要。
- [ ] **Step 5:** 运行 `uv run pytest tests/unit/test_retrieval.py -v`，期望全部通过。
- [ ] **Step 6:** 提交检索集成，提交信息建议为 `feat: integrate hybrid retrieval tool`。

### Task 6: 证据、引用和回答模式

**Files:**

- Create: `app/chat/evidence.py`
- Create: `tests/unit/test_evidence.py`

- [ ] **Step 1:** 测试 chunk 稳定编号、重复 chunk 去重、references 映射、非法引用不进入 references 以及无引用回答的标记。正文已经流出后不能回滚，因此非法引用文本只记录质量告警，不承诺在流末重写正文。
- [ ] **Step 2:** 测试四种 `answer_basis` 与固定免责声明完全对应。
- [ ] **Step 3:** 运行测试，确认失败。
- [ ] **Step 4:** 实现证据规范化和引用构造；保留回答实际使用的引用，不把所有候选 chunk 伪装成已引用来源。
- [ ] **Step 5:** 运行 `uv run pytest tests/unit/test_evidence.py -v`，期望全部通过。
- [ ] **Step 6:** 提交证据处理，提交信息建议为 `feat: add grounded answer evidence and citations`。

### Task 7: 编排器和 OpenAI SSE

**Files:**

- Create: `app/chat/events.py`
- Create: `app/chat/orchestrator.py`
- Create: `app/streaming/__init__.py`
- Create: `app/streaming/openai_sse.py`
- Modify: `app/api/routes.py`
- Create: `tests/integration/test_chat_direct.py`
- Create: `tests/integration/test_chat_retrieval.py`
- Create: `tests/integration/test_chat_fallback.py`
- Create: `tests/contract/test_openai_sse.py`

- [ ] **Step 1:** 测试无需检索时执行一次 strict 路由和一次流式生成，先输出固定未检索声明。
- [ ] **Step 2:** 测试需要检索时第一轮只产生结构化路由结果，检索后执行一次最终 Ark 调用，总调用数为二。
- [ ] **Step 3:** 测试检索成功、空结果以及包括 `401/403` 在内的检索错误分别产生正确 answer basis 和免责声明。
- [ ] **Step 4:** 测试状态 chunk、文本 chunk、终止 chunk、`[DONE]` 顺序以及全程相同的 `id/model/created`。
- [ ] **Step 5:** 测试 Ark 在 SSE 开始后失败时输出 `rag.error`，客户端断开会取消未完成任务。
- [ ] **Step 6:** 测试调用者传入 `system/developer/tool/function` 或非文本 content 时在 SSE 前被拒绝，确保服务身份和内部工具策略不能被覆盖。
- [ ] **Step 7:** 运行上述测试，确认失败。
- [ ] **Step 8:** 实现内部异步事件流、最多两次调用的编排器和 SSE adapter，再将路由接入编排器。
- [ ] **Step 9:** 运行 `uv run pytest tests/integration tests/contract -v`，期望全部通过。
- [ ] **Step 10:** 用 OpenAI Python SDK 指向本地 `base_url` 做一次兼容性 smoke test，确认标准正文 delta 可被 SDK 消费，`rag` 扩展可通过原始字段读取。
- [ ] **Step 11:** 提交主问答链路，提交信息建议为 `feat: implement streaming knowledge chat orchestration`。

### Task 8: Debug trace 和关闭态零额外链路

**Files:**

- Create: `app/platform/trace.py`
- Modify: `app/chat/orchestrator.py`
- Modify: `app/integrations/retrieval.py`
- Create: `tests/integration/test_debug_disabled.py`
- Create: `tests/integration/test_debug_enabled.py`

- [ ] **Step 1:** 测试 `DEBUG_ENABLED=false` 时不创建问答 trace，但仍按
  `request.rag.include_debug` 请求并透传 retrieval debug。
- [ ] **Step 2:** 测试问答 debug 与 retrieval 双开关分别开启时，终止事件只包含对应
  trace；全部关闭时响应不含 `rag.debug`。
- [ ] **Step 3:** 测试任何响应都不包含 API Key、系统提示词和隐藏推理。
- [ ] **Step 4:** 运行测试，确认失败。
- [ ] **Step 5:** 实现 enabled/no-op collector，并在编排边界记录事件，不在各模块中散落 debug 条件判断。
- [ ] **Step 6:** 运行 `uv run pytest tests/integration/test_debug_disabled.py tests/integration/test_debug_enabled.py -v`，期望全部通过。
- [ ] **Step 7:** 提交 debug 链路，提交信息建议为 `feat: add gated chat trace diagnostics`。

### Task 9: 独立开发面板

**Files:**

- Create: `dev/__init__.py`
- Create: `dev/server.py`
- Create: `dev/dataset.py`
- Create: `dev/static/index.html`
- Create: `dev/static/app.js`
- Create: `dev/static/styles.css`
- Create: `tests/unit/test_dataset.py`
- Create: `tests/integration/test_dev_proxy.py`

- [ ] **Step 1:** 测试 label 读取、gold doc 保留、噪声 doc 不重复且不等于 gold、固定 seed 可复现、manifest 路径错误可读。
- [ ] **Step 2:** 测试 dev proxy 从服务端配置注入 API Key 并原样转发 SSE，但响应和浏览器配置中不暴露 key。
- [ ] **Step 3:** 运行测试，确认失败。
- [ ] **Step 4:** 实现独立 dev FastAPI、manifest API、请求生成 API 和 SSE 代理。
- [ ] **Step 5:** 实现三栏静态页面、可编辑 JSON、状态展示、正文吐字、trace 时间线和原始事件折叠区。
- [ ] **Step 6:** 运行 `uv run pytest tests/unit/test_dataset.py tests/integration/test_dev_proxy.py -v`，期望全部通过。
- [ ] **Step 7:** 分别启动正式服务和 dev server，在桌面浏览器完成手工 smoke test；确认关闭 dev server 后正式服务不受影响。
- [ ] **Step 8:** 提交开发面板，提交信息建议为 `feat: add local knowledge chat debug panel`。

### Task 10: 文档、全量验证和上线前检查

**Files:**

- Create: `README.md`
- Modify: `.env.example`
- Modify: `docs/implement.md`（仅在实现与计划存在经确认的偏差时更新）

- [ ] **Step 1:** README 写明启动命令、环境变量、curl 示例、SSE 扩展、四种 answer basis、错误语义、两层独立 debug 和开发面板启动方式。
- [ ] **Step 2:** 运行 `uv run ruff check .` 和项目配置的格式检查，期望无错误。
- [ ] **Step 3:** 运行 `uv run pytest -q`，期望全部测试通过。
- [ ] **Step 4:** 使用 mock Ark/retrieval 跑并发 smoke test，确认 Ark 实际调用数不超过配置 RPM，单请求不超过两次 LLM 调用。
- [ ] **Step 5:** 配置 chat 入站 key（若启用）和检索兼容 Bearer，确认服务只在可信
  内网访问无服务级鉴权的 retrieval，完成真实检索联调。
- [ ] **Step 6:** 验证检索成功、无结果、检索 `401/403`、Ark 超时、上下文过大、debug 开关和客户端断开场景。
- [ ] **Step 7:** 删除或脱敏联调日志中的测试密钥，确认 `.env` 未进入版本控制。
- [ ] **Step 8:** 提交文档和上线配置，提交信息建议为 `docs: document knowledge chat deployment and protocol`。

## 8. 验收标准

### 8.1 核心问答

- [ ] 身份询问由 strict 路由判定为无需检索，第二次调用依据系统身份流式回答，总 LLM 调用数为 2。
- [ ] 闲聊和通识问题不调用检索，并由代码明确说明本次未检索知识库。
- [ ] 知识库问题产生 strict 结构化路由结果，只有独立 query 来自模型，权限范围来自请求代码注入。
- [ ] 检索成功后回答只使用提供的 chunks，正文引用编号能映射到 `rag.references`。
- [ ] 检索为空时继续生成通识回答，并明确说明知识库没有检索到结果。
- [ ] 检索任何错误（包括 `401/403`）时继续生成通识回答，`answer_basis=general_retrieval_error`。
- [ ] 任意请求最多调用 Ark 两次，任意轮次最多调用检索工具一次。

### 8.2 协议与错误

- [ ] 正文可被标准 OpenAI Chat Completions 流式客户端消费。
- [ ] 调用者只能发送文本 `user/assistant` 历史，不能通过 system 或 tool 消息覆盖服务身份和工具策略。
- [ ] 状态、references、chunks、retrieval 状态和 debug 位于顶层 `rag` 扩展，不污染最终回答文本。
- [ ] 所有成功流以终止 chunk 和 `[DONE]` 结束。
- [ ] 格式、鉴权、明显上下文大小和首个限流错误发生在 SSE 前，并返回正确 HTTP 状态。
- [ ] SSE 开始后的 Ark 错误以 `rag.error` 返回，HTTP 状态保持 200。
- [ ] `stream=false` 和 `include_usage=true` 返回明确的流前 `400`，不静默忽略。

### 8.3 安全与稳定性

- [ ] 入站鉴权开启时，未携带或携带错误知识库问答 API Key 的请求不会调用 Ark。
- [ ] 知识库问答入站 Bearer 不会转发给检索服务，密钥不出现在响应、trace 或浏览器配置中。
- [ ] Ark 限流按真实 LLM 调用次数生效，并发信号量能限制同时进行的 Ark 请求。
- [ ] 客户端断开后 Ark 和检索请求被取消。
- [ ] 问答 debug 关闭时不产生问答 trace；检索 debug 仅由请求 `rag.include_debug` 与
  检索端 `RAG_DEBUG_ENABLED` 双开关控制，并可独立透传。
- [ ] 正式应用未导入 `dev` 包，开发面板未启动时没有额外运行开销。

### 8.4 开发面板

- [ ] 能从 `query_labels.jsonl` 生成包含 gold doc 和可配置噪声 doc 的请求体。
- [ ] 请求 JSON 可编辑、格式化并发送。
- [ ] 中栏能够分开展示状态事件和最终正文 SSE。
- [ ] 右栏能够按时间回放意图/工具选择、检索和生成阶段，并查看 retrieval debug。
- [ ] 面板不使用 SQLite，不持久化请求模板或运行历史。

## 9. 上线前默认配置建议

以下值应进入 `.env.example`，真实值根据 Ark 配额和检索服务延迟调整：

```dotenv
APP_ENV=development
DEBUG_ENABLED=false
AUTH_ENABLED=false
KNOWLEDGE_CHAT_INBOUND_API_KEYS=

CHAT_MODEL_ALIAS=rag-knowledge-chat
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_API_KEY=
ARK_MODEL=

RAG_RETRIEVAL_SERVICE_URL=http://rag-retrieval-service:8120
RETRIEVAL_PATH=/rag/v1/retrieve

REQUEST_RPM_PER_CALLER=20
ARK_RPM_LIMIT=50
ARK_MAX_CONCURRENCY=4
RATE_LIMIT_WAIT_SECONDS=3

REQUEST_DEADLINE_SECONDS=90
ARK_FIRST_CALL_TIMEOUT_SECONDS=30
RETRIEVAL_TIMEOUT_SECONDS=35
ARK_FINAL_CALL_TIMEOUT_SECONDS=45

MAX_REQUEST_BYTES=262144
MAX_MESSAGES=80
MAX_MESSAGE_CHARS=120000
MAX_DOC_IDS=100
MAX_RETRIEVAL_QUERY_CHARS=2000
DEFAULT_TOP_K=8
MAX_TOP_K=20

DEV_CHAT_BASE_URL=http://127.0.0.1:8000
DEV_DATASET_MANIFEST_DIR=../rag-offline-eval/dataset/manifests
```

`ARK_RPM_LIMIT` 必须低于实际配额并保留安全余量。上线前用真实配额替换示例值，不能直接把示例当作生产结论。

## 10. 实现过程中不可改变的约束

- 不因“agent”命名引入 LangGraph、循环工具调用或第三次 LLM 调用。
- 不让 LLM 生成或覆盖 `user_id`、`kb_id`、`doc_ids`、API Key、检索模式和 debug 权限。
- 不把内部结构化路由结果暴露成需要前端执行的 OpenAI tool call。
- 不用提示词代替代码级免责声明、鉴权、限流和权限边界。
- 不接受调用者提供的 system/developer/tool/function 消息；服务系统提示词始终由后端独占管理。
- 不因检索 `401/403` 中断问答；它们进入明确的通识降级路径。
- 不为了未来 Redis、usage 持久化或多实例部署提前引入当前不需要的基础设施；只保持接口边界可替换。
- 若 Ark 的 strict response format 或 streaming 行为与 OpenAI SDK 存在差异，只修改 `app/integrations/ark.py` 和对应测试，不让差异扩散到业务编排。
