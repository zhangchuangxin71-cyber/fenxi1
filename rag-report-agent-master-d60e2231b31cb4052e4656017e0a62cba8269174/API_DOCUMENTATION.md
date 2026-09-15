# RAG Agent Codex API 文档

本文档面向前端和联调同学，描述 `/opt/example-user/rag_agent_codex` 当前提供的 HTTP API。

当前只有 2 个接口：

1. `GET /health`
2. `POST /agent/v1/chat/completions`

其中核心接口是 `POST /agent/v1/chat/completions`，响应为 SSE 流。

## 1. 服务基础信息

默认本地地址：

```text
http://127.0.0.1:8100
```

Docker 或服务器部署时，以实际域名/IP 和端口为准。

建议请求头：

```http
Content-Type: application/json
Accept: text/event-stream
```

重要说明：

- `chat/completions` 当前始终以 SSE 形式返回。
- SSE 中每个 `event` 的 `data` 都是 JSON 字符串。
- SSE 中可能出现 `: ping - ...` 形式的心跳注释行，前端应忽略。
- `text_delta` 用于左侧普通对话区域。
- `outline_delta` / `outline_complete` 用于报告大纲区域。
- `report_text_delta` 用于右侧报告正文区域。
- 同一个请求可能同时出现 `text_delta` 和 `report_text_delta`。

## 2. 健康检查接口

### 2.1 请求

```http
GET /health
```

### 2.2 响应

```json
{
  "status": "ok"
}
```

## 3. 聊天与报告接口

### 3.1 请求

```http
POST /agent/v1/chat/completions
```

### 3.2 请求体字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---:|---:|---:|---|
| `session_id` | string | 是 | 无 | 会话 ID，同一个会话建议保持一致 |
| `kb_id` | string | 是 | 无 | 知识库 ID |
| `query` | string | 是 | 无 | 用户本轮输入 |
| `history` | array | 否 | `[]` | 历史消息，元素为 `{role, content}` |
| `stream` | boolean | 否 | `true` | 当前接口按 SSE 返回，建议固定传 `true` |
| `doc_ids` | string[] | 否 | `[]` | 候选正式文档 ID |
| `temp_doc_ids` | string[] | 否 | `[]` | 候选临时文档 ID |
| `retrieval_config` | object | 否 | 见下方 | 检索配置 |
| `generation_config` | object | 否 | 见下方 | 生成配置 |
| `report_context` | object | 否 | 见下方 | 报告上下文 |

### 3.3 `history` 字段

```json
[
  {
    "role": "user",
    "content": "马飞是谁"
  },
  {
    "role": "assistant",
    "content": "马飞是光明实验室媒体智能团队负责人。"
  }
]
```

`role` 只支持：

- `user`
- `assistant`

### 3.4 `retrieval_config` 字段

```json
{
  "top_k": 5,
  "search_mode": "hybrid"
}
```

| 字段 | 类型 | 范围 | 默认值 | 说明 |
|---|---:|---:|---:|---|
| `top_k` | integer | `1` 到 `20` | `5` | 每次检索召回数量 |
| `search_mode` | string | `hybrid` / `keyword` / `semantic` | `hybrid` | 检索模式 |

### 3.5 `generation_config` 字段

```json
{
  "temperature": 0.2,
  "max_tokens": 4096
}
```

| 字段 | 类型 | 范围 | 默认值 | 说明 |
|---|---:|---:|---:|---|
| `temperature` | number | `0.0` 到 `1.0` | `0.7` | 生成随机性 |
| `max_tokens` | integer | `1` 到 `32768` | `4096` | 最大输出 token 数 |

建议：

- 知识问答可用 `2048` 到 `4096`
- 完整报告生成建议 `8192` 或更高，避免正文被截断

### 3.6 `report_context` 字段

```json
{
  "current_outline": null,
  "outline_confirmed": false,
  "current_report": null
}
```

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `current_outline` | object/null | `null` | 当前报告大纲 |
| `outline_confirmed` | boolean | `false` | 大纲是否已被用户确认 |
| `current_report` | string/null | `null` | 当前已有报告全文，用于整体编辑 |

`current_outline` 标准格式：

```json
{
  "title": "《马飞研究员个人介绍报告》",
  "sections": [
    {
      "index": 1,
      "title": "一、报告摘要",
      "subsections": []
    },
    {
      "index": 2,
      "title": "二、个人基本概况",
      "subsections": [
        {
          "index": "2.1",
          "title": "基础身份信息"
        }
      ]
    }
  ]
}
```

也兼容前端多包一层的格式：

```json
{
  "outline": {
    "title": "《马飞研究员个人介绍报告》",
    "sections": []
  }
}
```

## 4. SSE 响应事件总览

### 4.1 SSE 格式

SSE 返回不是单个 JSON，而是多个事件块：

```text
event: text_delta
data: {"delta":"你好"}
```

前端处理方式：

1. 读取 `event` 名称。
2. 将 `data` 按 JSON 解析。
3. 根据 `event` 决定渲染位置和动作。

### 4.2 事件列表

| 事件 | data 示例 | 前端建议动作 |
|---|---|---|
| `stream_start` | `{"conversation_id":"conv_x","session_id":"sess_x","created_at":"2026-05-19T01:00:00+00:00"}` | 初始化本轮响应 |
| `step` | `{"step":"thinking","message":"正在分析您的问题..."}` | 展示阶段状态 |
| `thinking_delta` | `{"delta":"2026-05-19T09:00:00，思考：正在分析问题。\n"}` | 展示思考过程，可追加 |
| `intent` | `[{"intent_type":"knowledge_qa","confidence":0.9}]` | 记录本轮模式，决定 UI 布局 |
| `text_delta` | `{"delta":"马飞是..."}` | 追加到左侧普通对话 |
| `outline_delta` | `{"delta":"# 报告大纲\n"}` | 追加到大纲区域 |
| `outline_complete` | `{"outline":{"title":"...","sections":[]}}` | 保存结构化大纲 |
| `report_start` | `{"message_id":""}` | 打开右侧报告窗口，准备接收正文 |
| `report_text_delta` | `{"delta":"# 报告标题\n"}` | 追加到右侧报告正文 |
| `references` | `{"references":[{"doc_id":"...","doc_name":"...","chunk_ids":["..."]}]}` | 展示引用来源 |
| `report_end` | `{"message_id":"","status":"completed"}` | 标记右侧报告生成完成 |
| `stream_end` | `{"usage":{},"finish_reason":"complete","duration_ms":1234}` | 标记本轮请求结束 |
| `error` | `{"code":500,"type":"RuntimeError","message":"...","fatal":true}` | 展示错误并结束本轮 |

### 4.3 `intent_type` 枚举

| intent_type | 含义 | 常见输出事件 |
|---|---|---|
| `chitchat` | 闲聊 | `text_delta` |
| `knowledge_qa` | 知识问答 | `text_delta` + `references` |
| `report_outline` | 报告大纲生成 | `outline_delta` + `outline_complete` + `references` |
| `outline_modify` | 大纲修改 | `outline_delta` + `outline_complete` |
| `report_write` | 根据已确认大纲撰写报告正文 | `report_start` + `report_text_delta` + `references` + `report_end` |
| `report_edit` | 基于已有报告整体编辑 | `report_start` + `report_text_delta` + `references` + `report_end` |

`intent` 事件当前通常返回数组，例如：

```json
[
  {
    "intent_type": "knowledge_qa",
    "confidence": 0.9
  },
  {
    "intent_type": "report_write",
    "confidence": 0.95
  }
]
```

## 5. 前端渲染建议

### 5.1 左侧聊天区

追加这些事件：

- `text_delta`

### 5.2 思考过程区

追加这些事件：

- `step`
- `thinking_delta`

### 5.3 报告大纲区

追加这些事件：

- `outline_delta`

保存这些事件：

- `outline_complete`

### 5.4 右侧报告正文区

遇到 `report_start` 时打开或初始化右侧报告窗口。

追加这些事件：

- `report_text_delta`

遇到 `report_end` 时标记完成。

### 5.5 引用区

每次收到 `references` 都可以追加到当前模块引用中。

注意：复合意图下可能出现多次 `references`：

- 第一次可能属于左侧知识问答。
- 第二次可能属于右侧报告正文。

前端可按最近一个内容流类型归属：

- 如果前面最近出现的是 `text_delta`，归属左侧回答。
- 如果前面最近出现的是 `outline_delta`，归属大纲。
- 如果前面最近出现的是 `report_text_delta`，归属报告正文。

## 6. 示例 1：闲聊

### 6.1 请求 JSON

```json
{
  "session_id": "sess-chat-001",
  "kb_id": "kb-demo",
  "query": "你好",
  "history": [],
  "stream": true,
  "doc_ids": [],
  "temp_doc_ids": [],
  "retrieval_config": {
    "top_k": 5,
    "search_mode": "hybrid"
  },
  "generation_config": {
    "temperature": 0.2,
    "max_tokens": 1024
  },
  "report_context": {
    "current_outline": null,
    "outline_confirmed": false,
    "current_report": null
  }
}
```

### 6.2 SSE 响应流程示例

```text
event: stream_start
data: {"conversation_id":"conv_001","session_id":"sess-chat-001","created_at":"2026-05-19T01:00:00+00:00"}

event: step
data: {"step":"thinking","message":"正在分析您的问题..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:00:00，思考：用户当前问题是“你好”，先根据报告上下文、历史和问题措辞判断应该进入知识问答、报告大纲、报告撰写还是闲聊流程。\n"}

event: intent
data: [{"intent_type":"chitchat","confidence":0.95}]

event: thinking_delta
data: {"delta":"2026-05-19T09:00:00，思考：意图判断完成，当前将进入「闲聊」流程。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:00:00，思考：当前意图是闲聊，不需要检索知识库，直接生成简短回复。\n"}

event: text_delta
data: {"delta":"你好，我是广州日报粤传媒和光明实验室联合研发的智能体。"}

event: stream_end
data: {"usage":{"prompt_tokens":120,"completion_tokens":20,"total_tokens":140},"finish_reason":"complete","duration_ms":1200}
```

### 6.3 前端动作

- `intent=chitchat`：保持普通聊天模式。
- 只渲染 `text_delta` 到左侧聊天区。
- 不打开右侧报告窗口。

## 7. 示例 2：知识问答

### 7.1 请求 JSON

```json
{
  "session_id": "sess-qa-001",
  "kb_id": "kb-demo",
  "query": "马飞是谁",
  "history": [],
  "stream": true,
  "doc_ids": [
    "c4a32e97-2733-5dd9-8f1e-49deb2175251",
    "8b739733-dca5-5696-8c39-f997dfc5cd8b"
  ],
  "temp_doc_ids": [],
  "retrieval_config": {
    "top_k": 5,
    "search_mode": "hybrid"
  },
  "generation_config": {
    "temperature": 0.2,
    "max_tokens": 4096
  },
  "report_context": {
    "current_outline": null,
    "outline_confirmed": false,
    "current_report": null
  }
}
```

### 7.2 SSE 响应流程示例

```text
event: stream_start
data: {"conversation_id":"conv_002","session_id":"sess-qa-001","created_at":"2026-05-19T01:01:00+00:00"}

event: step
data: {"step":"thinking","message":"正在分析您的问题..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:01:00，思考：用户当前问题是“马飞是谁”，先根据报告上下文、历史和问题措辞判断应该进入知识问答、报告大纲、报告撰写还是闲聊流程。\n"}

event: intent
data: [{"intent_type":"knowledge_qa","confidence":0.9}]

event: thinking_delta
data: {"delta":"2026-05-19T09:01:01，思考：意图判断完成，当前将进入「知识问答」流程。\n"}

event: step
data: {"step":"thinking","message":"正在拆解和改写问题..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:01:01，思考：先把用户问题整理成适合检索的子问题，避免多个问题互相干扰。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:01:02，思考：当前问题可作为一个检索问题处理。\n"}

event: step
data: {"step":"thinking","message":"正在确定相关文档..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:01:02，思考：当前有 2 篇候选文档，需要根据问题“马飞是谁”筛选最相关的参考资料。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:01:03，思考：已确定最相关的参考文档，接下来进入资料检索。\n"}

event: step
data: {"step":"retrieving","message":"正在检索知识库..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:01:03，思考：已经确定需要查询知识库，接下来查找与问题最相关的资料内容。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:01:04，思考：资料检索完成，找到 5 条可参考内容。\n"}

event: step
data: {"step":"generating","message":"正在生成回答..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:01:04，思考：已经拿到相关资料，来源文档包括《迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf》，接下来基于这些内容生成回答。\n"}

event: text_delta
data: {"delta":"马飞是光明实验室媒体智能团队负责人，职称为研究员、博士。"}

event: text_delta
data: {"delta":"他曾于 2022~2024 年就职于华为，2024 年入职光明实验室，核心研究方向为以人为中心的世界模型构建。"}

event: references
data: {"references":[{"doc_id":"c4a32e97-2733-5dd9-8f1e-49deb2175251","doc_name":"迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf","chunk_ids":["c4a32e97-2733-5dd9-8f1e-49deb2175251:page:4","c4a32e97-2733-5dd9-8f1e-49deb2175251:node:0003"]}]}

event: stream_end
data: {"usage":{"prompt_tokens":1500,"completion_tokens":300,"total_tokens":1800},"finish_reason":"complete","duration_ms":8000}
```

### 7.3 前端动作

- `intent=knowledge_qa`：普通知识问答模式。
- `text_delta` 渲染到左侧聊天区。
- `references` 归属左侧回答。
- 不打开右侧报告窗口。

## 8. 示例 3：闲聊 + 知识问答

### 8.1 请求 JSON

```json
{
  "session_id": "sess-mix-qa-001",
  "kb_id": "kb-demo",
  "query": "你是谁？顺便介绍一下马飞",
  "history": [],
  "stream": true,
  "doc_ids": [
    "c4a32e97-2733-5dd9-8f1e-49deb2175251"
  ],
  "temp_doc_ids": [],
  "retrieval_config": {
    "top_k": 5,
    "search_mode": "hybrid"
  },
  "generation_config": {
    "temperature": 0.2,
    "max_tokens": 4096
  },
  "report_context": {
    "current_outline": null,
    "outline_confirmed": false,
    "current_report": null
  }
}
```

### 8.2 SSE 响应流程示例

```text
event: stream_start
data: {"conversation_id":"conv_003","session_id":"sess-mix-qa-001","created_at":"2026-05-19T01:02:00+00:00"}

event: step
data: {"step":"thinking","message":"正在分析您的问题..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:02:00，思考：用户当前问题是“你是谁？顺便介绍一下马飞”，先根据报告上下文、历史和问题措辞判断应该进入知识问答、报告大纲、报告撰写还是闲聊流程。\n"}

event: intent
data: [{"intent_type":"chitchat","confidence":0.8},{"intent_type":"knowledge_qa","confidence":0.9}]

event: thinking_delta
data: {"delta":"2026-05-19T09:02:01，思考：意图判断完成，当前将进入「闲聊」、「知识问答」流程。\n"}

event: step
data: {"step":"thinking","message":"正在拆解和改写问题..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:02:01，思考：先把用户问题整理成适合检索的子问题，避免多个问题互相干扰。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:02:02，思考：当前问题可作为一个检索问题处理。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:02:02，思考：当前只有 1 篇候选文档，将直接围绕这篇文档查找答案。\n"}

event: step
data: {"step":"retrieving","message":"正在检索知识库..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:02:02，思考：已经确定需要查询知识库，接下来查找与问题最相关的资料内容。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:02:03，思考：资料检索完成，找到 5 条可参考内容。\n"}

event: step
data: {"step":"generating","message":"正在生成回答..."}

event: text_delta
data: {"delta":"我是广州日报粤传媒和光明实验室联合研发的智能体，可以回答你有关知识库文档内的问题，还可以帮助你进行报告生成哦。\n\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:02:03，思考：已经拿到相关资料，来源文档包括《迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf》，接下来基于这些内容生成回答。\n"}

event: text_delta
data: {"delta":"马飞是光明实验室媒体智能团队负责人，研究员、博士。"}

event: references
data: {"references":[{"doc_id":"c4a32e97-2733-5dd9-8f1e-49deb2175251","doc_name":"迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf","chunk_ids":["c4a32e97-2733-5dd9-8f1e-49deb2175251:page:4"]}]}

event: stream_end
data: {"usage":{"prompt_tokens":1600,"completion_tokens":360,"total_tokens":1960},"finish_reason":"complete","duration_ms":9000}
```

### 8.3 前端动作

- `intent` 同时包含 `chitchat` 和 `knowledge_qa`。
- 仍然只使用左侧聊天区。
- 第一个 `text_delta` 是身份介绍，后续 `text_delta` 是知识问答内容。
- `references` 归属左侧回答。

## 9. 示例 4：闲聊 + 知识问答 + 报告生成

本例是“首次要求生成报告”的情况，系统会先生成报告大纲，因此报告相关事件是 `outline_delta` 和 `outline_complete`。

### 9.1 请求 JSON

```json
{
  "session_id": "sess-mix-outline-001",
  "kb_id": "kb-demo",
  "query": "你是谁？介绍一下马飞，并帮我生成一份他的介绍报告",
  "history": [],
  "stream": true,
  "doc_ids": [
    "c4a32e97-2733-5dd9-8f1e-49deb2175251"
  ],
  "temp_doc_ids": [],
  "retrieval_config": {
    "top_k": 5,
    "search_mode": "hybrid"
  },
  "generation_config": {
    "temperature": 0.2,
    "max_tokens": 4096
  },
  "report_context": {
    "current_outline": null,
    "outline_confirmed": false,
    "current_report": null
  }
}
```

### 9.2 SSE 响应流程示例

```text
event: stream_start
data: {"conversation_id":"conv_004","session_id":"sess-mix-outline-001","created_at":"2026-05-19T01:03:00+00:00"}

event: step
data: {"step":"thinking","message":"正在分析您的问题..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:03:00，思考：用户当前问题是“你是谁？介绍一下马飞，并帮我生成一份他的介绍报告”，先根据报告上下文、历史和问题措辞判断应该进入知识问答、报告大纲、报告撰写还是闲聊流程。\n"}

event: intent
data: [{"intent_type":"chitchat","confidence":0.8},{"intent_type":"knowledge_qa","confidence":0.9},{"intent_type":"report_outline","confidence":0.9}]

event: thinking_delta
data: {"delta":"2026-05-19T09:03:01，思考：意图判断完成，当前将进入「闲聊」、「知识问答」、「报告大纲生成」流程。\n"}

event: step
data: {"step":"thinking","message":"正在拆解和改写问题..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:03:01，思考：先把用户问题整理成适合检索的子问题，避免多个问题互相干扰。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:03:02，思考：已拆解出 2 个子问题，将分别查找相关资料后再综合回答。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:03:02，思考：当前只有 1 篇候选文档，将直接围绕这篇文档查找答案。\n"}

event: step
data: {"step":"retrieving","message":"正在检索知识库..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:03:02，思考：已经确定需要查询知识库，接下来查找与问题最相关的资料内容。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:03:03，思考：资料检索完成，找到 5 条可参考内容。\n"}

event: step
data: {"step":"generating","message":"正在生成回答..."}

event: text_delta
data: {"delta":"我是广州日报粤传媒和光明实验室联合研发的智能体，可以回答你有关知识库文档内的问题，还可以帮助你进行报告生成哦。\n\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:03:03，思考：已经拿到相关资料，来源文档包括《迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf》，接下来基于这些内容生成回答。\n"}

event: text_delta
data: {"delta":"马飞是光明实验室媒体智能团队负责人，研究员、博士，核心研究方向为以人为中心的世界模型构建。"}

event: references
data: {"references":[{"doc_id":"c4a32e97-2733-5dd9-8f1e-49deb2175251","doc_name":"迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf","chunk_ids":["c4a32e97-2733-5dd9-8f1e-49deb2175251:page:4"]}]}

event: thinking_delta
data: {"delta":"2026-05-19T09:03:05，思考：当前任务是生成报告大纲，已获得 5 条参考内容，接下来组织章节结构。\n"}

event: outline_delta
data: {"delta":"# 《马飞研究员个人介绍报告》\n\n"}

event: outline_delta
data: {"delta":"## 一、报告摘要\n## 二、个人基本概况\n## 三、核心研究方向\n"}

event: outline_complete
data: {"outline":{"title":"《马飞研究员个人介绍报告》","sections":[{"index":1,"title":"一、报告摘要","subsections":[]},{"index":2,"title":"二、个人基本概况","subsections":[{"index":"2.1","title":"基础身份信息"}]}]}}

event: references
data: {"references":[{"doc_id":"c4a32e97-2733-5dd9-8f1e-49deb2175251","doc_name":"迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf","chunk_ids":["c4a32e97-2733-5dd9-8f1e-49deb2175251:page:4","c4a32e97-2733-5dd9-8f1e-49deb2175251:node:0008"]}]}

event: stream_end
data: {"usage":{"prompt_tokens":2500,"completion_tokens":900,"total_tokens":3400},"finish_reason":"complete","duration_ms":18000}
```

### 9.3 前端动作

- `intent` 同时包含 `chitchat`、`knowledge_qa`、`report_outline`。
- `text_delta` 渲染到左侧聊天区。
- 收到 `outline_delta` 后打开大纲区域或右侧笔记区域。
- 收到 `outline_complete` 后保存结构化大纲，供用户确认或修改。
- 本模式不会出现 `report_start` 和 `report_text_delta`，因为当前阶段是大纲生成，不是正文撰写。

## 10. 示例 5：闲聊 + 报告生成

本例是“已有大纲且已确认，用户要求开始写正文，同时问身份”的情况。报告正文通过 `report_text_delta` 返回。

### 10.1 请求 JSON

```json
{
  "session_id": "sess-chat-report-001",
  "kb_id": "kb-demo",
  "query": "你是谁？OK，开始写吧",
  "history": [],
  "stream": true,
  "doc_ids": [
    "c4a32e97-2733-5dd9-8f1e-49deb2175251"
  ],
  "temp_doc_ids": [],
  "retrieval_config": {
    "top_k": 5,
    "search_mode": "hybrid"
  },
  "generation_config": {
    "temperature": 0.2,
    "max_tokens": 8192
  },
  "report_context": {
    "outline_confirmed": true,
    "current_outline": {
      "title": "《马飞研究员个人介绍报告》",
      "sections": [
        {
          "index": 1,
          "title": "一、报告摘要",
          "subsections": []
        },
        {
          "index": 2,
          "title": "二、个人基本概况",
          "subsections": [
            {
              "index": "2.1",
              "title": "基础身份信息"
            },
            {
              "index": "2.2",
              "title": "教育背景"
            }
          ]
        }
      ]
    },
    "current_report": null
  }
}
```

### 10.2 SSE 响应流程示例

```text
event: stream_start
data: {"conversation_id":"conv_005","session_id":"sess-chat-report-001","created_at":"2026-05-19T01:04:00+00:00"}

event: step
data: {"step":"thinking","message":"正在分析您的问题..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:04:00，思考：用户当前问题是“你是谁？OK，开始写吧”，先根据报告上下文、历史和问题措辞判断应该进入知识问答、报告大纲、报告撰写还是闲聊流程。\n"}

event: intent
data: [{"intent_type":"chitchat","confidence":0.8},{"intent_type":"report_write","confidence":0.95}]

event: thinking_delta
data: {"delta":"2026-05-19T09:04:01，思考：意图判断完成，当前将进入「闲聊」、「报告撰写」流程。\n"}

event: step
data: {"step":"thinking","message":"正在拆解和改写问题..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:04:01，思考：先把用户问题整理成适合检索的子问题，避免多个问题互相干扰。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:04:02，思考：当前问题可作为一个检索问题处理。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:04:02，思考：当前只有 1 篇候选文档，将直接围绕这篇文档查找答案。\n"}

event: step
data: {"step":"retrieving","message":"正在检索知识库..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:04:02，思考：已经确定需要查询知识库，接下来查找与问题最相关的资料内容。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:04:03，思考：资料检索完成，找到 5 条可参考内容。\n"}

event: step
data: {"step":"generating","message":"正在生成回答..."}

event: text_delta
data: {"delta":"我是广州日报粤传媒和光明实验室联合研发的智能体，可以回答你有关知识库文档内的问题，还可以帮助你进行报告生成哦。\n\n"}

event: text_delta
data: {"delta":"好的，我将根据已确认的大纲继续生成报告正文。"}

event: references
data: {"references":[{"doc_id":"c4a32e97-2733-5dd9-8f1e-49deb2175251","doc_name":"迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf","chunk_ids":["c4a32e97-2733-5dd9-8f1e-49deb2175251:page:4"]}]}

event: step
data: {"step":"report_writing","message":"正在撰写报告..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:04:05，思考：大纲已确认，当前进入整篇报告生成流程，将基于大纲和参考资料生成正文。\n"}

event: report_start
data: {"message_id":""}

event: report_text_delta
data: {"delta":"# 《马飞研究员个人介绍报告》\n\n## 一、报告摘要\n"}

event: report_text_delta
data: {"delta":"本报告围绕马飞研究员的个人基本概况、核心研究方向、科研成果和团队建设情况展开。"}

event: references
data: {"references":[{"doc_id":"c4a32e97-2733-5dd9-8f1e-49deb2175251","doc_name":"迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf","chunk_ids":["c4a32e97-2733-5dd9-8f1e-49deb2175251:page:4","c4a32e97-2733-5dd9-8f1e-49deb2175251:node:0003"]}]}

event: report_end
data: {"message_id":"","status":"completed"}

event: stream_end
data: {"usage":{"prompt_tokens":3000,"completion_tokens":1800,"total_tokens":4800},"finish_reason":"complete","duration_ms":30000}
```

### 10.3 前端动作

- `intent` 包含 `chitchat` 和 `report_write`。
- `text_delta` 渲染到左侧聊天区。
- 收到 `report_start` 后打开右侧报告窗口。
- `report_text_delta` 渲染到右侧报告正文。
- `report_end` 标记右侧报告完成。

## 11. 示例 6：知识问答 + 报告生成

本例是“已确认大纲，用户要求开始写报告，同时顺便问一个知识库问题”。这是前端最需要区分 `text_delta` 和 `report_text_delta` 的模式。

### 11.1 请求 JSON

```json
{
  "session_id": "sess-qa-report-001",
  "kb_id": "kb-demo",
  "query": "OK，开始写吧。顺便回答一下徐洪波的过往经历",
  "history": [
    {
      "role": "user",
      "content": "帮我生成一份马飞研究员个人介绍报告"
    },
    {
      "role": "assistant",
      "content": "已生成报告大纲，请确认。"
    }
  ],
  "stream": true,
  "doc_ids": [
    "c4a32e97-2733-5dd9-8f1e-49deb2175251",
    "8b739733-dca5-5696-8c39-f997dfc5cd8b"
  ],
  "temp_doc_ids": [],
  "retrieval_config": {
    "top_k": 5,
    "search_mode": "hybrid"
  },
  "generation_config": {
    "temperature": 0.2,
    "max_tokens": 8192
  },
  "report_context": {
    "outline_confirmed": true,
    "current_outline": {
      "title": "《马飞研究员个人介绍报告》",
      "sections": [
        {
          "index": 1,
          "title": "一、报告摘要",
          "subsections": []
        },
        {
          "index": 2,
          "title": "二、个人基本概况",
          "subsections": [
            {
              "index": "2.1",
              "title": "基础身份信息"
            },
            {
              "index": "2.2",
              "title": "教育背景"
            },
            {
              "index": "2.3",
              "title": "工作经历"
            }
          ]
        },
        {
          "index": 3,
          "title": "三、核心研究方向",
          "subsections": [
            {
              "index": "3.1",
              "title": "总体研究方向：以人为中心的世界模型构建"
            },
            {
              "index": "3.2",
              "title": "细分研究方向"
            }
          ]
        }
      ]
    },
    "current_report": null
  }
}
```

### 11.2 SSE 响应流程示例

```text
event: stream_start
data: {"conversation_id":"conv_006","session_id":"sess-qa-report-001","created_at":"2026-05-19T01:05:00+00:00"}

event: step
data: {"step":"thinking","message":"正在分析您的问题..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:05:00，思考：用户当前问题是“OK，开始写吧。顺便回答一下徐洪波的过往经历”，先根据报告上下文、历史和问题措辞判断应该进入知识问答、报告大纲、报告撰写还是闲聊流程。\n"}

event: intent
data: [{"intent_type":"knowledge_qa","confidence":0.9},{"intent_type":"report_write","confidence":0.95}]

event: thinking_delta
data: {"delta":"2026-05-19T09:05:01，思考：意图判断完成，当前将进入「知识问答」、「报告撰写」流程。\n"}

event: step
data: {"step":"thinking","message":"正在拆解和改写问题..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:05:01，思考：先把用户问题整理成适合检索的子问题，避免多个问题互相干扰。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:05:02，思考：当前问题可作为一个检索问题处理。\n"}

event: step
data: {"step":"thinking","message":"正在确定相关文档..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:05:02，思考：当前有 2 篇候选文档，需要根据问题“徐洪波的过往经历是什么”筛选最相关的参考资料。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:05:03，思考：已确定最相关的参考文档，接下来进入资料检索。\n"}

event: step
data: {"step":"retrieving","message":"正在检索知识库..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:05:03，思考：已经确定需要查询知识库，接下来查找与问题最相关的资料内容。\n"}

event: thinking_delta
data: {"delta":"2026-05-19T09:05:04，思考：资料检索完成，找到 5 条可参考内容。\n"}

event: step
data: {"step":"generating","message":"正在生成回答..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:05:04，思考：已经拿到相关资料，来源文档包括《迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf》，接下来基于这些内容生成回答。\n"}

event: text_delta
data: {"delta":"徐洪波是光明实验室媒体智能团队的全职研究型工程师。"}

event: text_delta
data: {"delta":"他的经历包括：2017-2021 年就读于河南工业大学，2021-2024 年就读于北京工业大学；曾在联想、昆仑万维实习，2024 年 7 月入职光明实验室。"}

event: references
data: {"references":[{"doc_id":"c4a32e97-2733-5dd9-8f1e-49deb2175251","doc_name":"迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf","chunk_ids":["c4a32e97-2733-5dd9-8f1e-49deb2175251:page:4","c4a32e97-2733-5dd9-8f1e-49deb2175251:node:0003"]}]}

event: step
data: {"step":"report_writing","message":"正在撰写报告..."}

event: thinking_delta
data: {"delta":"2026-05-19T09:05:06，思考：大纲已确认，当前进入整篇报告生成流程，将基于大纲和参考资料生成正文。\n"}

event: report_start
data: {"message_id":""}

event: report_text_delta
data: {"delta":"# 《马飞研究员个人介绍报告》\n\n## 一、报告摘要\n"}

event: report_text_delta
data: {"delta":"本报告系统介绍马飞研究员的基本情况、研究方向、科研成果和团队建设情况。"}

event: report_text_delta
data: {"delta":"\n\n## 二、个人基本概况\n\n### 2.1 基础身份信息\n马飞现任光明实验室媒体智能团队负责人。"}

event: references
data: {"references":[{"doc_id":"c4a32e97-2733-5dd9-8f1e-49deb2175251","doc_name":"迈向以人为中心的世界模型-初步探索-马飞-2026年2月10日.pdf","chunk_ids":["c4a32e97-2733-5dd9-8f1e-49deb2175251:page:4","c4a32e97-2733-5dd9-8f1e-49deb2175251:node:0008","c4a32e97-2733-5dd9-8f1e-49deb2175251:page:9"]}]}

event: report_end
data: {"message_id":"","status":"completed"}

event: stream_end
data: {"usage":{"prompt_tokens":3500,"completion_tokens":2200,"total_tokens":5700},"finish_reason":"complete","duration_ms":42000}
```

### 11.3 前端动作

- `intent` 包含 `knowledge_qa` 和 `report_write`。
- 在收到 `text_delta` 时，渲染左侧聊天回答。
- 在收到第一个 `references` 时，可归属给左侧聊天回答。
- 在收到 `report_start` 时，打开右侧报告窗口。
- 在收到 `report_text_delta` 时，渲染右侧报告正文。
- 在收到第二个 `references` 时，可归属给右侧报告正文。
- 在收到 `report_end` 时，右侧报告生成完成。

## 12. 错误响应示例

如果运行过程中出现异常，接口仍可能先返回部分 SSE，然后输出 `error` 和 `stream_end`。

```text
event: stream_start
data: {"conversation_id":"conv_error","session_id":"sess-error","created_at":"2026-05-19T01:06:00+00:00"}

event: error
data: {"code":500,"type":"RuntimeError","message":"LLM request failed","fatal":true}

event: stream_end
data: {"usage":{},"finish_reason":"complete","duration_ms":500}
```

前端建议：

- 收到 `error.fatal=true` 时停止当前消息加载态。
- 已经收到的 `text_delta` / `report_text_delta` 可保留，但应提示本轮未完整完成。

## 13. 联调注意事项

1. `intent` 是数组，不要按单一字符串处理。
2. 组合意图下，同一轮可能先输出左侧回答，再输出右侧报告。
3. `references` 可能出现多次，前端需要按上下文归属。
4. 报告正文一定看 `report_text_delta`，不要把 `text_delta` 当作报告正文。
5. 首次要求“生成报告”时通常先输出大纲，即 `report_outline`。
6. 用户确认大纲后再“开始写”，才会输出 `report_write` 和 `report_text_delta`。
7. 完整报告建议 `generation_config.max_tokens >= 8192`。
8. `current_outline` 支持标准对象，也支持 `{ "outline": {...} }` 包装对象。
