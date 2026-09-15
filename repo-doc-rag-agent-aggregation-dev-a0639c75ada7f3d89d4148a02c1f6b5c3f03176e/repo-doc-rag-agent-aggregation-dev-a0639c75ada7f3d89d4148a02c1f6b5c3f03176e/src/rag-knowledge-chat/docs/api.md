# 知识库问答 API

本文面向 `rag-knowledge-chat` 的下游调用方。服务提供 OpenAI Chat Completions 风格的
流式问答接口，并通过自定义 `rag` 字段接收知识库范围、临时文档范围和检索预算。

## 快速开始

- 生产默认地址：`http://<server>:8130`
- 问答接口：`POST /v1/chat/completions`
- 健康检查：`GET /health`
- 响应类型：`text/event-stream`
- 当前版本只支持 `stream=true`

最小请求只需要传入消息和知识库业务范围：

```bash
curl -N http://127.0.0.1:8130/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <KNOWLEDGE_CHAT_INBOUND_API_KEY>" \
  -d '{
    "messages": [
      {"role": "user", "content": "这几份材料的报销流程是什么？"}
    ],
    "rag": {
      "user_id": "user-1",
      "kb_id": "kb-1",
      "session_id": "session-1",
      "doc_ids": ["doc-1", "doc-2"],
      "temp_doc_ids": []
    }
  }'
```

当服务配置 `AUTH_ENABLED=false` 时不要求 `Authorization`；启用鉴权时必须携带有效的
Bearer key。入站 key 只用于知识库问答鉴权，不会被转发给检索服务。

## 请求体

### 顶层字段

| 字段 | 类型 | 必填 | 默认值 | 含义 |
|---|---|---:|---|---|
| `model` | string | 否 | `rag-knowledge-chat` | 模型别名。当前只接受该固定值。 |
| `messages` | array | 是 | 无 | 完整对话历史，按时间正序排列。1–80 条。 |
| `stream` | boolean | 否 | `true` | 当前版本必须为 `true`。 |
| `stream_options` | object | 否 | `{"include_usage": false}` | 流式选项。当前不支持 usage，`include_usage` 必须为 `false`。 |
| `temperature` | number | 否 | `0.2` | 回答生成温度，范围 0–2。 |
| `max_tokens` | integer | 否 | `1200` | 最终回答最大输出 token，范围 1–8192；不控制检索返回量。 |
| `rag` | object | 是 | 无 | 知识库范围、临时文档范围和检索预算。 |

请求模型采用严格校验：未声明的额外字段会被拒绝。当前消息只支持 `user` 和
`assistant` 两种角色，不接受 `system`、`developer`、`tool` 或 `function`。

### `messages` 与历史传递

服务无状态，不保存对话历史。调用方必须在每次请求中把本轮回答所需的完整历史放入
`messages`，顺序从最早到最新，并确保最后一条是当前 `user` 问题。

```json
{
  "messages": [
    {
      "role": "user",
      "content": "请介绍《差旅管理制度》的适用范围"
    },
    {
      "role": "assistant",
      "content": "该制度适用于……"
    },
    {
      "role": "user",
      "content": "那这篇文档中的报销流程是什么？"
    }
  ],
  "rag": {
    "user_id": "user-1",
    "kb_id": "kb-1",
    "session_id": "session-1",
    "doc_ids": ["travel-policy-doc-id"],
    "temp_doc_ids": []
  }
}
```

知识库问答会使用历史做指代消解和检索 query 改写。例如上例中的“这篇文档”会结合
前文改写为明确问题。若历史不足以唯一判断指代对象，服务应先要求用户澄清，而不是
猜测文档。跨文档比较会拆成多个可独立检索的事实问题；复合请求中的元信息问题不会被
主动丢弃。内部路由模型以 `query` 字符串数组返回这些子问题，并将数组直接传给检索服务；
指代消解仍必须由能够看到完整历史的知识库问答完成。

当前请求范围只有一篇文档时，“这篇文档”“这个文件”“该文档”“它”等文档指代固定解析为
当前范围内唯一文档。该请求范围规则优先于对话历史，即使历史曾讨论其他文档，也不会要求用户
重新说明文档名。检索服务会直接接受唯一文档并跳过 keyword 预筛和多文档路由。

调用方可以用 `rag.incremental_doc_ids` 声明本轮相对上一轮新增的正式或临时文档。两类 ID
可以混合放在同一个列表中；知识库问答会根据当前 `doc_ids` 和 `temp_doc_ids` 自动分区，批量
读取真实文件名，并在 query 改写阶段消解“新增的文档”“另外几篇”“刚上传的材料”等增量
指代。改写后的检索问题会使用确切文件名，不会继续向检索服务传递“新增文档”等相对表述。
服务本身不保存上一轮文档范围，因此增量关系由调用方负责计算。

`rag.session_id` 不是历史标识，也不会恢复消息。它是临时文档调用链的兼容关联字段；
检索服务按调用方给出的文档 ID 读取数据，不把该字段作为文档读取授权条件。

消息约束：

- 单条 `content` 长度为 1–32000 字符。
- 所有消息总长度不得超过服务的输入预检上限。
- 最后一条消息必须是 `user`。
- 调用方应只携带当前回答实际需要的历史，避免无限累积旧消息。

### `rag` 字段

| 字段 | 类型 | 必填 | 默认值 | 含义 |
|---|---|---:|---|---|
| `user_id` | string | 是 | 无 | 请求用户和限流身份，1–256 字符。必须由可信上游提供；不作为检索 SQL 的文档权限条件。 |
| `kb_id` | string | 是 | 无 | 兼容的知识库上下文字段，1–256 字符；不作为检索 SQL 的文档权限条件。 |
| `session_id` | string/null | 条件必填 | `null` | 临时文档调用链关联字段。`temp_doc_ids` 非空时必须传入，最长 256 字符；不作为检索 SQL 的文档权限条件。 |
| `doc_ids` | string[] | 否 | `[]` | 正式文档 ID，最多 100 个。 |
| `incremental_doc_ids` | string[] | 否 | `[]` | 本轮新增正式或临时文档 ID，最多 100 个；必须是当前 `doc_ids ∪ temp_doc_ids` 的子集。 |
| `temp_doc_ids` | string[] | 否 | `[]` | 临时文档 ID，最多 100 个。检索服务与正式文档一样按 ID 定位。 |
| `top_k` | integer/null | 否 | 动态计算 | 检索结果的兼容软上限，允许 4–20。省略时根据改写后的 query 和文档数量动态计算。 |
| `max_return_tokens` | integer/null | 否 | 动态计算 | 检索服务返回证据的 token 预算；下限为 128，上限由部署配置 `RAG_MAX_RETURN_TOKENS` 决定。省略时在 4096 到该配置上限之间动态计算。 |
| `include_debug` | boolean | 否 | `false` | 是否请求调试信息。生产调用通常保持 `false`。 |

`top_k` 和 `max_return_tokens` 是检索证据预算，不是最终回答长度。省略时服务会按 query
长度、问题类型和文档数量动态选择；动态计算与入站校验共同引用 `RAG_MAX_RETURN_TOKENS`。详细总结、全文解释等
问题仍可能需要较大的 `max_return_tokens`，调用方不应为了缩短响应而设置过小预算。

显式值超出部署允许范围时，知识库问答会在任何路由模型调用和 SSE 输出之前返回 HTTP 400
`invalid_request`。当前 doubao-2.0-lite 部署建议配置为 `180000`，为其 224K 最大输入预留
对话历史、系统提示词、回答指令及消息封装空间。检索服务仍执行自己的独立上限校验，作为内部契约防线。

正式文档和临时文档可以同时传入：

```json
{
  "rag": {
    "user_id": "user-1",
    "kb_id": "kb-1",
    "session_id": "upload-session-20260724",
    "doc_ids": ["permanent-doc-1"],
    "incremental_doc_ids": ["permanent-doc-1", "temporary-doc-1"],
    "temp_doc_ids": ["temporary-doc-1"]
  }
}
```

## 完整请求示例

```json
{
  "model": "rag-knowledge-chat",
  "messages": [
    {
      "role": "user",
      "content": "龙江交通和新乡化纤的 2023 年营业收入分别是多少？"
    }
  ],
  "stream": true,
  "stream_options": {
    "include_usage": false
  },
  "temperature": 0.2,
  "max_tokens": 1200,
  "rag": {
    "user_id": "eval-user",
    "kb_id": "eval-rag-kb",
    "session_id": null,
    "doc_ids": ["doc-a", "doc-b"],
    "incremental_doc_ids": ["doc-b"],
    "temp_doc_ids": [],
    "top_k": 8,
    "max_return_tokens": 16384,
    "include_debug": false
  }
}
```

## SSE 响应

服务生成的 OpenAPI 3.1 文档会在该接口的 HTTP 200 响应中声明
`text/event-stream -> ChatCompletionStreamChunk`。`ChatCompletionStreamChunk` 包含正文增量、
思考增量、状态、references、chunks、debug 和流内 error 字段，用于描述每一个
`data: <JSON>` 帧；结束标记 `data: [DONE]` 是 SSE 协议控制值，不属于 JSON schema。

FastAPI 导出的 OpenAPI 3.1 文档会在 `components.schemas` 中完整定义
`ChatCompletionStreamChunk`，并在 200 响应的 `text/event-stream.schema` 中引用该模型。
但 OpenAPI 3.1 没有“逐个流事件 schema”的标准字段；该能力直到 OpenAPI 3.2 才由
`itemSchema` 正式定义。Apifox 2.8.41-alpha.1 当前不会把导入的 OpenAPI 3.1
`text/event-stream.schema` 自动绑定到界面中的“ SSE 封装结构”，因此可能仍显示
`No schema defined`。这是 Apifox 的 OpenAPI 导入限制，不代表导出的组件模型为空，
也不影响接口运行。

需要在 Apifox 界面中查看字段树时，可编辑该接口的 200 `text/event-stream` 响应，
为 SSE `data` 字段选择 string 类型，并将其 Content Schema 手工关联到已导入的
`ChatCompletionStreamChunk` 数据模型。无法手工绑定时，以本节及下文列出的事件契约
作为下游开发依据。

每个事件格式为：

```text
data: <JSON>

```

流结束时固定发送：

```text
data: [DONE]

```

调用方必须逐个读取 SSE 事件，拼接所有 `choices[0].delta.content`，并同时处理
`rag.event`、最终 `rag` 元数据和流内 `rag.error`。

### 状态事件

状态事件不包含正文：

```json
{
  "id": "chatcmpl_xxx",
  "object": "chat.completion.chunk",
  "created": 1784188800,
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

常见 `stage`：

| 阶段 | 含义 |
|---|---|
| `route` | 判断是否需要检索，并结合历史改写问题。 |
| `retrieval` | 调用独立检索服务。 |
| `generation` | 根据证据或通识生成回答。 |
| `thinking` | 最终回答模型进入思考阶段；随后可能返回 reasoning 增量。 |

启用回答模型 thinking 时，随后会收到一个或多个 SSE chunk，使用
`choices[0].delta.reasoning_content` 承载模型的推理增量；最终答案仍使用
`choices[0].delta.content`。路由、query 改写等内部调用不会返回 reasoning。

```json
{
  "object": "chat.completion.chunk",
  "choices": [
    {
      "index": 0,
      "delta": {"reasoning_content": "先核对检索证据中的统计口径"},
      "finish_reason": null
    }
  ]
}
```

### 正文事件

```json
{
  "id": "chatcmpl_xxx",
  "object": "chat.completion.chunk",
  "created": 1784188800,
  "model": "rag-knowledge-chat",
  "choices": [
    {
      "index": 0,
      "delta": {"content": "根据制度要求"},
      "finish_reason": null
    }
  ]
}
```

### 完成事件

最后一个 JSON chunk 的 `finish_reason` 为 `stop`，并带有 RAG 元数据：

```json
{
  "id": "chatcmpl_xxx",
  "object": "chat.completion.chunk",
  "created": 1784188800,
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
    "references": [
      {
        "citation_index": 1,
        "doc_id": "doc-1",
        "doc_name": "差旅管理制度.pdf",
        "page_number": 3
      }
    ],
    "chunks": [
      {
        "citation_index": 1,
        "chunk_id": "doc-1:page:3",
        "document_id": "doc-1",
        "document_name": "差旅管理制度.pdf",
        "path": "page:3",
        "content": "报销申请应当……",
        "hint": "本 chunk 用于回答：报销流程是什么？",
        "source_type": "page"
      }
    ]
  }
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `answer_basis` | 回答依据，见下表。 |
| `retrieval.status` | `success`、`no_result`、`error` 或未调用时的 `not_called`。 |
| `retrieval.request_id` | 检索服务请求 ID；排障时与检索日志关联。 |
| `references` | 回答正文中实际引用的来源，不包含 chunk 正文。 |
| `chunks` | 本次提供给回答模型的候选证据原文。 |
| `citation_index` | 与回答正文中的 `[N]` 对应。 |
| `references[].doc_id` | 引用对应的文档 ID；集合级系统引用为 `null`。 |
| `references[].doc_name` | 引用对应的文档名；集合级系统引用固定为 `system`。 |
| `references[].page_number` | 引用对应的一基页码；集合级系统引用固定为 `1`，其他非 page 引用为 `null`。 |
| `debug` | 双方 debug 开关允许时才存在。 |

`references` 是面向调用方展示的稳定精简契约，只包含上述四个字段。`chunk_id`、
原文、hint 和检索内部元信息不会在 reference 中透传；需要开发调试或展开原文时使用同一完成
事件中的 `chunks`。

集合级系统 chunk 使用固定展示位置：`doc_id=null`、`doc_name="system"`、`page_number=1`。

`answer_basis`：

| 值 | 含义 |
|---|---|
| `knowledge_base` | 使用知识库证据回答。 |
| `general_no_retrieval` | 问题不需要知识库，使用模型通识回答。 |
| `general_no_result` | 已检索但没有结果；正文会明确提示后再使用通识回答。 |
| `general_retrieval_error` | 检索失败；正文会明确提示后再使用通识回答。 |

调用方不应假设每次请求都会调用检索服务。身份、闲聊或一般知识问题可能直接生成回答；
无法消解指代时也可能直接要求用户补充信息。

## 错误处理

### SSE 建立前

SSE 建立前的错误使用 HTTP 状态码和 JSON：

```json
{
  "error": {
    "code": "invalid_request",
    "message": "request validation failed",
    "retryable": false,
    "request_id": "req_xxx",
    "details": {
      "validation_errors": []
    }
  }
}
```

| HTTP 状态 | 常见场景 |
|---:|---|
| 400 | 字段校验失败、最后一条消息不是 user、`stream=false`、模型名不支持。 |
| 401 | 入站鉴权已开启，但 Bearer key 缺失或无效。 |
| 413 | 请求体超过 256 KiB。 |
| 429 | 用户请求 RPM 或 Ark 调用预算超限。 |

所有 HTTP 响应都包含 `X-Request-Id`。调用方可以主动传入该请求头；否则服务生成一个。

### SSE 建立后

流建立后无法再修改 HTTP 状态码，因此模型、检索或总 deadline 错误通过
`rag.error` 返回，随后仍发送 `[DONE]`：

```json
{
  "choices": [
    {"index": 0, "delta": {}, "finish_reason": null}
  ],
  "rag": {
    "error": {
      "code": "request_deadline_exceeded",
      "message": "问答请求超过总处理时限",
      "retryable": true,
      "request_id": "req_xxx"
    }
  }
}
```

因此调用方必须同时检查 HTTP 状态和所有 SSE 事件中的 `rag.error`，不能把 HTTP 200
直接视为问答成功。

## Debug

生产请求建议保持 `rag.include_debug=false`。

- 知识库问答自身 trace 需要服务端 `DEBUG_ENABLED=true`。
- 检索 trace 需要请求 `rag.include_debug=true`，且检索服务端
  `RAG_DEBUG_ENABLED=true`。
- debug 可能包含改写后的检索 query、节点耗时、候选摘要、LLM 结构化输出和工具事件，
  响应体可能明显增大。
- debug 不应包含 API Key 或模型隐藏 reasoning。

## 调用方检查清单

- 每轮都传入完成当前问答所需的 `messages` 历史，最后一条必须是 `user`。
- 临时文档请求同时传入正确的 `session_id` 和 `temp_doc_ids`。
- 不把 `session_id` 当作服务端聊天记忆。
- 按 SSE 协议逐段读取，不等待连接关闭后再一次性解析 JSON。
- 拼接正文的同时处理状态事件、流内错误、完成元数据和 `[DONE]`。
- 使用 `X-Request-Id`、`retrieval.request_id` 定位跨服务问题。
- 生产关闭 debug，并为长文档检索设置足够的客户端和网关读取超时。
