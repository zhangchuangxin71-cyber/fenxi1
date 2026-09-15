# Responses-like 扩展协议规范

## 1. 文档目的

本文定义供长任务 Agent 共用的 Responses-like V1 协议。它适合内部采用 LangGraph Agent Server、
对外需要文本流、运行状态、阶段产物和 HITL 的应用。

协议借用了 OpenAI Responses API 的请求形态、生命周期事件和文本/reasoning 事件，但增加了项目上下文、
HITL 和三种 `agent.*` SSE 事件。因此它是 **Responses-like 扩展协议**，不是 OpenAI 官方 Responses
API 的完全兼容实现。实现者不得声称未知客户端可以在不做扩展适配的情况下完整消费本协议。

本文将兼容性要求分成两层：

- **事件层是强约束**：`agent.activity`、`agent.artifact`、`agent.interrupt` 的事件名、公共字段、
  枚举语义和版本规则必须与本文对齐。这是多个 Agent 复用同一套前端 SSE parser 和 reducer 的基础。
- **接口层是推荐 profile**：应用可以只实现适合自身的接口子集，也可以增加状态查询、artifact 下载、
  历史记录或管理类扩展接口；但只要采用本文中的同名接口，就应尽量保持本文定义的请求目的、恢复语义、
  cancel 语义和状态码，避免同一路径在不同 Agent 中表示不同操作。

兼容性边界如下：

| 内容 | 兼容级别 | 约束 |
| --- | --- | --- |
| 三个 `agent.*` 事件名 | 必须对齐 | 只能使用 `agent.activity`、`agent.artifact`、`agent.interrupt` 表达这三类公共语义 |
| 三个事件的公共字段、枚举和 upsert 语义 | 必须对齐 | 遵守第 5 节和第 7 节，破坏性变化必须提升协议版本 |
| activity 名称、artifact stage、form fields | 应用扩展 | 放在既有 payload 中扩展，不新增按节点命名的公共事件 |
| 本文列出的同名 HTTP 接口 | 语义应对齐 | 可以不全部实现；一旦使用同一路径，应保持创建、恢复或取消的基本含义 |
| 应用专有 HTTP 接口 | 自由扩展 | 可以增加查询、下载、历史、管理或独立 resume 等接口 |

V1 的设计原则：

1. 标准事件承载 Response 生命周期、用户可见文本和 reasoning summary。
2. 自定义事件固定为 `agent.activity`、`agent.artifact`、`agent.interrupt` 三种。
3. 图节点名称和业务产物类型放入 payload，不为每个节点增加新的 SSE 事件名。
4. 推荐将 HITL 恢复合并到 `POST /v1/responses`；应用即使额外提供独立 resume 接口，也应保持相同
   decision、ID 校验和新 Response 生命周期语义。
5. cancel 是资源控制操作；采用本文推荐接口族时使用独立的 cancel endpoint，其他接口设计也不能把
   cancel 伪装成普通用户消息。
6. 本推荐 profile 不提供 Response 查询、事件重放或持久化事件序号；应用可以扩展查询接口，但 SSE
   断线始终不应被解释为取消后台 run。

## 2. 推荐接口族

本节描述一套已经经过实现验证的最小接口族，不是所有应用必须逐项照搬的封闭清单。应用可以增加例如：

```text
GET  /v1/responses/{response_id}
GET  /v1/artifacts/{artifact_id}
POST /v1/responses/{response_id}/resume
```

这些额外接口不属于本文的跨 Agent 必选部分。若增加独立 resume，它只是主接口恢复能力的便捷入口，
不能采用与第 4.2 节相冲突的 `interrupt_id/decision/feedback` 含义。无论接口族如何扩展，公开 SSE 中
三个 `agent.*` 事件仍必须遵守第 7 节。

### 2.1 创建、修改和恢复

```http
POST /v1/responses
Content-Type: application/json
Accept: text/event-stream
```

该接口统一处理：

- 首次创建工作流；
- 工作流完成后的业务修改；
- HITL `approve/revise/regenerate`；
- pending 状态下的自由输入；
- “继续”“刚才到哪了”等状态恢复请求；
- 失败后用户明确要求的有限重试。

本推荐 profile **不要求** `POST /v1/responses/{response_id}/resume`。恢复所需的
`previous_response_id` 和 HITL decision 合并在新的 `POST /v1/responses` 请求中。其他应用可以额外
提供独立 resume 接口，但同一应用应选定一个主恢复路径，避免前端维护两套不一致的状态机。

### 2.2 取消

```http
POST /v1/responses/{response_id}/cancel
Content-Type: application/json
```

cancel 用于正式停止当前正在执行或等待 HITL 的 workflow。它与关闭 SSE 连接不同：关闭连接只停止
客户端接收，后台 run 继续执行；cancel 会请求内部 runtime 停止工作，避免继续调用模型和工具。

### 2.3 健康检查

```text
GET /health/live
GET /health/ready
```

健康检查不属于 Responses-like 协议兼容范围。生产服务建议提供存活和就绪探针；路径可以按部署平台
调整。若使用上述同名路径，应分别保持“进程存活”和“依赖就绪”的含义。

## 3. 标识与生命周期

| 标识 | 语义 | 生命周期 |
| --- | --- | --- |
| `session_id` | 调用方管理的会话标识；服务端据此派生 LangGraph thread | 一个前端会话 |
| `run_id` | 一次完整业务工作流，例如首次生成或完成后的新一轮修改 | 跨多个 HITL Response |
| `response_id` | 从开始/恢复到下一个 interrupt 或终态的一段 SSE | 一个流片段 |
| `artifact_id` | 一个版本化业务产物快照 | 一个 revision |
| `interrupt_id` | 当前待处理 HITL 交互 | 到 decision 被接受或被 supersede/cancel |

同一个业务 run 可以产生多个 response：

```text
run-1
  response-1 -> requirements interrupt
  response-2 -> plan interrupt
  response-3 -> draft interrupt
  response-4 -> workflow completed
```

到达 interrupt 时，当前 SSE 以 `response.completed` 结束，但
`response.metadata.workflow_status=waiting_for_input`。这里的 `response.completed` 只表示当前流片段
完成，不表示整个 workflow 已完成。

## 4. 推荐请求契约

本节是 `POST /v1/responses` 推荐 profile 的请求契约。应用可以在 `context` 中增加业务字段，也可以为
额外接口定义其他请求体；但使用该同名主接口时，应保持“创建新 Response 或恢复到下一段 Response”
的基本语义。

### 4.1 首次创建或普通业务修改

```json
{
  "model": "example-long-task-agent",
  "stream": true,
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "请根据本次输入完成任务并生成交付物"
        }
      ]
    }
  ],
  "context": {
    "session_id": "client-session-1",
    "debug": false,
    "application_context": {
      "example_option": "application-defined-value"
    }
  }
}
```

通用约束：

- `stream` 在 V1 中必须为 `true`。
- `input` 是调用方管理的完整对话历史；adapter 不负责永久保存或重新拼接对话。
- message `role` 支持 `user/assistant/system/developer`。
- message `content` 可以是字符串，也可以是 `[{"type":"input_text","text":"..."}]`。
- Agent 专有字段放在顶层 `context` 或其中约定的扩展对象；不同 Agent 可以定义自己的业务字段，本文
  不规定知识库、文档、项目或租户字段。
- `session_id` 必填且在调用方系统内稳定。
- 首次创建时必须满足具体 Agent 对业务输入的要求；这些要求属于应用契约，不属于公共事件协议。
- 普通创建/修改请求不得携带 `previous_response_id` 或 `context.hitl`。

### 4.2 HITL 恢复

```json
{
  "model": "example-long-task-agent",
  "stream": true,
  "previous_response_id": "resp_previous",
  "input": [
    {"role": "user", "content": "请按反馈调整当前候选结果的第二部分"}
  ],
  "context": {
    "session_id": "client-session-1",
    "debug": false,
    "hitl": {
      "interrupt_id": "int_current",
      "decision": "revise",
      "feedback": "请调整第二部分的结构，并补充必要说明"
    }
  }
}
```

`context.hitl` 契约：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `interrupt_id` | string | 必须等于当前 pending interrupt 的 ID |
| `decision` | enum | `approve`、`revise` 或 `regenerate` |
| `feedback` | string | artifact review 的 `revise` 时必填；结构化 clarification 可以为空 |
| `selection` | object | 可选的应用自定义结构化选择；仅用于 clarification 且必须配合 `decision=revise` |

`previous_response_id` 是产生当前 interrupt 的 Response ID，不等于 `interrupt_id`。adapter 必须同时
校验 session/thread、当前 response、interrupt、artifact 和 revision。过期审批返回 HTTP 409
`STALE_INTERRUPT`，不得错误恢复新版本。

decision 语义：

- `approve`：批准当前候选，进入下一阶段。
- `revise`：根据 feedback 修改当前候选，或提交 clarification 的结构化 selection；两者至少提供一个。
  同一业务 revision 内覆盖尚未批准的候选。
- `regenerate`：不保留当前候选内容，重新生成同一阶段。

adapter 接受审批后创建新的 `response_id`，再把以下内部控制值转换为 LangGraph
`Command(resume=...)`：

```json
{
  "decision": "revise",
  "feedback": "请调整第二部分的结构",
  "response_id": "resp_new"
}
```

review interrupt node 恢复后必须校验 payload，并把新的 `response_id` 写入 Graph State。adapter 不应
直接修改 checkpointer 内部表。

### 4.3 pending 状态下的自由输入

若请求没有 `context.hitl`，但 checkpoint 中存在 pending interrupt，adapter 应先执行 preflight：

- “继续”“刚才到哪了”：不 resume，不重跑图，复用原 `response_id/interrupt_id` 重发 artifact 和
  HITL 卡片；
- 明确接受、修改或重生成当前候选：可转换为结构化 decision 后恢复；
- 明确改变上游需求：supersede 当前 workflow，创建新 revision；
- 无法可靠分类：保留 pending，重新展示当前审批卡，不擅自推进。

对跨 Agent 共用协议而言，推荐前端始终发送显式 `context.hitl`；自由输入分类属于具体 Agent 的产品
能力，不应成为协议正确性的唯一保障。

### 4.4 cancel 请求和响应

```json
{
  "context": {
    "session_id": "client-session-1"
  }
}
```

确认取消后返回普通 JSON，不返回 SSE：

```json
{
  "id": "resp_current",
  "object": "response",
  "status": "cancelled",
  "run_id": "run_current",
  "artifact_id": "art_current"
}
```

- HTTP 200：已经确认 `cancelled`；对同一已取消 Response 重复调用时幂等返回。
- HTTP 202：内部 runtime 已接受请求，但尚未确认终止，状态为 `cancelling`；客户端应暂时禁用该
  session 的新请求并按退避策略重试同一 cancel。
- HTTP 409：Response 已完成、失败、被 supersede，或该 ID 已不再控制当前 workflow。

cancel 不创建新业务 Response，不需要追加到用户对话历史。cancelled checkpoint 不能通过“继续”复活；
用户随后提出真实新需求时，应创建新 revision。

## 5. SSE 通用规则

响应的 media type 为 `text/event-stream`。每一帧同时设置 `event:` 和 JSON `type`，二者必须相同：

```text
event: agent.activity
data: {"type":"agent.activity",...}

```

流结束时发送：

```text
data: [DONE]

```

公开事件至少包含与其语义相关的 `run_id`、`response_id` 和 ISO 8601 UTC `timestamp`。V1 不分配
持久化 `sequence/event_id`，断线后不会补发错过的 token、reasoning、activity 或临时工具状态。

如果应用支持从 checkpoint/业务表重放 pending interrupt 或最终 artifact，重放必须仍发送一条完整 SSE
流：`response.created -> 可选说明文本 -> artifact/interrupt -> 内容 done -> response.completed -> [DONE]`。
重放可以复用原 `response_id` 和 `interrupt_id`，因此 `response.created` 不保证对同一 ID 只出现一次。
客户端必须对 Response 使用 get-or-create，对 artifact/interrupt 使用 upsert，禁止重复 created 时清空旧文本
或创建重复审批卡。重放说明文本属于临时状态，不应覆盖同 ID 的历史 assistant 正文。

客户端必须忽略未知事件和未知可选字段。增加可选字段不提升 `schema_version`；删除字段、改变含义或
改变枚举语义时必须提升版本。

## 6. 标准 Responses-like 事件

V1 使用以下标准风格事件：

```text
response.created
response.output_item.added
response.content_part.added
response.output_text.delta
response.output_text.done
response.content_part.done
response.output_item.done
response.reasoning_summary_part.added
response.reasoning_summary_text.delta
response.reasoning_summary_text.done
response.reasoning_summary_part.done
response.completed
response.failed
```

文本由 `response.output_text.delta.delta` 累加；reasoning summary 由
`response.reasoning_summary_text.delta.delta` 累加。adapter 应保持 item ID、output index 和 content
index 在同一 Response 内稳定。

终态示例：

```json
{
  "type": "response.completed",
  "response": {
    "id": "resp_01",
    "object": "response",
    "created_at": 1786546812,
    "model": "example-long-task-agent",
    "status": "completed",
    "output": [],
    "error": null,
    "metadata": {
      "run_id": "run_01",
      "artifact_id": "art_01",
      "revision": 1,
      "workflow_status": "waiting_for_input",
      "pending_interrupt_id": "int_01"
    },
    "usage": {
      "input_tokens": 1000,
      "output_tokens": 300,
      "total_tokens": 1300
    }
  }
}
```

`workflow_status` 至少可能为 `running`、`waiting_for_input`、`completed`、`failed`、`cancelled` 或
`superseded`。多个 LLM 调用的 usage 可以在当前 Response 终态中聚合。

`response.failed` 使用相同 Response 外形，并在 `response.error` 中放入稳定错误。建立 SSE 之前发生
的请求错误使用普通 HTTP JSON：

```json
{
  "error": {
    "code": "STALE_INTERRUPT",
    "message": "...",
    "retryable": false,
    "request_id": "req_01",
    "stage": "plan",
    "details": {}
  }
}
```

## 7. 三种自定义事件

### 7.1 `agent.activity`

用于节点、工具和降级状态。相同 `activity.id` 表示同一个前端项目，前端应原位更新。

```text
event: agent.activity
data: {"type":"agent.activity","schema_version":"1","run_id":"run_01","response_id":"resp_01","timestamp":"2026-08-12T08:00:00Z","activity":{"id":"activity_collect_inputs_01","kind":"tool","name":"collect_inputs","label":"正在整理任务输入","status":"running","node":"requirements","input_summary":{"input_count":3},"output_summary":null,"error":null}}

```

字段约束：

- `kind`：`node` 或 `tool`；
- `status`：`running/completed/degraded/cancelled/failed`；
- `name/node`：机器可读的稳定名称；
- `label`：可直接展示的用户文案；
- `input_summary/output_summary`：经过裁剪和脱敏的摘要，禁止放完整原文、系统提示词或工具大结果；
- `error`：失败时的稳定错误摘要。

warning 不需要第四种事件，应归一化为 `status=degraded` 的 activity。

### 7.2 `agent.artifact`

用于阶段产物和最终交付物：

```text
event: agent.artifact
data: {"type":"agent.artifact","schema_version":"1","run_id":"run_01","response_id":"resp_01","timestamp":"2026-08-12T08:00:10Z","artifact":{"id":"art_01","revision":2,"stage":"plan","status":"pending_review","content":{"title":"执行计划","steps":["准备输入","生成交付物"]},"summary":null}}

```

- `artifact.id` 标识一个聚合业务 revision，不要求每个阶段生成不同 ID。
- `stage` 是跨 Agent 可扩展的机器可读枚举，例如 `requirements/plan/draft/final_result`；具体取值由应用
  定义，前端不应通过事件名编码 stage。
- `status` 常见值为 `pending_review/completed/degraded`。
- 同一 `artifact.id + stage` 应原位更新。
- HITL 审批所需的候选内容必须在 interrupt 前或同一流中通过 artifact 事件发送。
- 大产物可以只发 summary，但若没有查询接口，则审批所需内容不能被省略。

### 7.3 `agent.interrupt`

用于要求用户参与的 HITL 卡片：

```text
event: agent.interrupt
data: {"type":"agent.interrupt","schema_version":"1","run_id":"run_01","response_id":"resp_01","timestamp":"2026-08-12T08:00:11Z","interrupt":{"id":"int_01","status":"pending","stage":"plan_review","artifact_id":"art_01","revision":2,"form":{"form_type":"agent_artifact_review","title":"请审核执行计划","description":"接受后将进入下一阶段；也可以重新生成或按反馈修改。","fields":[]}}}

```

V1 定义两种 form：

- `agent_artifact_review`：显示对应 artifact，并提供接受、按反馈修改、完全重生成；
- `agent_clarification`：渲染一次性澄清表单。应用可以使用通用 `fields`，也可以使用
  `selection_mode=single + options + custom_option` 表达三选一及自定义输入。选择结果通过
  `decision=revise` 和应用定义的结构化 `selection` 提交。

单选澄清表单示例：

```json
{
  "form_type": "agent_clarification",
  "title": "请确认处理方向",
  "description": "请选择一个推荐方向，或填写自定义方向。",
  "selection_mode": "single",
  "options": [
    {"id": "A", "about": "政策要求", "target": "实际落地方式"},
    {"id": "B", "about": "行业现状", "target": "关键变化和影响"},
    {"id": "C", "about": "典型案例", "target": "可复用的经验"}
  ],
  "custom_option": {"enabled": true, "about_label": "处理范围", "target_label": "关注重点"},
  "fields": []
}
```

公共协议只约束 `selection` 必须是 JSON 对象；对象内字段由具体 Agent 定义。同名推荐项必须通过
服务端返回的 `option.id` 引用，不能由前端回传并覆盖选项正文。

前端必须保存 interrupt 对应的 `response_id` 和 `interrupt.id`。审批成功提交后禁用旧卡片；遇到 409
应保留用户反馈并刷新当前状态，不能自动改用新 interrupt 重试旧 decision。

V1 不要求服务端发送额外的 `interrupt.status=resolved` 事件。推荐卡片状态机是
`pending -> submitting -> resolved/stale/unknown/cancelled`：显式恢复请求收到一个新的
`response.created` 后，才把该请求提交的旧卡标为 resolved；重放同一个 `response_id` 不能触发 resolved。
应用若增加 resolved 事件，可以作为向后兼容补充，但前端不能只依赖它关闭旧卡。

## 8. Adapter 实现建议

建议将 adapter 分为四层：

```text
HTTP admission
  -> session/thread 与 pending preflight
  -> LangGraph Agent Server SDK client
  -> AgentEventNormalizer
  -> Responses-like SSE serializer
```

实现要求：

1. 使用 `session_id` 确定性派生内部 thread ID，不向前端公开 thread。
2. 同一 session 的启动、恢复、supersede 和 cancel 串行化；同时校验当前 interrupt，防止重复审批。
3. 图节点通过一个稳定的 internal custom-event helper 发送 activity、artifact、public text 和 reasoning。
4. normalizer 是唯一读取 LangGraph stream shape 的模块；业务 HTTP 层不直接依赖原始 event 字段。
5. 对外重新生成 response/item ID，不透传模型商或 Agent Server 的内部 Response ID。
6. 文本和 reasoning 使用标准事件；节点、工具、产物和 HITL 分别映射到三种 `agent.*` 事件。
7. 明确区分 Response 片段终态和 workflow 终态。
8. SSE 断开时不要默认 cancel Agent Server run；正式 cancel 必须走 cancel endpoint。
9. 若不持久化产品事件，就不得宣称支持 token 级断线重放；pending 卡片和最终 artifact 可以从
   checkpoint/业务表重新展示。
10. debug 原始提示词、模型输入输出和工具结果不得进入 `agent.activity`；需要时只在显式 debug 模式的
    terminal payload 中一次性返回，并设置大小上限。

跨 Agent 复用时，必须保持顶层事件名、公共字段、枚举含义和 upsert 规则一致。允许差异的部分是：
`model`、`context` 业务字段、activity 名称、artifact stage 和 form fields。接口可以按应用增加，但
同名接口应保持本文语义。任何事件层破坏性变化都应发布新的协议版本并同步前端。
