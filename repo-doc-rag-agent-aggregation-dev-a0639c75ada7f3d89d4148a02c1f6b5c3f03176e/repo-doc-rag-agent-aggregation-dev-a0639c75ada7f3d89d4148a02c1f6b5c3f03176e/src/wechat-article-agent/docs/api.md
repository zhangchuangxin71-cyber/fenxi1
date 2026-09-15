# 微信公众号文章 Agent API 参考

## 1. 基本信息

本文面向微信公众号文章 Agent 的前端调用方，描述当前实际公开接口。生产默认入口为：

```text
http://<agent-host>:8140
```

本地开发默认入口为：

```text
http://127.0.0.1:8240
```

公开接口包括：

| 方法与路径 | 用途 | 响应类型 |
|---|---|---|
| `POST /v1/responses` | 首次生成、后续修改、继续/重试、HITL 恢复 | `text/event-stream` |
| `POST /v1/responses/{response_id}/cancel` | 正式取消后台 run | JSON |
| `GET /health/live` | 进程存活检查 | JSON |
| `GET /health/ready` | 数据库就绪检查 | JSON |

内部 LangGraph Agent Server 不属于公开 API。前端不能调用其 thread、run、command 或 SSE 接口。

### 1.1 产品术语兼容说明

当前产品把原“文档研读”阶段称为“素材调研”，因为该阶段除了读取用户文档，还可能理解网络背景、补充
网络素材并处理事实冲突。前端的阶段标题、进度文案和 HITL 卡片应使用“素材调研”。协议为了兼容已有
调用方，仍保留以下内部值：

| 协议值 | 推荐显示文案 |
|---|---|
| `docs_research` | 素材调研 |
| `docs_research_clarification` | 确认素材调研方向 |
| `document_material_worker` | 研读用户文档 |
| `web_material_worker` | 搜索网络素材 |

前端只做显示映射，不要修改事件中的 `stage/node`，也不要把这些内部英文值直接展示给用户。完整文案和
交互建议见 [前端接入指南 1.1 节](frontend-integration.md#11-产品术语与内部标识)。

当前接口没有独立的 resume endpoint。**所有 HITL approve/revise/regenerate 都继续调用
`POST /v1/responses`**，通过 `previous_response_id` 和 `context.hitl` 触发 checkpoint 恢复。

请求和错误响应都使用 UTF-8 JSON。业务流使用 SSE，响应最终以 `data: [DONE]` 结束。调用方可以发送
`X-Request-ID`；服务端也会在响应头返回 `x-request-id`，未提供时由服务端生成。

当前服务自身不提供浏览器 CORS 或调用方 `Authorization` 校验。生产调用应经过可信 BFF/API 网关；
网关负责 TLS、鉴权、权限和 CORS，不应把 adapter 端口直接暴露为无鉴权公网接口。`X-Request-ID` 仅用于
追踪，不提供请求去重能力。

## 2. ID 的含义和前端责任

### 2.1 先看结论

这些 ID 不都由前端生成，也不都需要在每次请求中传回。前端的责任如下：

| ID | 谁生成 | 前端是否保存 | 前端何时传入 |
|---|---|---|---|
| `session_id` | 前端调用方 | 必须，随会话持久化 | 每次 `POST /v1/responses` 和 cancel 都传 |
| `run_id` | Agent 服务端 | 建议，至少保留在 UI 运行记录中 | 当前公开请求不传 |
| `response_id` | Agent 服务端 | 必须与每段 Response/HITL 卡片关联保存 | cancel 路径参数；HITL 时复制到 `previous_response_id` |
| `previous_response_id` | 不是新 ID，是前端复制已有 `response_id` | 不需要作为独立实体保存 | 仅 HITL 恢复时传 |
| `interrupt_id` | Agent 服务端 | 有 pending 卡片时必须保存 | 仅提交该 HITL 卡片时传入 `context.hitl.interrupt_id` |
| `artifact_id + revision` | Agent 服务端 | 建议与 artifact/HITL 卡片一起保存 | 当前公开请求不传，主要用于 UI 关联与版本展示 |

因此，前端只主动生成 `session_id`。其余 ID 都必须使用 SSE/JSON 响应中的服务端原值，不能自行构造、
递增或根据前缀推导。

### 2.2 `session_id`

`session_id` 是前端会话 ID，也是前后端关联同一次长期对话的稳定业务键。服务端会根据它确定内部
LangGraph thread，并查找该会话当前 artifact、pending interrupt 和运行状态。

前端应当：

- 在创建新对话时生成全局唯一且不可预测的 ID，推荐直接复用前端数据库已有的会话主键或 UUID；
- 将它和完整对话历史一起持久化；
- 同一对话的首次生成、HITL 审批、完成后 revise、“继续”和 cancel 始终传同一个值；
- 新建对话时使用新值；不能因为 HITL 恢复、页面刷新或产生新 revision 而更换；
- 同一 `session_id` 的创建、审批和取消请求必须串行发送。

`session_id` 不是用户 ID。多个会话即使属于同一个 `user_id`，也必须使用不同的 `session_id`。错误复用
会把本应独立的文章关联到同一个 LangGraph thread。

### 2.3 `run_id`

`run_id` 是服务端为一次业务 revision 生成的运行标识，首次出现在
`response.created.response.metadata.run_id`，也会出现在三个 `agent.*` 事件中。同一业务 revision 经过多次
HITL 暂停和恢复时，`run_id` 保持不变；完成后用户提出新修改、服务端创建新 revision 时会生成新的
`run_id`。

前端不需要把 `run_id` 放入任何当前公开请求。建议保存它用于：

- 把同一 revision 内的多段 Response 归到同一个运行视图；
- 新 `run_id` 到达时新建 activity/消息/artifact 展示分组，避免和旧 run 重叠；
- 日志排障和链路定位。

### 2.4 `response_id` 与 `previous_response_id`

`response_id` 标识一次 HTTP SSE Response。首次执行、每次真正的 HITL 恢复以及部分失败重试都会产生
新的 `response_id`。它来自 `response.created.response.id`；三个 `agent.*` 事件也携带对应
`response_id`。

前端至少要区分两种用途：

- `latestResponseId`：当前 workflow 最新、可用于 cancel 的 Response ID；
- `interrupt.sourceResponseId`：产生某一张 HITL 卡片的 Response ID。

`previous_response_id` 不是服务端另外生成的 ID，而是 HITL 请求字段。用户操作某张卡片时，前端把
该卡绑定的 `interrupt.sourceResponseId` 原样复制到请求顶层：

```ts
payload.previous_response_id = pendingCard.sourceResponseId;
```

不要对普通对话、“继续”或完成后的修改传 `previous_response_id`，也不要无条件使用某个全局“最后一次
Response ID”替代卡片自己的 source ID。否则页面中有旧卡片、卡片重放或并发状态变化时容易得到
`STALE_INTERRUPT`。

### 2.5 `interrupt_id`

`interrupt_id` 标识一次等待用户参与的 HITL 交互，来自 `agent.interrupt.interrupt.id`。它和
`response_id` 是两个不同维度：前者是审批卡，后者是产生该卡的 SSE Response。

前端应把下面的信息作为一个整体保存：

```ts
type PendingHitlCard = {
  interruptId: string;
  sourceResponseId: string;
  runId: string;
  artifactId: string;
  revision: number;
  stage: string;
  form: unknown;
};
```

提交卡片时同时发送：

```json
{
  "previous_response_id": "<card.sourceResponseId>",
  "context": {
    "session_id": "<current session>",
    "hitl": {
      "interrupt_id": "<card.interruptId>",
      "decision": "approve"
    }
  }
}
```

一次 decision 被服务端接受，或该 run 被 cancel/supersede 后，旧 `interrupt_id` 就失效。前端应将卡片
标为已处理或已过期，不能再次提交。断线后的“继续”可能重放同一个 `interrupt_id`；此时按 ID upsert，
不要生成重复卡片。

### 2.6 `artifact_id + revision`

artifact 是服务端保存的一版聚合业务产物，包含素材库、任务书、大纲、Markdown 正文、图片信息和最终
HTML。`artifact_id` 标识该 artifact 记录，`revision` 是同一 session 中从 1 开始递增的业务版本号。

当前实现中每个新 revision 都会生成新的 `artifact_id`；二者仍应一起使用，因为：

- `revision` 适合面向用户展示“第几版”；
- `artifact_id` 用于精确关联服务端事件；
- interrupt 同时携带二者，前端可验证审批卡对应哪一版产物；
- artifact 事件还包含 `stage`，同一 revision 内会依次产生多种阶段产物。

建议 artifact UI key 使用：

```ts
const artifactKey = `${artifact.id}:${artifact.revision}:${artifact.stage}`;
```

HITL 卡片关联候选 artifact 时，先匹配 `artifact_id + revision`，再根据 interrupt stage 匹配 artifact
stage。当前公开请求不接受前端回传 `artifact_id/revision`，前端不能通过请求直接指定或切换服务端版本。

### 2.7 一次 HITL 的 ID 生命周期

```text
前端创建 session_id=S1
  -> POST /v1/responses(context.session_id=S1)
  <- response.created: run_id=R1, response_id=P1, artifact_id=A1, revision=1
  <- agent.interrupt: interrupt_id=I1, response_id=P1, artifact_id=A1, revision=1

前端保存卡片 {I1, sourceResponseId=P1, R1, A1, revision=1}
  -> POST /v1/responses(
       context.session_id=S1,
       previous_response_id=P1,
       context.hitl.interrupt_id=I1
     )
  <- response.created: run_id=R1, response_id=P2, artifact_id=A1, revision=1
```

注意恢复后 `response_id` 从 P1 变为 P2，但仍属于同一个 run R1 和同一个 artifact revision A1/1。若
P2 后又产生下一张卡 I2，应保存 `{I2, sourceResponseId=P2}`。文章完成后用户提出新修改，服务端可能
返回 R2、A2、revision 2；这时 UI 新建 run 分组，但 `session_id` 仍是 S1。

## 3. `POST /v1/responses`

### 3.1 用途

同一个接口根据请求字段处理四类行为：

| 请求形态 | 服务端行为 |
|---|---|
| 无 `previous_response_id`、无 `context.hitl`，首次 session | 创建第一个 artifact revision 并启动图 |
| 无 HITL 字段，已有 session，输入真实修改要求 | 创建新 revision，由 orchestrator 判断入口阶段 |
| 无 HITL 字段，输入“继续/重试” | 按运行中、待审批、完成、失败或已取消状态处理 |
| 同时携带 `previous_response_id` 和 `context.hitl` | 校验当前 interrupt 并恢复 LangGraph checkpoint |

`previous_response_id` 不是通用的对话续接字段。它只用于 HITL 恢复；普通后续对话仍通过稳定的
`context.session_id` 和完整 `input` 历史关联。

### 3.2 通用请求体

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "请根据参考文档生成一篇微信公众号文章"
        }
      ]
    }
  ],
  "context": {
    "session_id": "session-20260813-001",
    "user_id": "user-001",
    "kb_id": "kb-001",
    "doc_ids": ["doc-001", "doc-002"],
    "temp_doc_ids": [],
    "debug": false
  }
}
```

顶层字段：

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `model` | string | 否 | 固定为 `wechat-article-agent`，省略时使用该值 |
| `stream` | boolean | 否 | 当前只接受 `true`，省略时为 `true` |
| `input` | array | 是 | 规范化完整对话历史，1 到 500 条；服务端不负责永久保存完整历史，也不静默裁剪超限历史 |
| `previous_response_id` | string | HITL 时是 | 产生当前 interrupt 的 Response ID；最多 256 字符，不能用于普通请求 |
| `context` | object | 是 | 会话、文档范围和可选 HITL 信息 |

`input[]` 字段：

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `role` | string | 是 | `user`、`assistant`、`system` 或 `developer` |
| `content` | string 或 array | 是 | 对话文本，或 Responses 风格的文本 part 数组 |

文本 part 字段：

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `type` | string | 是 | `input_text` 或 `output_text` |
| `text` | string | 是 | 非空文本，单 part 最大 1,000,000 字符 |

`context` 字段：

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `session_id` | string | 是 | 前端会话的稳定 ID，1 到 512 字符；同 session 的请求必须串行提交 |
| `user_id` | string/null | 否 | 调用方用户 ID，最多 256 字符 |
| `kb_id` | string/null | 否 | 知识库 ID，最多 256 字符 |
| `doc_ids` | string[]/null | 否 | 普通文档 ID，最多 500 个；空白和重复项会被清理 |
| `temp_doc_ids` | string[]/null | 否 | 临时文档 ID，最多 500 个 |
| `debug` | boolean | 否 | 默认 `false`；仅服务端 `DEBUG_ENABLED=true` 时返回 debug trace |
| `hitl` | object/null | HITL 时是 | 结构化审批；普通请求不能携带 |

请求体是严格 schema，未知字段会返回 HTTP 422 `INVALID_REQUEST`。

“规范化完整历史”只包含真实 user/assistant 对话：保留用户需求、修改和可读的审批记录，以及非重放流的
`response.output_text.done`；不包含 artifact 全文、reasoning、activity、工具/debug 或 SSE JSON。“继续”
作为临时恢复消息追加到当次请求，但不永久保存。HITL 的用户反馈既以可读 `user` 消息进入历史，又以
`context.hitl` 结构化提交，这是有意的职责分离。确定映射表和长历史压缩规则见
[前端接入指南 3.1 节](frontend-integration.md#31-下一次-input-的确定性构造规则)。

### 3.3 首次生成

首次生成不携带 `previous_response_id` 和 `context.hitl`：

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "input": [
    {"role": "user", "content": "请根据这些材料生成一篇面向企业管理者的微信公众号文章"}
  ],
  "context": {
    "session_id": "session-001",
    "user_id": "user-001",
    "kb_id": "kb-001",
    "doc_ids": ["doc-a", "doc-b"],
    "temp_doc_ids": [],
    "debug": false
  }
}
```

### 3.4 完成后的修改

已有文章完成后，用户提出真实修改要求时，仍不携带 HITL 字段。前端应传入自己保存的完整历史：

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "input": [
    {"role": "user", "content": "请根据这些材料生成一篇面向企业管理者的微信公众号文章"},
    {"role": "assistant", "content": "文章已经生成并完成排版。"},
    {"role": "user", "content": "在第三章增加一个关于数据安全的小节"}
  ],
  "context": {
    "session_id": "session-001",
    "user_id": "user-001",
    "kb_id": "kb-001",
    "doc_ids": ["doc-a", "doc-b"],
    "temp_doc_ids": [],
    "debug": false
  }
}
```

服务端创建新的 artifact revision，并由 orchestrator 根据原始历史和最新要求判断从素材调研、任务书、
大纲或正文中的哪个阶段重新开始。前端不能自行假定入口节点。协议中的部分内部 stage 仍保留
`docs_research*` 命名，但产品界面应显示“素材调研”。

### 3.5 “继续”和断线恢复

“继续”在协议形状上是一条不携带 HITL 字段的 `user` 输入，但恢复场景应把它视作临时控制消息：只追加
到本次请求，不写入前端永久对话历史，也不渲染为新的用户气泡。

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "input": [
    {"role": "user", "content": "请生成文章"},
    {"role": "user", "content": "继续"}
  ],
  "context": {
    "session_id": "session-001",
    "user_id": "user-001",
    "kb_id": "kb-001",
    "doc_ids": ["doc-a"],
    "temp_doc_ids": [],
    "debug": false
  }
}
```

可能结果：

| 服务端状态 | 结果 |
|---|---|
| 后台 run 仍运行 | HTTP 409 `RUN_IN_PROGRESS` |
| 正等待 HITL | SSE 重发当前 artifact 和原 interrupt，复用原 ID |
| 已完成 | SSE 重发最终 HTML artifact |
| 可恢复临时失败 | 创建新 Response，从失败 checkpoint 重试 |
| 本轮次数耗尽 | 创建新 revision，由 orchestrator 重新路由 |
| 必须修正输入 | SSE/错误提示修正文档或请求，不能盲目重试 |
| 已取消 | 不复活旧 checkpoint；作为新请求进入 orchestrator |

断开 SSE 只停止接收，不会取消后台 run，也不会补发断线期间错过的 token、reasoning 或 activity。
“继续”不是无副作用的 GET 状态查询：失败时可能触发重试，已取消时可能创建新 revision。页面刷新时
不应无条件自动调用；完整启动恢复算法见
[前端接入指南 6.2 节](frontend-integration.md#62-页面启动恢复算法)。

### 3.6 HITL 恢复通用字段

HITL 请求必须同时携带 `previous_response_id` 和 `context.hitl`：

```json
{
  "previous_response_id": "resp_that_created_interrupt",
  "context": {
    "session_id": "session-001",
    "hitl": {
      "interrupt_id": "int_current",
      "decision": "approve",
      "feedback": ""
    }
  }
}
```

`context.hitl` 字段：

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `interrupt_id` | string | 是 | 当前卡片的 `interrupt.id`，1 到 256 字符 |
| `decision` | string | 是 | `approve`、`revise` 或 `regenerate` |
| `feedback` | string | 否 | 最多 100,000 字符；artifact 的 `revise` 必填 |
| `selection` | object/null | 意图/文档澄清时是 | 结构化选择，只能配合 `decision=revise` |

各 ID 的生成方、保存方式和回传规则见第 2 节。HITL 请求必须使用当前卡片绑定的
`sourceResponseId/interruptId`，不能从其他卡片或其他 session 取值。

### 3.7 公众号文章意图确认

当输入不是明确的公众号文章请求时，服务端可能返回 `stage=intent_clarification` 的选项卡。
选择 A/B/C 时只回传 `option_id`：

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "previous_response_id": "resp_intent_001",
  "input": [
    {"role": "user", "content": "已确认围绕‘人工智能如何改变日常工作’生成微信公众号文章。"}
  ],
  "context": {
    "session_id": "session-001",
    "user_id": "user-001",
    "kb_id": "kb-001",
    "debug": false,
    "hitl": {
      "interrupt_id": "int_intent_001",
      "decision": "revise",
      "feedback": "",
      "selection": {"option_id": "A"}
    }
  }
}
```

选择“其他”时，使用 `topic` 提交用户填写的主题：

```json
{
  "interrupt_id": "int_intent_001",
  "decision": "revise",
  "feedback": "",
  "selection": {
    "option_id": "custom",
    "topic": "乡村教育数字化"
  }
}
```

A/B/C 会直接确认公众号文章意图；自定义输入只会再判断一次意图，不会产生第二张意图确认卡。
如果仍不是公众号文章意图，服务端将用普通文本提示用户切换 Agent。

### 3.8 素材调研方向：推荐项

方向选项中的 `target/about` 定义如下：

| 字段 | 产品含义 | 不应解释为 |
|---|---|---|
| `target` | 要围绕什么对象、事件或问题搜集素材 | 底层搜索 query、最终文章标题 |
| `about` | 针对该主题重点搜集哪些事实、观点、案例或表达方式 | 文章受众、语气、写作目标 |

前端建议显示为“围绕「{target}」搜集素材 / 重点关注：{about}”。字段的职责是定义素材筛选边界，受众、
目标和语气由后续任务书节点确认。

用户选择服务端给出的 A/B/C 时，只提交 `option_id`，不能把 about/target 回传给服务端覆盖原选项：

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "previous_response_id": "resp_docs_001",
  "input": [
    {"role": "user", "content": "已确认素材调研方向：围绕银行资产质量搜集素材，重点关注不良贷款、拨备覆盖和风险变化。"}
  ],
  "context": {
    "session_id": "session-001",
    "user_id": "user-001",
    "kb_id": "kb-001",
    "doc_ids": ["doc-a", "doc-b"],
    "temp_doc_ids": [],
    "debug": false,
    "hitl": {
      "interrupt_id": "int_docs_001",
      "decision": "revise",
      "feedback": "",
      "selection": {"option_id": "B"}
    }
  }
}
```

### 3.9 素材调研方向：自定义项

自定义项必须同时提供非空 `about` 和 `target`：

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "previous_response_id": "resp_docs_001",
  "input": [
    {"role": "user", "content": "我希望搜集区域银行普惠金融业务的相关素材。"}
  ],
  "context": {
    "session_id": "session-001",
    "user_id": "user-001",
    "kb_id": "kb-001",
    "doc_ids": ["doc-a", "doc-b"],
    "temp_doc_ids": [],
    "debug": false,
    "hitl": {
      "interrupt_id": "int_docs_001",
      "decision": "revise",
      "feedback": "",
      "selection": {
        "option_id": "custom",
        "about": "小微企业服务模式和风险控制措施",
        "target": "区域银行的普惠金融业务"
      }
    }
  }
}
```

`selection` 字段：

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `option_id` | string | 是 | `A`、`B`、`C` 或 `custom` |
| `about` | string | custom 时是 | 关注重点：针对该主题重点搜集哪些事实、观点、案例或表达方式，最多 10,000 字符 |
| `target` | string | custom 时是 | 素材主题：围绕什么对象、事件或问题搜集素材，最多 10,000 字符 |
| `topic` | string | 意图确认的 custom 时是 | 用户自定义的公众号文章主题，最多 10,000 字符 |

A/B/C 不允许携带 `about/target`。自定义方向仍含糊时，服务端可能返回新的 clarification interrupt；
前端应按新的 `interrupt_id` 渲染新卡片。

### 3.10 Artifact 审批

任务书、大纲和正文使用相同请求结构。

接受当前候选：

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "previous_response_id": "resp_outline_001",
  "input": [
    {"role": "user", "content": "我接受当前文章大纲。"}
  ],
  "context": {
    "session_id": "session-001",
    "user_id": "user-001",
    "kb_id": "kb-001",
    "doc_ids": ["doc-a"],
    "temp_doc_ids": [],
    "debug": false,
    "hitl": {
      "interrupt_id": "int_outline_001",
      "decision": "approve",
      "feedback": ""
    }
  }
}
```

按反馈修改当前候选：

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "previous_response_id": "resp_outline_001",
  "input": [
    {"role": "user", "content": "请在第三章增加风险提示小节。"}
  ],
  "context": {
    "session_id": "session-001",
    "user_id": "user-001",
    "kb_id": "kb-001",
    "doc_ids": ["doc-a"],
    "temp_doc_ids": [],
    "debug": false,
    "hitl": {
      "interrupt_id": "int_outline_001",
      "decision": "revise",
      "feedback": "在第三章增加风险提示小节"
    }
  }
}
```

完全重新生成当前候选：

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "previous_response_id": "resp_outline_001",
  "input": [
    {"role": "user", "content": "请换一种更紧凑的组织方式重新生成大纲。"}
  ],
  "context": {
    "session_id": "session-001",
    "user_id": "user-001",
    "kb_id": "kb-001",
    "doc_ids": ["doc-a"],
    "temp_doc_ids": [],
    "debug": false,
    "hitl": {
      "interrupt_id": "int_outline_001",
      "decision": "regenerate",
      "feedback": "换一种更紧凑的组织方式"
    }
  }
}
```

### 3.11 SSE 响应

成功建立流后返回：

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
```

典型流：

```text
event: response.created
data: {"type":"response.created","response":{"id":"resp_001","object":"response","created_at":1786608000,"model":"wechat-article-agent","status":"in_progress","output":[],"error":null,"metadata":{"run_id":"run_001","artifact_id":"art_001","revision":1,"workflow_status":"running","pending_interrupt_id":null}}}

event: agent.activity
data: {"type":"agent.activity","run_id":"run_001","response_id":"resp_001","timestamp":"2026-08-13T08:00:00Z","schema_version":"1","activity":{"id":"activity_001","kind":"node","name":"intent_router","label":"识别请求类型","status":"running","node":"intent_router","input_summary":null,"output_summary":null,"error":null}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","run_id":"run_001","response_id":"resp_001","timestamp":"2026-08-13T08:00:01Z","item_id":"msg_001","output_index":0,"content_index":0,"delta":"正在分析参考文档。"}

event: agent.artifact
data: {"type":"agent.artifact","run_id":"run_001","response_id":"resp_001","timestamp":"2026-08-13T08:00:10Z","schema_version":"1","artifact":{"id":"art_001","revision":1,"stage":"outline","status":"pending_review","content":"# 示例大纲","summary":null}}

event: agent.interrupt
data: {"type":"agent.interrupt","run_id":"run_001","response_id":"resp_001","timestamp":"2026-08-13T08:00:11Z","schema_version":"1","interrupt":{"id":"int_outline_001","status":"pending","stage":"outline_review","artifact_id":"art_001","revision":1,"form":{"form_type":"agent_artifact_review","title":"请审核文章大纲","description":"接受后将进入下一阶段；也可以重新生成或按反馈修改。","fields":[]}}}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_001","object":"response","created_at":1786608011,"model":"wechat-article-agent","status":"completed","output":[],"error":null,"metadata":{"run_id":"run_001","artifact_id":"art_001","revision":1,"workflow_status":"waiting_for_input","pending_interrupt_id":"int_outline_001"}}}

data: [DONE]

```

公共 SSE 外层字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `type` | string | 事件类型，与 `event:` 通常一致 |
| `run_id` | string | 当前业务 run；部分终态事件位于 `response.metadata` |
| `response_id` | string | 当前流对应的 Response ID |
| `timestamp` | string | UTC ISO 8601 时间 |
| `schema_version` | string | 三种 `agent.*` 当前为 `1` |

前端必须处理的事件：

| 事件 | 关键字段 | 用途 |
|---|---|---|
| `response.created` | `response.id/metadata` | 初始化本段 Response 和 ID 映射 |
| `response.output_text.delta` | `delta` | 追加解释性文本，形成打字机效果 |
| `response.reasoning_summary_text.delta` | `delta` | 追加可折叠 reasoning 文本 |
| `response.output_text.done` | `text` | 固化完整解释文本，不重复追加 |
| `agent.activity` | `activity` | 按 `activity.id` 原位更新节点/工具状态 |
| `agent.artifact` | `artifact` | 按 `artifact.id + stage` 原位更新产物 |
| `agent.interrupt` | `interrupt` | 根据 `form.form_type` 渲染 HITL 卡片 |
| `response.completed` | `response.metadata.workflow_status` | 本段流正常结束，可能仍等待 HITL |
| `response.failed` | `response.error` | 本段流失败，展示稳定错误和恢复建议 |
| `[DONE]` | 无 | HTTP SSE 流结束 |

`response.completed` 不等于整篇文章已完成。只有
`response.metadata.workflow_status == "completed"` 才表示最终完成；`waiting_for_input` 表示应保留审批卡。

pending/final 重放会再次发送旧 `response_id` 的 `response.created`，然后发送当前 artifact/interrupt、
终态和 `[DONE]`。这不是新 Response，前端必须 get-or-create，不能清空同 ID 的旧文本。完整逐帧顺序和
reducer 规则见 [前端接入指南 6.1 节](frontend-integration.md#61-pending-重放的完整-sse-顺序)。

`agent.activity`、`agent.artifact`、`agent.interrupt` 的完整 JSON、字段和渲染规则见
[前端接入指南](frontend-integration.md)。标准 OpenAI SDK 可能不会分派未知的 `agent.*`，必须保留原始
SSE frame 解析能力。

联网素材搜索也是普通 `agent.activity` 工具事件。开始与完成事件共用同一个 `activity.id`，前端必须
原位更新：

```text
event: agent.activity
data: {"type":"agent.activity","run_id":"run_001","response_id":"resp_002","timestamp":"2026-08-20T08:00:20Z","schema_version":"1","activity":{"id":"activity_web_material_001","kind":"tool","name":"ark.web_search","label":"搜索网络写作素材","status":"running","node":"web_material_worker","input_summary":{"categories":4},"output_summary":null,"error":null}}

event: agent.activity
data: {"type":"agent.activity","run_id":"run_001","response_id":"resp_002","timestamp":"2026-08-20T08:00:42Z","schema_version":"1","activity":{"id":"activity_web_material_001","kind":"tool","name":"ark.web_search","label":"网络写作素材搜索完成","status":"completed","node":"web_material_worker","input_summary":null,"output_summary":{"material_count":3,"query_count":4},"error":null}}
```

公网非 debug 模式只返回上述安全摘要，不返回实际 query、工具参数、annotations 或搜索结果正文。

排版 Skill 也遵循同一 `agent.activity` 契约。配置为 `skill_driven` 时，可能收到如下安全活动（示例仅
展示协议形状，前端应按 `activity.id` 做幂等更新）：

```json
{"type":"agent.activity","run_id":"run_001","response_id":"resp_005","timestamp":"2026-08-21T08:04:00Z","schema_version":"1","activity":{"id":"activity_layout_skill_001","kind":"skill","name":"wechat_layout.skill_driven","label":"使用排版 Agent 与 Skill","status":"running","node":"render_html","input_summary":{"renderer":"skill_driven"},"output_summary":null,"error":null}}
```

具体工具活动使用 `kind=tool`，例如 `agent_engine.assemble_skill_html`；`kind=skill` 表示加载或应用
Skill 资产。debug 关闭时只发送摘要，不发送 `arguments`、完整 HTML 中间候选或工具结果；debug 开启且
请求允许 debug 时，完整参数和结果只出现在终态 debug trace 中。无论使用哪种排版模式，最终业务产物仍
是同一个 `final_html` artifact，HTTP 接口和 artifact stage 不变。

`response` snapshot 字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | string | 当前 Response ID |
| `object` | string | 固定为 `response` |
| `created_at` | integer | Unix 秒时间戳 |
| `model` | string | `wechat-article-agent` |
| `status` | string | `in_progress`、`completed` 或 `failed` |
| `output` | array | 当前 adapter 不在终态 snapshot 中重建完整 output，通常为空 |
| `error` | object/null | 失败时的稳定错误 |
| `usage` | object/缺省 | 可获得时累计 token usage |
| `metadata.run_id` | string | 当前业务 run |
| `metadata.artifact_id` | string | 当前 artifact ID |
| `metadata.revision` | integer | 业务 revision |
| `metadata.workflow_status` | string | `running/waiting_for_input/completed/failed/cancelled/superseded` 等 |
| `metadata.pending_interrupt_id` | string/null | 当前待审批 interrupt |

### 3.12 `agent.artifact` 的应用级数据契约

公共事件外形如下，`content` 和 `summary` 的类型由 `artifact.stage` 判别：

```ts
type ArtifactStatus = "pending_review" | "completed" | "degraded";

type AgentArtifactEvent = {
  type: "agent.artifact";
  run_id: string;
  response_id: string;
  timestamp: string;       // ISO 8601 UTC
  schema_version: "1";
  artifact: {
    id: string;
    revision: number;
    stage: "material_sources" | "material_conflicts" | "task_spec" |
           "outline" | "article_markdown" | "images" | "final_html";
    status: ArtifactStatus;
    content: TaskSpec | ImageArtifact[] | string[] | MaterialConflictArtifact | string;
    summary: object | null;
  };
};
```

所有 object 的字段均按下述定义处理。前端应允许未来新增可选字段，但不应在 stage 与类型不匹配时继续
渲染。artifact UI 主键使用 `${id}:${revision}:${stage}`。

#### 3.12.1 产品与 debug artifact 边界

产品流包含 `material_sources`、`material_conflicts`、`task_spec`、`outline`、`article_markdown`、`images`
和 `final_html`。其中 `material_conflicts` 只在系统发现会影响文章结论的事实冲突时出现。
`session_memory` 与 `material_library` 仅供本地开发面板观察：只有服务端 `DEBUG_ENABLED=true` 且请求
`context.debug=true` 时才可能返回。服务端总开关关闭时，请求字段不能越权开启 trace，终态响应中也
不会出现 `debug`。产品前端应忽略未来未知 stage，不应为这两个调试 stage 建立用户 UI。

#### 3.12.2 `material_sources`

素材调研全部完成后总会发送一次。`content` 严格是去重后的字符串数组：文档素材是文档名，不带页码；
网络素材是 Ark 搜索实际返回的原文 URL。数组可以为空，且不包含素材正文、summary、query 或 warning。

```json
{"type":"agent.artifact","run_id":"run_001","response_id":"resp_002","timestamp":"2026-08-20T08:00:50Z","schema_version":"1","artifact":{"id":"art_001","revision":1,"stage":"material_sources","status":"completed","content":["公司年度报告.pdf","https://example.com/official-report","https://example.com/reference-article"],"summary":{"source_count":3}}}
```

#### 3.12.3 `material_conflicts`

只有存在待处理事实冲突时才发送，且先于对应的 `agent.interrupt`。`sources[].excerpt` 是最多数百字的
定位摘录，不是完整素材；`path` 是可读出处，`ref` 对网络来源通常是 URL。

```ts
type MaterialConflictArtifact = {
  conflicts: Array<{
    message: string;
    sources: Array<{
      chunk_id?: string;
      path: string;
      ref?: string;
      excerpt?: string;
    }>;
  }>;
};
```

```json
{"type":"agent.artifact","run_id":"run_001","response_id":"resp_002","timestamp":"2026-08-20T08:00:55Z","schema_version":"1","artifact":{"id":"art_001","revision":1,"stage":"material_conflicts","status":"pending_review","content":{"conflicts":[{"message":"年度报告与网络报道的营业额口径不一致。","sources":[{"chunk_id":"chunk_001","path":"年度报告.pdf：第 12 页","ref":"doc-1","excerpt":"营业额为 12 亿元"},{"chunk_id":"chunk_002","path":"example.com","ref":"https://example.com/report","excerpt":"营业额为 15 亿元"}]}]},"summary":null}}
```

冲突审批仍通过 `POST /v1/responses` 恢复，三种合法 `selection.option_id` 是
`document_priority`、`automatic_authority`、`custom_feedback`；均使用 `decision=revise`。选择
`custom_feedback` 时 `feedback` 必填，其他两种允许为空。

#### 3.12.4 `task_spec`

任务书字段由 strict structured output 产生，六个字段全部存在；`other_requirements` 没有额外要求时为空数组。

```ts
type TaskSpec = {
  topic: string;
  audience: string;
  goal: string;
  tone: string;
  length: string;
  other_requirements: string[];
};
```

```json
{"type":"agent.artifact","run_id":"run_001","response_id":"resp_002","timestamp":"2026-08-13T08:01:00Z","schema_version":"1","artifact":{"id":"art_001","revision":1,"stage":"task_spec","status":"pending_review","content":{"topic":"制造业数字化转型的落地路径","audience":"制造企业管理者","goal":"说明关键路径、风险与行动建议","tone":"专业、清晰、审慎","length":"约 3000 字","other_requirements":["引用参考材料中的事实","避免夸大结论"]},"summary":null}}
```

#### 3.12.5 `outline`

`content` 是非空 Markdown 字符串，不是 `{markdown: string}` object。

```json
{"type":"agent.artifact","run_id":"run_001","response_id":"resp_003","timestamp":"2026-08-13T08:02:00Z","schema_version":"1","artifact":{"id":"art_001","revision":1,"stage":"outline","status":"pending_review","content":"# 制造业数字化转型：从目标到落地\n\n## 一、为什么现在必须行动\n\n## 二、三条落地路径","summary":null}}
```

#### 3.12.6 `article_markdown`

`content` 是非空 Markdown 正文字符串。它是未排版候选，不包含最终 HTML。

```json
{"type":"agent.artifact","run_id":"run_001","response_id":"resp_004","timestamp":"2026-08-13T08:03:00Z","schema_version":"1","artifact":{"id":"art_001","revision":1,"stage":"article_markdown","status":"pending_review","content":"# 制造业数字化转型：从目标到落地\n\n数字化转型不是单纯上线一套系统……","summary":null}}
```

#### 3.12.7 `images`

`content` 始终是数组，模型决定实际图片数但不会超过服务端上限；不需要图片或全部生成失败时可以为空。
只保存和返回 URL、caption 与 Markdown 插入位置，不返回图片二进制。

```ts
type ImageArtifact = {
  url: string;
  caption: string;
  insertion_position: {
    position_id: string;
    heading_path: string[];
    paragraph_ordinal: number;
  };
};
```

```json
{"type":"agent.artifact","run_id":"run_001","response_id":"resp_005","timestamp":"2026-08-13T08:04:00Z","schema_version":"1","artifact":{"id":"art_001","revision":1,"stage":"images","status":"completed","content":[{"url":"https://example-cdn.invalid/image-01.png","caption":"数字化转型实施路径示意图","insertion_position":{"position_id":"pos_003","heading_path":["制造业数字化转型：从目标到落地","二、三条落地路径"],"paragraph_ordinal":1}}],"summary":null}}
```

`status=degraded` 表示计划过图片但全部失败，系统仍继续交付无图 HTML；部分图片失败但仍有成功图片时，
当前事件状态为 `completed`，具体失败过程通过 activity/warning 表达。

#### 3.12.8 `final_html`

`content` 是完整 HTML string，不是 `{html, images, theme}` object。前端使用 sandboxed iframe 预览，并
可把原字符串作为 `.html` 文件下载；图片仍引用 `images` 中的 URL，由调用方按业务要求转存 OSS。

```json
{"type":"agent.artifact","run_id":"run_001","response_id":"resp_005","timestamp":"2026-08-13T08:04:05Z","schema_version":"1","artifact":{"id":"art_001","revision":1,"stage":"final_html","status":"completed","content":"<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"></head><body><article><h1>制造业数字化转型：从目标到落地</h1></article></body></html>","summary":null}}
```

若主 renderer 失败而最小安全主题成功，`status=degraded`，`content` 仍是可交付 HTML string。

## 4. `POST /v1/responses/{response_id}/cancel`

### 4.1 用途

正式停止当前后台 run，避免继续消耗模型或工具资源。关闭浏览器 SSE、调用 AbortController 或网络断开
不等于 cancel。

路径参数：

| 参数 | 类型 | 含义 |
|---|---|---|
| `response_id` | string | 当前控制该 workflow 的最新 Response ID |

请求体：

```json
{
  "context": {
    "session_id": "session-001"
  }
}
```

| 字段 | 类型 | 必填 | 含义 |
|---|---|---:|---|
| `context.session_id` | string | 是 | Response 所属 session，1 到 512 字符 |

### 4.2 已确认取消

```http
HTTP/1.1 200 OK
Content-Type: application/json
```

```json
{
  "id": "resp_001",
  "object": "response",
  "status": "cancelled",
  "run_id": "run_001",
  "artifact_id": "art_001"
}
```

### 4.3 正在取消

内部 runtime 已接受取消但服务端尚未确认终止时返回 HTTP 202：

```json
{
  "id": "resp_001",
  "object": "response",
  "status": "cancelling",
  "run_id": "run_001",
  "artifact_id": "art_001"
}
```

响应字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | string | 被取消的 Response ID |
| `object` | string | 固定为 `response` |
| `status` | string | `cancelled` 或 `cancelling` |
| `run_id` | string | 对应业务 run |
| `artifact_id` | string | 对应 artifact revision |

HTTP 202 时前端应暂时禁用该 session 的新请求和审批，并按退避重试同一个 cancel。重复取消已经
cancelled 的 Response 会幂等返回 HTTP 200。旧 Response 已不再控制 workflow 时返回 409
`STALE_RESPONSE`；已经正常完成或失败时返回 409 `RESPONSE_NOT_CANCELLABLE`。

取消操作不应写入 `input` 对话历史。取消后下一条用户消息通过主接口创建新 revision 并由 orchestrator
重新判断入口，不恢复旧 interrupt。

## 5. 健康检查

### 5.1 `GET /health/live`

只检查 adapter 进程能够响应：

```json
{"status":"ok"}
```

HTTP 200 表示进程存活，不代表 PostgreSQL、Agent Server 或模型服务均可用。

### 5.2 `GET /health/ready`

检查 adapter 已完成启动并能执行 PostgreSQL `SELECT 1`：

```json
{"status":"ready"}
```

HTTP 200 表示可以接收业务请求。该接口当前不探测 Ark、检索服务或内部 Agent Server 的完整业务链路。

## 6. 错误响应

在 SSE 建立前发生的错误使用普通 JSON，而不是 SSE：

```json
{
  "error": {
    "code": "STALE_INTERRUPT",
    "message": "The pending interaction is no longer current.",
    "retryable": false,
    "request_id": "req_001",
    "stage": "outline_review",
    "details": {}
  }
}
```

错误字段：

| 字段 | 类型 | 必有 | 含义 |
|---|---|---:|---|
| `code` | string | 是 | 稳定机器码；前端逻辑必须匹配该字段，而不是英文 message |
| `message` | string | 是 | 面向开发者的错误描述 |
| `retryable` | boolean | 是 | 当前错误是否可能通过稍后重试恢复 |
| `request_id` | string | 是 | 服务端请求追踪 ID |
| `stage` | string | 否 | 错误对应的工作流阶段 |
| `details` | object | 否 | 结构化上下文，例如当前 response ID 或校验详情 |

常见状态码：

| HTTP | code 示例 | 前端行为 |
|---:|---|---|
| 404 | `RESPONSE_NOT_FOUND` | 提示目标 Response 不存在 |
| 409 | `RUN_IN_PROGRESS` | 保持只读，稍后发送“继续”或重试 |
| 409 | `RUN_CANCELLING` | 禁用本 session 操作，等待取消完成 |
| 409 | `STALE_INTERRUPT` | 将旧审批卡标记过期，不自动重交 decision |
| 409 | `STALE_RESPONSE` | 当前 Response 已被新操作取代 |
| 409 | `RESPONSE_NOT_CANCELLABLE` | 当前 run 已终态，不能取消 |
| 422 | `INVALID_REQUEST` | 修正请求字段；`details.validation` 含校验信息 |
| 422 | `INTENT_TOPIC_REQUIRED` | 意图确认卡必须提交结构化 selection |
| 422 | `INVALID_INTENT_SELECTION` | 意图确认卡错误提交了素材调研方向字段 |
| 422 | `RESEARCH_DIRECTION_REQUIRED` | 文档澄清必须提交结构化 selection |
| 422 | `UNEXPECTED_RESEARCH_DIRECTION` | 非意图/文档澄清阶段不能提交 selection |
| 429 | `REQUEST_RATE_LIMITED` | 按退避策略稍后重试 |
| 503 | 数据库/准入错误 | 根据 `retryable` 提示稍后重试 |

SSE 建立后的失败使用 `response.failed`，其中 `response.error` 包含 `code/message/retryable/stage/details`
及可能的 `recovery_action`。随后仍发送 `[DONE]`。如果连接直接中断且没有收到 terminal event，只能判定
网络断线，不能自行把 workflow 标记为失败或取消。

`STALE_INTERRUPT` 的具体恢复步骤是：保留反馈草稿、将旧卡置为 stale、禁止自动重交 decision，然后由
用户触发一条不带 `previous_response_id/context.hitl` 的临时“继续”请求。该临时消息不进入永久历史；
前端根据重放的新/旧 interrupt、运行中错误或最终 artifact 更新 UI。不能把旧 feedback 自动应用到服务端
返回的另一个 interrupt。

当前没有无副作用状态查询接口，也没有显式 `SESSION_EXPIRED` 错误。artifact/checkpoint 过期后，提交旧卡
通常返回 `STALE_INTERRUPT`；不带 HITL 的请求可能作为新工作开始。前端页面恢复必须遵守
[启动恢复算法](frontend-integration.md#62-页面启动恢复算法)。

## 7. 连接、超时与重试契约

- 当前 SSE 不发送 heartbeat/comment，长模型或图片调用期间可能暂时没有事件；代理必须关闭响应缓冲。
- 不要为整条工作流设置短的总超时。当前最长单次模型/图片调用可达 600 秒，代理 idle/read timeout 建议
  至少 700 秒，生产可取 900 秒。
- `X-Request-ID` 只追踪一次 HTTP 尝试，不是 idempotency key。建议每次尝试生成新值；是否复用都不会去重。
- 除 cancel 的幂等轮询外，不要让 SDK 自动重试 `POST /v1/responses`。连接结果不明时发送“继续”恢复状态，
  不要原样重发可能已经被接受的 HITL decision。
- 建立 SSE 前收到 429/503 且 `retryable=true` 时可指数退避；收到 SSE 后断线不能仅凭网络错误判断 run 失败。

## 8. 前端实现检查清单

1. 每次请求传完整 `input` 历史，使用 `session_id` 关联会话。
2. 普通后续对话不传 `previous_response_id`；它只用于 HITL。
3. HITL 恢复仍调用 `POST /v1/responses`，同时提交准确的 Response ID 和 interrupt ID。
4. 原始解析三种 `agent.*` 事件，不假设 OpenAI SDK 一定保留未知事件。
5. 文本和 reasoning 按 delta 追加；activity、artifact 和 interrupt 按 ID upsert。
6. 按 `run_id -> response_id` 分组 UI，不能把新 revision 叠加到旧 run 的 activity/卡片中。
7. `response.completed` 后检查 `workflow_status`，不能仅看 HTTP/SSE 已结束。
8. 审批提交失败时保留 feedback 和卡片；只有收到新 `response.created` 才确认恢复已被接受。
9. 停止接收和正式 cancel 使用不同按钮与接口。
10. 最终 HTML 放入 sandboxed iframe，图片 URL 由调用方按业务要求转存 OSS。
11. 重复的 `response.created` 使用 get-or-create，重放文本不覆盖历史 Response。
12. 对话历史按规范映射；artifact/reasoning/activity/debug 不写回 `input`。
13. 页面刷新优先恢复本地候选和卡片，不无条件自动发具有副作用的“继续”。

事件渲染和 HITL 卡片的详细设计见 [前端接入指南](frontend-integration.md)，三个 `agent.*` 的跨 Agent
公共约束见 [Responses-like 扩展协议规范](responses-like-protocol.md)。
