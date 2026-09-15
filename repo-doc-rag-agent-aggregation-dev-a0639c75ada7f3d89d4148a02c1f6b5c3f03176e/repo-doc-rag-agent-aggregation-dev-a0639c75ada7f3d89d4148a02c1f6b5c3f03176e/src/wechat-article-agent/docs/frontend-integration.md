# 微信公众号文章 Agent 前端接入指南

## 1. 接入范围

微信公众号文章 Agent 对外只暴露产品 API，前端不需要、也不应访问内部 LangGraph Agent Server。
本地产品地址为 `http://127.0.0.1:8240`，生产默认端口为 `8140`。

前端需要接入两个业务接口：

```text
POST /v1/responses
POST /v1/responses/{response_id}/cancel
```

`POST /v1/responses` 同时负责首次生成、完成后的修改和 HITL 恢复。没有独立的 resume 接口。

本协议基于 OpenAI Responses 的流式事件风格，但包含项目 `context`、HITL 和三个 `agent.*` 扩展事件，
因此不能只依赖 OpenAI SDK 的标准事件类型完成全部功能。

### 1.1 产品术语与内部标识

当前产品把原“文档研读”阶段升级为“素材调研”。素材调研既可能读取用户文档，也可能理解网络背景、
搜索网络素材、整理历史素材并处理事实冲突。因此用户界面、进度标题、HITL 卡片和完成提示应统一使用
“素材调研”，不要继续把整个阶段称为“文档研读”。只有某个工具确实正在读取用户文档时，才显示“研读
用户文档”。

为保持协议兼容，内部标识暂不改名：事件仍可能包含 `docs_research`、`docs_research_clarification`、
`document_material_worker`。这些值用于 reducer 分派，不能作为用户文案直接显示，也不能由前端改写。
推荐显示映射如下：

| 内部标识 | 用户可见名称 |
|---|---|
| `docs_research` | 素材调研 |
| `docs_research_clarification` | 确认素材调研方向 |
| `document_material_worker` | 研读用户文档 |
| `web_material_worker` | 搜索网络素材 |
| `finalize_material_research` | 整理并完成素材调研 |

## 2. 前端应保存的状态

前端自己的数据库继续保存第 3.1 节定义的规范化完整对话历史。每个正在展示的 session 至少维护：

```ts
type AgentSessionState = {
  sessionId: string;
  latestResponseId?: string; // 当前可用于 cancel 的最新 Response
  workflowStatus?: string;
  runs: Map<string, AgentRunView>; // key: run_id
  interrupts: Map<string, HitlCardView>; // 保留已处理卡，key: interrupt.id
  pendingInterruptId?: string; // 当前唯一可操作卡
};

type HitlCardView = {
  interrupt: AgentInterrupt;
  sourceResponseId: string;
  lifecycle: "pending" | "submitting" | "resolved" | "stale" | "cancelled" | "unknown";
  feedbackDraft?: string;
};

type AgentRunView = {
  runId: string;
  artifactId: string;
  revision: number;
  responses: Map<string, AgentResponseView>; // key: response_id
  activities: Map<string, AgentActivity>;
  artifacts: Map<string, AgentArtifact>; // key: artifact.id + revision + stage
};

type AgentResponseView = {
  responseId: string;
  assistantText: string;
  reasoningText: string;
  status: string;
};

type StreamContext = {
  runId?: string;
  responseId?: string;
  isReplay: boolean;
  recoveryText: string; // 重放流的临时状态文案，不写回原 Response 正文
  submittedInterruptId?: string; // 仅显式 HITL 提交请求设置
};
```

不要把 `response_id` 当成会话 ID。一次完整 run 在每次 HITL 恢复后都会产生新的 `response_id`；
`session_id` 才是前端和服务端关联同一会话的稳定键。

建议把 UI 再按 `run_id -> response_id` 分组：一次业务 revision 对应一个 `run_id`，每次首次执行或
HITL 恢复对应一段新的 Response。新的 `run_id` 不能继续复用上一 run 的 activity、文本或审批卡；同一
`run_id` 内的新 `response_id` 则追加到当前 run 下方。这样完成后的 revise 不会和上一版结果叠在一起。

### 2.1 哪些 ID 由前端维护

前端只生成 `session_id`，服务端生成 `run_id`、`response_id`、`interrupt_id`、`artifact_id` 和
`revision`。推荐维护方式：

| ID | 保存位置 | 生命周期 | 是否回传 |
|---|---|---|---|
| `session_id` | 会话主记录 | 整个对话 | 每个业务请求都传 |
| `run_id` | run 展示记录 | 一个业务 revision | 不回传，用于 UI 分组 |
| `response_id` | Response 记录 | 一段 SSE Response | cancel 使用最新值；审批卡保存其来源值 |
| `previous_response_id` | 不单独建记录 | 单次 HITL 请求字段 | 复制当前卡片来源 `response_id` |
| `interrupt_id` | pending HITL 卡片 | 到审批、取消或 supersede | 提交该卡片时回传 |
| `artifact_id + revision` | artifact 和卡片记录 | 一个业务版本 | 当前请求不回传，用于产物/卡片关联 |

不要只在 session 上保存一个会被不断覆盖的 `responseId`。至少同时保存：

```ts
session.latestResponseId = event.response.id;       // 用于 cancel
card.sourceResponseId = event.response_id;          // 用于审批这张卡
card.interruptId = event.interrupt.id;
```

当用户操作卡片时：

```ts
request.previous_response_id = card.sourceResponseId;
request.context.hitl.interrupt_id = card.interruptId;
```

前端不能生成或修改服务端 ID。一次审批成功后，新流会产生新的 `response_id`，但同一 revision 的
`run_id/artifact_id/revision` 不变；完成后修改文章产生新 revision 时，这三个值会变化，而
`session_id` 不变。完整生命周期和每个 ID 的业务含义见 [API 参考第 2 节](api.md#2-id-的含义和前端责任)。

持久化建议：`session_id`、`latestResponseId` 和规范化对话历史必须持久化；为了页面刷新后仍能完成审批，pending 卡片的
`interrupt_id + sourceResponseId + runId + artifactId + revision + form + lifecycle + feedbackDraft` 以及它所
引用的候选 artifact 也应持久化。若不持久化候选 artifact，刷新后必须经过第 6.2 节的显式恢复流程才能
重新取得候选内容。不能把浏览器内存中的旧 ID 用于另一个 session。

## 3. 首次生成和完成后的修改

首次请求示例：

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
          "text": "请根据这些文档生成一篇微信公众号文章"
        }
      ]
    }
  ],
  "context": {
    "session_id": "frontend-session-1",
    "user_id": "user-1",
    "kb_id": "kb-1",
    "doc_ids": ["doc-1", "doc-2"],
    "temp_doc_ids": [],
    "debug": false
  }
}
```

前端每次都传入自己保存的规范化完整 `input` 历史。这里的“完整”是指第 3.1 节定义的聊天消息，不是把
SSE、artifact、reasoning 和工具日志全部塞回请求。服务端会压缩历史中的大型正文并提取仍有效的审批
偏好；前端不能依赖服务端永久保存完整聊天记录。

工作流完成后，用户提出“第三章增加一小节”等修改时，仍然发送同一种请求，但不要携带
`previous_response_id/context.hitl`。服务端会创建新 artifact revision，并根据需求选择最早需要重做
的阶段。

### 3.1 下一次 `input` 的确定性构造规则

前端数据库应保存“对话历史”和“运行视图”两套数据。只有对话历史进入下一次 `input`：

| 来源 | 是否进入 `input` | 角色与内容规则 |
|---|---:|---|
| 用户首次需求、普通修改意见 | 是 | 原文保存为一条 `user` |
| 非重放流的 `response.output_text.done.text` | 是 | 每个 `response_id` 最多一条 `assistant`；done 用于校准 delta，不重复追加 |
| HITL approve | 是 | 前端生成可读的 `user` 记录，如“我接受当前文章大纲。” |
| HITL revise/regenerate | 是 | 保存用户原始反馈为 `user`；不要保存 `task_spec_review` 等内部 stage 名 |
| 素材调研方向选择 | 是 | 保存可读结果，如“已确认素材调研方向：围绕……搜集素材，重点关注……。”；前端可由卡片选项生成 |
| `context.hitl.feedback/selection` | 当前请求必须 | 它负责本次结构化恢复；上面的 `user` 消息负责长期历史，二者有意同时存在 |
| `agent.artifact.content/summary` | 否 | artifact 单独持久化和展示；后端从业务表读取当前版本，不靠聊天历史还原 |
| reasoning、activity、工具/debug、SSE 事件 | 否 | 只属于运行视图 |
| 重放流的临时说明文本 | 否 | 重放使用旧 `response_id`，仅作状态提示，不覆盖旧 assistant 消息 |
| “继续/重试/恢复进度” | 否 | 作为本次请求的临时最后一条 `user` 发送，请求结束后不写入永久聊天历史 |
| cancel | 否 | 是控制操作，不是聊天消息 |

前端发送请求时，先复制持久化历史，再追加本次用户消息或临时恢复消息。HITL 请求同时把用户可读操作
追加到这份请求历史，并发送结构化 `context.hitl`；请求被服务端接受后再持久化该用户操作。若建立 SSE
前返回 4xx/5xx，不要把未被接受的操作固化为已完成审批记录，应保留为卡片草稿。

API 最多接受 500 条消息，服务端不会静默裁剪并替前端决定语义。接近上限时按以下优先级压缩：先移除
误存的运行状态和重放文案，再合并同一 Response 的碎片 assistant 文本，再将较早的普通 assistant 说明
汇总为一条明确标注的历史摘要；用户业务需求、HITL 选择和修改偏好优先保留。压缩后仍超过 500 条时，
前端必须在发请求前提示会话过长并完成自己的摘要/归档，不能依赖服务端接受超限请求。

一个跨三次审批的最小历史形状如下；任务书、大纲和正文全文均不在其中：

```json
[
  {"role":"user","content":"请根据参考文档生成一篇面向企业管理者的公众号文章。"},
  {"role":"assistant","content":"请确认本次素材调研方向。"},
  {"role":"user","content":"选择方向 B：围绕行业数字化转型，重点关注落地路径和风险。"},
  {"role":"assistant","content":"素材调研完成，已形成文章任务书，请审核。"},
  {"role":"user","content":"我接受当前文章任务书。"},
  {"role":"assistant","content":"已根据任务书生成文章大纲，请审核。"},
  {"role":"user","content":"请在第三章增加数据安全小节。"},
  {"role":"assistant","content":"已按反馈修改文章大纲，请审核。"},
  {"role":"user","content":"我接受当前文章大纲。"}
]
```

## 4. SSE 消费

### 4.1 OpenAI SDK 与扩展事件

标准 OpenAI SDK 可以帮助处理标准 Responses 事件，但本协议不是官方 Responses API 的完整实现。
特别是 `agent.activity`、`agent.artifact` 和 `agent.interrupt` 不属于官方事件集合，SDK 的强类型事件
分派可能忽略、归入 unknown，或在版本升级后改变处理方式。

推荐做法：

1. 保留现有 SDK 对标准文本和 reasoning 事件的处理；
2. 在 SDK 之下或并行增加原始 SSE frame 分派层，按 `event:` 或 JSON `type` 捕获三种 `agent.*`；
3. 若当前 SDK 不允许读取未知事件，则该接口直接使用 `fetch`/HTTP client 读取 SSE，再将标准事件送入
   现有 UI reducer；
4. 不要修改 SDK 包内部代码，也不要假设未知事件永远会被 SDK 原样透传；
5. 未识别事件必须忽略，不能导致整条流失败。

一个最小的浏览器 SSE reader：

```ts
async function readAgentSse(response: Response, onEvent: (type: string, data: any) => void) {
  if (!response.ok || !response.body) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      let eventType = "message";
      const dataLines: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) eventType = line.slice(6).trim();
        if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      const raw = dataLines.join("\n");
      if (!raw || raw === "[DONE]") continue;
      const payload = JSON.parse(raw);
      onEvent(eventType === "message" ? payload.type : eventType, payload);
    }
  }
}
```

生产实现还应处理跨 chunk UTF-8、CRLF、代理错误和请求 abort。上述代码用于说明事件分派，不是通用
SSE 库替代品。

### 4.2 标准事件的展示

当前 adapter 可能产生的标准 Responses-like 事件如下。标记为“必须”的事件需要进入产品 reducer；标记为
“可选”的生命周期事件仅在前端需要重建标准 Responses output item 时消费。

| 事件 | 前端要求 | 前端行为 |
| --- | --- | --- |
| `response.created` | 必须 | 保存 response/run/artifact/revision，进入运行态 |
| `response.output_item.added` | 可选 | 按 `item.id + output_index` 创建 message 或 reasoning item |
| `response.content_part.added` | 可选 | 创建 message 的 `output_text` part |
| `response.output_text.delta` | 必须 | 将 `delta` 追加到当前助手消息，形成打字机效果 |
| `response.reasoning_summary_part.added` | 可选 | 创建 reasoning 的 summary part |
| `response.reasoning_summary_text.delta` | 必须 | 追加到可折叠的“思考过程/分析摘要”区域 |
| `response.output_text.done` | 必须 | 用 `text` 校准并固化本轮助手文本，不能再次追加到 delta 后面 |
| `response.content_part.done` | 可选 | 完成 message content part |
| `response.reasoning_summary_text.done` | 建议 | 用 `text` 校准 reasoning 完整文本 |
| `response.reasoning_summary_part.done` | 可选 | 完成 reasoning summary part |
| `response.output_item.done` | 可选 | 将对应 message/reasoning item 标记 completed |
| `response.completed` | 必须 | 结束当前 SSE 片段，读取 `workflow_status` 判断是否还在等 HITL |
| `response.failed` | 必须 | 展示稳定错误和重试提示，不将失败误记为正常完成 |

每种内容第一次出现时，added 事件先于 delta；全部模型内容结束后发送各类 done。若本段产生 artifact 或
interrupt，它们在终态 `response.completed` 之前发送。最后一个 frame 是无 event 名的 `data: [DONE]`。
同一段流可能只有文本、只有 activity/artifact，也可能没有 reasoning，前端不能要求每种事件必然出现。

`response.completed` 不一定表示文章生成完成。必须读取：

```ts
const workflowStatus = event.response.metadata.workflow_status;
```

若值为 `waiting_for_input`，前端应保持 HITL 卡片可操作；只有 `completed` 才表示完整 workflow 已完成。

### 4.2.1 排版 Skill 活动

当排版配置为 `skill_driven` 时，服务端仍只使用既有的 `agent.activity` 事件，不新增事件名。前端按
`activity.kind` 区分：`node` 放入运行链路，`skill` 放入 Skill 活动，`tool` 放入工具活动。当前稳定的
排版名称包括：

| kind | name | 用户可见含义 |
|---|---|---|
| `skill` | `wechat_layout.skill_driven` | 使用排版 Agent 与 Skill |
| `skill` | `agent_engine.skill.<skill_id>` | 加载某个排版 Skill 资产 |
| `tool` | `agent_engine.analyze_markdown_layout` | 分析文章结构 |
| `tool` | `agent_engine.get_skill_theme_catalog` | 读取可用主题目录 |
| `tool` | `agent_engine.get_skill_component_catalog` | 读取主题组件目录 |
| `tool` | `agent_engine.validate_skill_layout_plan` | 校验富组件排版计划 |
| `tool` | `agent_engine.assemble_skill_html` | 装配主题组件 HTML |
| `tool` | `agent_engine.validate_skill_html` | 校验正文与图片一致性 |

开始和完成事件使用同一个 `activity.id`，因此必须原位 upsert，不能渲染为两条重复消息。产品界面只显示
`label/status/output_summary` 等安全摘要；只有开发面板在 debug 开启时才显示工具参数和结果。Skill 卡片
建议使用魔法棒或积木图标，工具卡片使用扳手或插件图标，均放在 `render_html` 节点活动下方或独立的
“工具/Skill 调用”区域，不能把它们误显示成新的工作流节点。

### 4.2.2 推荐的事件 reducer

所有事件先按 `run_id/response_id` 定位 Response，再执行下面的幂等更新。不要通过事件到达顺序推测
artifact 或 interrupt 的归属。

```ts
type StreamContext = {
  runId?: string;
  responseId?: string;
  isReplay: boolean;
  recoveryText: string;
  submittedInterruptId?: string;
};

function reduceAgentEvent(
  state: AgentSessionState,
  stream: StreamContext,
  type: string,
  event: any,
) {
  switch (type) {
    case "response.created": {
      const metadata = event.response.metadata;
      const previousLatestResponseId = state.latestResponseId;
      stream.runId = metadata.run_id;
      stream.responseId = event.response.id;
      state.latestResponseId = event.response.id;
      state.workflowStatus = metadata.workflow_status;
      const run = getOrCreateRun(state, {
        runId: metadata.run_id,
        artifactId: metadata.artifact_id,
        revision: metadata.revision,
      });
      const existed = run.responses.has(event.response.id);
      stream.isReplay =
        existed ||
        previousLatestResponseId === event.response.id ||
        metadata.workflow_status !== "running";
      getOrCreateResponse(run, event.response.id); // 已存在时绝不能清空旧文本
      if (stream.submittedInterruptId && !stream.isReplay) {
        const card = state.interrupts.get(stream.submittedInterruptId);
        if (card?.lifecycle === "submitting") {
          card.lifecycle = "resolved";
          if (state.pendingInterruptId === stream.submittedInterruptId) {
            state.pendingInterruptId = undefined;
          }
        }
      }
      break;
    }
    case "response.output_text.delta": {
      if (stream.isReplay) stream.recoveryText += event.delta ?? "";
      else requireResponse(state, stream).assistantText += event.delta ?? "";
      break;
    }
    case "response.reasoning_summary_text.delta": {
      if (!stream.isReplay) requireResponse(state, stream).reasoningText += event.delta ?? "";
      break;
    }
    case "agent.activity": {
      const run = requireRun(state, event.run_id ?? stream.runId);
      run.activities.set(event.activity.id, event.activity);
      break;
    }
    case "agent.artifact": {
      const run = requireRun(state, event.run_id ?? stream.runId);
      const artifact = event.artifact;
      run.artifacts.set(`${artifact.id}:${artifact.revision}:${artifact.stage}`, artifact);
      break;
    }
    case "response.output_text.done": {
      if (stream.isReplay) stream.recoveryText = event.text ?? stream.recoveryText;
      else requireResponse(state, stream).assistantText = event.text ?? "";
      break;
    }
    case "response.reasoning_summary_text.done": {
      if (!stream.isReplay) requireResponse(state, stream).reasoningText = event.text ?? "";
      break;
    }
    case "agent.interrupt": {
      const card = state.interrupts.get(event.interrupt.id) ?? {
        interrupt: event.interrupt,
        sourceResponseId: event.response_id ?? requireResponseId(stream),
        lifecycle: "pending" as const,
      };
      card.interrupt = event.interrupt;
      card.sourceResponseId = event.response_id ?? requireResponseId(stream);
      card.lifecycle = "pending"; // 服务端当前 pending 状态覆盖本地的未知/误判状态
      state.interrupts.set(event.interrupt.id, card);
      state.pendingInterruptId = event.interrupt.id;
      break;
    }
    case "response.completed":
    case "response.failed": {
      state.workflowStatus = event.response.metadata.workflow_status;
      requireResponse(state, stream).status = event.response.status;
      break;
    }
    default:
      // 协议允许增加标准可选事件；未知事件必须向前兼容地忽略。
      break;
  }
}
```

上例中的辅助函数由前端按自身状态库实现，不是服务端 API 或 OpenAI SDK 方法。每次 HTTP 请求创建一个
独立 `StreamContext`。普通请求初始化 `isReplay=false/recoveryText=""`；显式 HITL 提交还应把被提交的
`interrupt_id` 放进 `submittedInterruptId`，并在发送前将卡片置为 `submitting`。服务端 V1 不发送
`interrupt.status=resolved`，因此只有该 HITL 请求收到一个**新的** `response.created` 后，旧卡才转为
`resolved`。重放流使用旧 `response_id`，不能据此误判审批已接受。

标准的 `response.output_item.*`、`response.content_part.*` 和 reasoning part 事件用于描述完整 Responses
item 生命周期。只做打字机 UI 时可以以 delta 为准；如果前端需要持久化标准 Responses item，则同时按
`item_id + output_index` 维护 item，不能把 delta 和 done 内容重复拼接。

### 4.3 `agent.activity`

使用 `activity.id` 作为 key 原位更新，不要把每个 running/completed 事件渲染为两条日志。

完整 SSE 示例：

```text
event: agent.activity
data: {"type":"agent.activity","run_id":"run_01","response_id":"resp_01","timestamp":"2026-08-13T08:00:00Z","schema_version":"1","activity":{"id":"activity_route_documents_01","kind":"tool","name":"route_documents","label":"筛选相关参考文档","status":"running","node":"docs_research","input_summary":{"document_count":6},"output_summary":null,"error":null}}

```

工具完成后，同一个 `activity.id` 会再次出现：

```json
{
  "type": "agent.activity",
  "run_id": "run_01",
  "response_id": "resp_01",
  "timestamp": "2026-08-13T08:00:02Z",
  "schema_version": "1",
  "activity": {
    "id": "activity_route_documents_01",
    "kind": "tool",
    "name": "route_documents",
    "label": "筛选相关参考文档",
    "status": "completed",
    "node": "docs_research",
    "input_summary": null,
    "output_summary": {"selected_count": 3, "warning_count": 0},
    "error": null
  }
}
```

建议视觉状态：

- `running`：旋转进度图标和当前动作；
- `completed`：完成图标，弱化展示；
- `degraded`：警告色并展示降级原因，但不误报为整体失败；
- `failed`：错误状态和可读错误；
- `cancelled`：明确显示已停止。

默认只展示 `label`、status 和经过裁剪的摘要。`name/node` 用于程序判断或开发模式，不应直接作为面向
用户的文案。activity 是工作流轨迹，不是聊天消息。

联网素材搜索使用同一契约，`name` 为 `ark.web_search`。前端应把它作为工具卡展示，不能把它当成新的
工作流节点，也不能展示 Ark 的原始 query、annotations 或网页摘要。典型开始事件如下：

```json
{"type":"agent.activity","run_id":"run_01","response_id":"resp_02","timestamp":"2026-08-20T08:00:20Z","schema_version":"1","activity":{"id":"activity_web_material_01","kind":"tool","name":"ark.web_search","label":"搜索网络写作素材","status":"running","node":"web_material_worker","input_summary":{"categories":4},"output_summary":null,"error":null}}
```

完成时会用同一个 `activity.id` 更新为 `completed` 或 `degraded`，`output_summary` 只包含
`material_count/query_count` 等安全计数。`degraded` 表示联网素材不可用但工作流可继续，不等于整次
Response 失败。

### 4.4 `agent.artifact`

使用 `artifact.id + artifact.revision + artifact.stage` 作为 key 原位更新：

完整 SSE 示例：

```text
event: agent.artifact
data: {"type":"agent.artifact","run_id":"run_01","response_id":"resp_02","timestamp":"2026-08-13T08:01:00Z","schema_version":"1","artifact":{"id":"art_01","revision":1,"stage":"outline","status":"pending_review","content":"# 年报解读\n\n## 一、经营表现\n\n## 二、风险与未来看点","summary":null}}

```

对应 JSON 结构：

```ts
type AgentArtifactEvent = {
  type: "agent.artifact";
  run_id: string;
  response_id: string;
  timestamp: string;
  schema_version: "1";
  artifact: {
    id: string;
    revision: number;
    stage: string;
    status: "pending_review" | "completed" | "degraded" | string;
    content: AgentArtifactContent;
    summary: object | null;
  };
};

type MaterialConflictArtifact = {
  conflicts: Array<{
    message: string;
    sources: Array<{chunk_id?: string; path: string; ref?: string; excerpt?: string}>;
  }>;
};

type AgentArtifactContent = TaskSpec | ImageArtifact[] | string[] |
  MaterialConflictArtifact | string;
```

非 debug 产品流会发送以下七种 stage：

| stage | `content` 类型 | `summary` 类型 | 用途 |
|---|---|---|---|
| `material_sources` | `string[]` | object/null | 素材调研完成后的文档名和可点击网页 URL |
| `material_conflicts` | `MaterialConflictArtifact` | object/null | 需要用户处理的事实冲突及其可读出处 |
| `task_spec` | `TaskSpec` object | `null` | 任务书候选 |
| `outline` | Markdown string | `null` | 大纲候选 |
| `article_markdown` | Markdown string | `null` | 正文候选 |
| `images` | `ImageArtifact[]` | `null` | 已生成图片，允许空数组 |
| `final_html` | HTML string | `null` | 最终交付物 |

完整 TypeScript 定义、字段是否必有和每种真实 JSON 示例见
[API 参考 3.12 节](api.md#312-agentartifact-的应用级数据契约)。前端不得根据同一个 `content: unknown`
盲猜类型；应先按 `stage` 分派，再校验对应结构。新素材事件可以带轻量计数摘要，既有业务产物的
`material_sources.summary` 当前为 `{ "source_count": number }`，其余既有业务产物的 `summary` 为
`null`。`material_sources.content` 严格是字符串数组：非 URL 字符串按文档名展示，
`http://` 或 `https://` 字符串渲染为安全外链，不展示素材正文、页码或检索 query。
`material_conflicts.content.conflicts` 应渲染为来源对照卡，不能把 `excerpt` 当作完整原文。
`session_memory` 和 `material_library` 是本地开发面板专用 artifact，只有服务端
`DEBUG_ENABLED=true` 且请求 `context.debug=true` 时才可能出现；产品前端不应渲染。服务端 debug
关闭时，即使调用方传入 `context.debug=true`，也不会返回这两个 stage 或终态 `debug` 字段。

`pending_review` 产物应紧邻对应 HITL 卡片。Markdown 使用受控 renderer；最终 HTML 建议放在 sandboxed
iframe 中，避免产物样式或脚本影响主页面。不要直接将未经隔离的 HTML 注入产品 DOM。

### 4.5 `agent.interrupt`

interrupt 到达后保存：

```ts
const card: HitlCardView = {
  interrupt: event.interrupt,
  sourceResponseId: event.response_id,
  lifecycle: "pending",
};
state.interrupts.set(event.interrupt.id, card);
state.pendingInterruptId = event.interrupt.id;
```

按 `interrupt.form.form_type` 渲染：

#### 4.5.1 公众号文章意图确认：`agent_clarification`

当 `form.clarification_type=intent_confirmation` 时，将 A/B/C 渲染为文章主题单选卡，并提供“其他”
主题输入框：

```text
event: agent.interrupt
data: {"type":"agent.interrupt","run_id":"run_01","response_id":"resp_intent_01","timestamp":"2026-08-17T08:00:10Z","schema_version":"1","interrupt":{"id":"int_intent_01","status":"pending","stage":"intent_clarification","artifact_id":"art_01","revision":1,"form":{"form_type":"agent_clarification","clarification_type":"intent_confirmation","title":"确认公众号文章生成方向","description":"请选择一个建议主题，或填写其他主题。","selection_mode":"single","options":[{"id":"A","topic":"人工智能如何改变日常工作"},{"id":"B","topic":"普通人使用人工智能的实用方法"},{"id":"C","topic":"人工智能应用中的机会与边界"}],"custom_option":{"enabled":true,"topic_label":"其他文章主题"},"fields":[]}}}
```

推荐显示文案为“围绕「{topic}」生成微信公众号文章”。提交 A/B/C 时只发送
`selection: {"option_id":"A"}`；提交其他主题时发送：

```json
{
  "interrupt_id": "int_intent_01",
  "decision": "revise",
  "feedback": "",
  "selection": {
    "option_id": "custom",
    "topic": "乡村教育数字化"
  }
}
```

推荐项会直接进入文章工作流；自定义项最多再判断一次意图，不会返回第二张意图确认卡。

#### 4.5.2 素材调研方向确认：`agent_clarification`

完整 SSE 响应示例：

```text
event: agent.interrupt
data: {"type":"agent.interrupt","run_id":"run_01","response_id":"resp_01","timestamp":"2026-08-13T08:00:10Z","schema_version":"1","interrupt":{"id":"int_clarification_01","status":"pending","stage":"docs_research_clarification","artifact_id":"art_01","revision":1,"form":{"form_type":"agent_clarification","clarification_type":"research_direction","title":"请确认素材搜集方向","description":"请选择一个素材方向，或填写自定义的搜集主题与关注方面。","selection_mode":"single","options":[{"id":"A","about":"经营数据与发展背景","target":"银行经营表现"},{"id":"B","about":"不良贷款、拨备覆盖和风险变化","target":"银行资产质量"},{"id":"C","about":"客户增长、产品结构和经营策略","target":"银行零售业务"}],"custom_option":{"enabled":true,"about_label":"素材关注方面","target_label":"素材搜集主题"},"fields":[]}}}

```

格式化后的响应体：

```json
{
  "type": "agent.interrupt",
  "run_id": "run_01",
  "response_id": "resp_01",
  "timestamp": "2026-08-13T08:00:10Z",
  "schema_version": "1",
  "interrupt": {
    "id": "int_clarification_01",
    "status": "pending",
    "stage": "docs_research_clarification",
    "artifact_id": "art_01",
    "revision": 1,
    "form": {
      "form_type": "agent_clarification",
      "clarification_type": "research_direction",
      "title": "请确认素材搜集方向",
      "description": "请选择一个素材方向，或填写自定义的搜集主题与关注方面。",
      "selection_mode": "single",
      "options": [
        {
          "id": "A",
          "about": "经营数据与发展背景",
          "target": "银行经营表现"
        },
        {
          "id": "B",
          "about": "不良贷款、拨备覆盖和风险变化",
          "target": "银行资产质量"
        },
        {
          "id": "C",
          "about": "客户增长、产品结构和经营策略",
          "target": "银行零售业务"
        }
      ],
      "custom_option": {
        "enabled": true,
        "about_label": "素材关注方面",
        "target_label": "素材搜集主题"
      },
      "fields": []
    }
  }
}
```

`target` 表示素材主题，即围绕什么对象、事件或问题搜集素材；`about` 表示关注重点，即重点查找该主题
的哪些事实、观点、案例或表达方式。`target` 不是底层搜索 query，也不必等于最终文章标题；`about` 不是
文章受众、语气或目标，这些创作要求由后续任务书节点确认。

推荐渲染为四张单选卡。产品界面建议使用：

- 标题：`确认素材调研方向`；
- 说明：`请选择本次要搜集的素材主题和重点关注方向。确认后，系统将据此筛选用户文档，并在需要且可用时补充网络素材。`；
- A/B/C 主文案：`围绕「{target}」搜集素材`；
- A/B/C 辅助文案：`重点关注：{about}`；
- 第四项：`其他：自定义素材调研方向`；
- 提交按钮：`确认并开始调研`。

选择“其他”时展开“素材主题”和“重点关注”两个输入框。点击 A/B/C 后服务端直接开始素材调研；自定义
输入可能被模型判定为仍然含糊，此时会返回一张新的同结构卡片。达到 `CLARIFICATION_MAX_ROUNDS` 后
服务端采用最后一次自定义输入并强制
开始素材调研，不再追问。

卡片交互建议：

- 默认不选中任何项，“确认方向”按钮禁用；
- A/B/C 整张卡可点击并允许文案自然换行，必须能查看 about 和 target 的完整内容；
- “其他”选中后才展示两个必填文本框，并保留用户尚未成功提交的草稿；
- 提交期间只禁用当前卡片，不提前从本地删除 interrupt；
- 收到新流的 `response.created` 后再把旧卡标记为已处理；HTTP 409/422 时恢复按钮和草稿；
- 服务端再次返回同一类型的澄清卡时，按新的 `interrupt.id` 新建卡，不覆盖用户此前已处理卡的展示记录。

选择推荐项时只回传 `option_id`，不要回传或覆盖服务端选项正文：

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "previous_response_id": "resp_01",
  "input": [
    {
      "role": "user",
      "content": "已确认素材调研方向：围绕‘银行资产质量’搜集素材，重点关注‘不良贷款、拨备覆盖和风险变化’。"
    }
  ],
  "context": {
    "session_id": "frontend-session-1",
    "user_id": "user-1",
    "kb_id": "kb-1",
    "doc_ids": ["doc-1"],
    "temp_doc_ids": [],
    "debug": false,
    "hitl": {
      "interrupt_id": "int_clarification_01",
      "decision": "revise",
      "feedback": "",
      "selection": {"option_id": "B"}
    }
  }
}
```

成功提交后，建议把用户消息规范化为：

```text
已确认素材调研方向：围绕「{target}」搜集素材，重点关注「{about}」。
```

不要在产品对话中显示“接受 docs_research”“选择 option B”或 `decision=revise` 等内部术语。

自定义项提交格式：

```json
{
  "interrupt_id": "int_clarification_01",
  "decision": "revise",
  "feedback": "",
  "selection": {
    "option_id": "custom",
    "about": "小微企业服务模式和风险控制措施",
    "target": "区域银行的普惠金融业务"
  }
}
```

#### 4.5.3 产物审批：`agent_artifact_review`

`agent.artifact` 会先发送候选产物，随后发送审批 interrupt。完整 SSE 响应示例：

```text
event: agent.interrupt
data: {"type":"agent.interrupt","run_id":"run_01","response_id":"resp_02","timestamp":"2026-08-13T08:01:01Z","schema_version":"1","interrupt":{"id":"int_outline_01","status":"pending","stage":"outline_review","artifact_id":"art_01","revision":1,"form":{"form_type":"agent_artifact_review","title":"请审核文章大纲","description":"接受后将进入下一阶段；也可以重新生成或按反馈修改。","fields":[]}}}

```

格式化后的响应体：

```json
{
  "type": "agent.interrupt",
  "run_id": "run_01",
  "response_id": "resp_02",
  "timestamp": "2026-08-13T08:01:01Z",
  "schema_version": "1",
  "interrupt": {
    "id": "int_outline_01",
    "status": "pending",
    "stage": "outline_review",
    "artifact_id": "art_01",
    "revision": 1,
    "form": {
      "form_type": "agent_artifact_review",
      "title": "请审核文章大纲",
      "description": "接受后将进入下一阶段；也可以重新生成或按反馈修改。",
      "fields": []
    }
  }
}
```

前端必须用 `artifact_id + revision` 找到同一版本，再根据 stage 将 `outline_review` 关联到 outline
候选。审批卡提供：

- 显示对应 `artifact_id/revision/stage` 的候选产物；
- 提供“接受”“按反馈修改”“完全重生成”三个动作；
- `revise` 时反馈必填；
- 提交后立即禁用按钮，防止重复审批。

审批卡应与候选 artifact 放在同一视觉分组内。任务书使用字段化只读视图，大纲和正文使用 Markdown
预览；正文较长时默认折叠或限制预览高度。接受、按反馈修改、完全重新生成必须是明确的三个动作，
不要把 regenerate 伪装成空 feedback 的 revise。

三种 decision 的 `context.hitl` 分别为：

```json
{"interrupt_id":"int_outline_01","decision":"approve","feedback":""}
```

```json
{"interrupt_id":"int_outline_01","decision":"revise","feedback":"第三章增加一个风险提示小节"}
```

```json
{"interrupt_id":"int_outline_01","decision":"regenerate","feedback":"换一种更紧凑的组织方式"}
```

这三个对象都放入下一次 `POST /v1/responses` 的 `context.hitl`，并同时携带
`previous_response_id=resp_02`。完整 revise 请求见第 5 节。

文章 Agent 的三种产物审批拥有相同 JSON shape，只是 stage、标题和关联 artifact stage 不同。前端应
覆盖下面三个实际响应体，而不是只硬编码大纲。

任务书审批：

```json
{
  "type": "agent.interrupt",
  "run_id": "run_01",
  "response_id": "resp_task_spec_01",
  "timestamp": "2026-08-13T08:00:40Z",
  "schema_version": "1",
  "interrupt": {
    "id": "int_task_spec_01",
    "status": "pending",
    "stage": "task_spec_review",
    "artifact_id": "art_01",
    "revision": 1,
    "form": {
      "form_type": "agent_artifact_review",
      "title": "请审核文章任务书",
      "description": "接受后将进入下一阶段；也可以重新生成或按反馈修改。",
      "fields": []
    }
  }
}
```

大纲审批：

```json
{
  "type": "agent.interrupt",
  "run_id": "run_01",
  "response_id": "resp_outline_01",
  "timestamp": "2026-08-13T08:01:01Z",
  "schema_version": "1",
  "interrupt": {
    "id": "int_outline_01",
    "status": "pending",
    "stage": "outline_review",
    "artifact_id": "art_01",
    "revision": 1,
    "form": {
      "form_type": "agent_artifact_review",
      "title": "请审核文章大纲",
      "description": "接受后将进入下一阶段；也可以重新生成或按反馈修改。",
      "fields": []
    }
  }
}
```

正文审批：

```json
{
  "type": "agent.interrupt",
  "run_id": "run_01",
  "response_id": "resp_article_01",
  "timestamp": "2026-08-13T08:02:30Z",
  "schema_version": "1",
  "interrupt": {
    "id": "int_article_01",
    "status": "pending",
    "stage": "article_review",
    "artifact_id": "art_01",
    "revision": 1,
    "form": {
      "form_type": "agent_artifact_review",
      "title": "请审核未排版文章",
      "description": "接受后将进入下一阶段；也可以重新生成或按反馈修改。",
      "fields": []
    }
  }
}
```

#### 4.5.4 素材冲突审批：`agent_artifact_review`

当 `form.review_type=material_conflict` 时，不使用普通产物的接受/修改/重生成按钮。服务端先发送
`stage=material_conflicts` 的来源对照 artifact，随后发送以下 interrupt：

```json
{
  "type": "agent.interrupt",
  "run_id": "run_01",
  "response_id": "resp_conflict_01",
  "timestamp": "2026-08-20T08:01:01Z",
  "schema_version": "1",
  "interrupt": {
    "id": "int_conflict_01",
    "status": "pending",
    "stage": "material_conflict_review",
    "artifact_id": "art_01",
    "revision": 1,
    "form": {
      "form_type": "agent_artifact_review",
      "review_type": "material_conflict",
      "title": "请选择素材冲突的处理方式",
      "description": "系统发现会影响文章结论的素材冲突，请选择判断依据。",
      "selection_mode": "single",
      "options": [
        {"id": "document_priority", "label": "以我提供的文档为准"},
        {"id": "automatic_authority", "label": "按来源权威性自动判断"},
        {"id": "custom_feedback", "label": "按我的补充意见处理"}
      ],
      "custom_option": {"enabled": true, "feedback_label": "补充处理意见"},
      "fields": [
        {
          "message": "用户年度报告与网络报道的营业额口径不一致。",
          "sources": [
            {"chunk_id": "chunk_001", "path": "年度报告.pdf：第 12 页", "ref": "doc-1", "excerpt": "营业额为 12 亿元"},
            {"chunk_id": "chunk_002", "path": "example.com", "ref": "https://example.com/report", "excerpt": "营业额为 15 亿元"}
          ]
        }
      ]
    }
  }
}
```

前端按 `fields[].sources` 展示出处和短摘录，渲染三个单选项；只有选择 `custom_feedback` 时才展开并
要求填写反馈。三种提交的 `context.hitl` 分别是：

```json
{"interrupt_id":"int_conflict_01","decision":"revise","feedback":"","selection":{"option_id":"document_priority"}}
```

```json
{"interrupt_id":"int_conflict_01","decision":"revise","feedback":"","selection":{"option_id":"automatic_authority"}}
```

```json
{"interrupt_id":"int_conflict_01","decision":"revise","feedback":"请以 2025 年经审计年报的合并口径为准","selection":{"option_id":"custom_feedback"}}
```

这三种处理都使用 `decision=revise`，并与普通 HITL 一样在主接口携带产生该 interrupt 的
`previous_response_id`。不要提交 `approve`，也不要自行把用户选择转换成文章修改意见。

关联表：

| interrupt stage | artifact stage | 前端内容 |
| --- | --- | --- |
| `intent_clarification` | 无候选 artifact | 一次性公众号文章意图确认表单 |
| `docs_research_clarification` | 无强制候选 artifact | 素材调研方向表单 |
| `material_conflict_review` | `material_conflicts` | 来源对照和冲突处理单选表单 |
| `task_spec_review` | `task_spec` | 结构化任务书 |
| `outline_review` | `outline` | Markdown 大纲 |
| `article_review` | `article_markdown` | Markdown 正文 |

## 5. HITL 恢复

用户点击审批后，继续调用主接口：

```http
POST /v1/responses
```

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "previous_response_id": "resp_previous",
  "input": [
    {"role": "user", "content": "把第二节拆成两个小节"}
  ],
  "context": {
    "session_id": "frontend-session-1",
    "user_id": "user-1",
    "kb_id": "kb-1",
    "doc_ids": ["doc-1", "doc-2"],
    "temp_doc_ids": [],
    "debug": false,
    "hitl": {
      "interrupt_id": "int_current",
      "decision": "revise",
      "feedback": "把第二节拆成两个小节"
    }
  }
}
```

关键规则：

- `previous_response_id` 使用产生当前卡片的 Response ID；
- `interrupt_id` 使用卡片内的 `interrupt.id`；
- 两者不是同一个 ID；
- approve 的 feedback 可以为空；artifact review 的 revise 必须有 feedback；意图确认和文档方向
  clarification 的 revise 必须有 selection；regenerate 可附带补充意见；
- 服务端接受后会通过新流返回新的 `response_id`，前端需更新当前 Response；
- HTTP 409 `STALE_INTERRUPT` 表示卡片已过期，不能把旧 feedback 自动提交到新卡片。

点击审批后的前端状态机建议为：

```text
pending -> submitting -> resolved
                    \-> rejected_by_server -> pending
                    \-> disconnected -> unknown
```

`resolved` 的判定点是恢复请求已经收到新的 `response.created`，不是 fetch 调用刚发出。若在收到任何
响应前断线，应保留旧卡但标记“结果待确认”，随后让用户发送“继续”触发服务端按真实 checkpoint
重放或报告运行中；不要自动重复提交同一个 decision。

更完整的卡片生命周期为：

```text
agent.interrupt -> pending
点击提交 -> submitting
该提交流收到新 response.created -> resolved
提交前 HTTP 失败 -> pending（保留 feedbackDraft）
提交后连接结果不明 -> unknown（保留 feedbackDraft）
STALE_INTERRUPT -> stale（不自动把 decision 提交给新卡）
cancel 200 -> cancelled
后续新 agent.interrupt -> 按新 interrupt_id 创建/更新当前 pending 卡
```

旧卡可以保留在历史 run 中，但只有 `pendingInterruptId` 指向的卡片可操作。新卡到达时替换当前 pending
指针，不删除已处理卡，也不复用旧卡反馈。服务端当前不会额外发 resolved interrupt 事件。

## 6. “继续”、断线和卡片重放

关闭或中断 SSE 不会取消后台 run。重新发送完整历史且最新用户消息为“继续”时，服务端按当前状态处理：

- run 仍执行：HTTP 409 `RUN_IN_PROGRESS`，前端展示“后台仍在运行”，稍后再试；
- 正等待 HITL：复用原 `response_id/interrupt_id` 重发产物和卡片，前端按 ID upsert；
- 已完成：重发最终 artifact；
- 已取消：说明上次运行已取消，不恢复旧 checkpoint；
- 可重试失败：只有用户明确要求“继续/重试”才从失败 checkpoint 重试。

V1 不重放断线期间错过的 token、reasoning 和临时 activity。前端不能依赖事件序号请求缺失片段。

### 6.1 pending 重放的完整 SSE 顺序

重放仍是完整 SSE 流，并且**允许再次发送旧 `response_id` 的 `response.created`**。典型顺序如下：

```text
event: response.created
data: {"type":"response.created","response":{"id":"resp_old","metadata":{"run_id":"run_01","artifact_id":"art_01","revision":1,"workflow_status":"waiting_for_input","pending_interrupt_id":"int_old"},...}}

event: response.output_item.added
data: {"type":"response.output_item.added","response_id":"resp_old",...}

event: response.content_part.added
data: {"type":"response.content_part.added","response_id":"resp_old",...}

event: response.output_text.delta
data: {"type":"response.output_text.delta","response_id":"resp_old","delta":"已重新展示当前待确认内容。",...}

event: agent.artifact
data: {"type":"agent.artifact","response_id":"resp_old","artifact":{"id":"art_01","revision":1,"stage":"outline","status":"pending_review","content":"# ...","summary":null},...}

event: agent.interrupt
data: {"type":"agent.interrupt","response_id":"resp_old","interrupt":{"id":"int_old","status":"pending","stage":"outline_review","artifact_id":"art_01","revision":1,"form":{...}},...}

event: response.output_text.done
data: {"type":"response.output_text.done","response_id":"resp_old","text":"已重新展示当前待确认内容。",...}

event: response.content_part.done
data: {"type":"response.content_part.done","response_id":"resp_old",...}

event: response.output_item.done
data: {"type":"response.output_item.done","response_id":"resp_old",...}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_old","status":"completed","metadata":{"workflow_status":"waiting_for_input","pending_interrupt_id":"int_old",...}},...}

data: [DONE]
```

说明：临时说明文本存在时才有文本 added/delta/done；`docs_research_clarification` 重放没有候选 artifact，
因此可能是 `response.created -> 临时文本 -> agent.interrupt -> 文本 done -> response.completed -> [DONE]`。
重放事件没有持久化 sequence，不能用它恢复此前丢失的 activity 或 token。

reducer 必须执行以下幂等规则：

- `response.created` 对 Response 使用 `getOrCreate`；事件 ID 等于请求前持久化的 `latestResponseId`、该
  Response 已存在，或 created 时 `workflow_status` 已不是 `running`，均将当前流标记为 replay，禁止清空旧文本；
- replay 的 `output_text` 放在临时“恢复状态”提示中，不覆盖/追加到旧 assistant 消息，也不写入对话历史；
- artifact 和 interrupt 均按稳定复合 key upsert；
- replay 不创建新的 run/Response 聊天气泡，不把旧审批卡标为 resolved；
- 最终结果重放同样复用旧 ID，区别是发送 `final_html` 且没有 interrupt。

### 6.2 页面启动恢复算法

V1 没有 GET 状态或事件重放接口，且 `POST /v1/responses.input` 至少需要一条消息。页面启动按下面顺序：

1. 从前端数据库恢复 session、规范化历史、run、pending 卡和候选 artifact。
2. 候选和卡片完整时直接本地渲染，不自动请求服务端；用户提交时服务端仍会做 stale 校验。
3. 已完成且本地保存了 `final_html` 时直接渲染，不请求服务端。
4. 只有本地内容缺失、上次连接结果为 unknown，或用户主动点击“恢复进度”时，发送不带
   `previous_response_id/context.hitl` 的临时“继续”请求；该消息不进入持久化历史和可见聊天记录。
5. 不要在每次刷新时无条件自动发“继续”。如果服务端状态实际是失败或已取消，它可能触发失败重试或由
   orchestrator 创建新 revision，属于有副作用的业务操作。
6. 返回 `RUN_IN_PROGRESS` 时保持恢复中并由用户稍后重试；返回 pending/final 重放时按旧 ID upsert；
   返回新 `run_id` 表示已经开始新 revision，建立新 run 视图。

artifact/checkpoint 采用三天滑动 TTL。服务端 V1 没有独立 `SESSION_EXPIRED` 状态：过期后旧 artifact 无法
重放；提交旧卡通常得到 `STALE_INTERRUPT`，而不带 HITL 的新请求会被当作该 session 的新工作开始。
因此前端不应承诺三天后仍可恢复旧候选或最终 HTML。

### 6.3 `STALE_INTERRUPT` 的确定恢复动作

```text
STALE_INTERRUPT
  -> 保留 feedbackDraft
  -> 将旧卡标记 stale 并禁用
  -> 不替换 interrupt_id，不自动重交 decision
  -> 用户点击“刷新当前状态”后发送临时“继续”（无 previous_response_id/context.hitl）
  -> 按返回结果 upsert 当前卡、显示运行中、重发最终产物或进入新 revision
```

这条临时“继续”不进入对话历史。若服务端返回一个新 interrupt，展示新卡；用户确认后才能把旧草稿手动
带入新卡，前端不能自动将旧 decision 应用到新 interrupt。

## 7. 正式取消

```http
POST /v1/responses/{response_id}/cancel
Content-Type: application/json
```

```json
{
  "context": {
    "session_id": "frontend-session-1"
  }
}
```

“停止接收”和“取消 run”必须是两个不同操作：

- 停止接收：abort 当前 HTTP/SSE，只影响浏览器；
- 取消 run：调用 cancel endpoint，要求后台停止模型、工具和后续 artifact 生成。

HTTP 200 表示已确认 cancelled；HTTP 202 表示正在取消，此时禁用该 session 的发送和审批，并按退避
策略重试同一个 cancel；HTTP 409 表示 Response 已不是可取消的当前运行。重复取消已经 cancelled 的
Response 是幂等的。

cancel 不应作为用户聊天消息写入 `input`。取消后用户发送真实的新修改意见时，服务端会创建新
revision；只发送“继续”不会复活已取消 run。

## 8. 错误处理

建立 SSE 前的错误是普通 JSON：

```json
{
  "error": {
    "code": "RUN_IN_PROGRESS",
    "message": "...",
    "retryable": true,
    "request_id": "req_01",
    "stage": null,
    "details": {}
  }
}
```

前端应以 `error.code` 决定行为，而不是匹配英文 message：

- `RUN_IN_PROGRESS/RUN_CANCELLING`：保持会话只读并允许稍后重试；
- `STALE_INTERRUPT`：按第 6.3 节将旧卡置为 stale，保留草稿；只在用户触发后发送无 HITL 的临时“继续”，
  不自动重复 decision；
- `STALE_RESPONSE`：停止对旧 Response 的 cancel/操作，使用当前 session 的最新本地状态；需要恢复时走
  第 6.2 节，不得猜测新的 Response ID；
- `REQUEST_RATE_LIMITED`：按 retryable 提示稍后重试；
- `DOCUMENT_NOT_FOUND/DOCUMENTS_REQUIRED`：要求调用方修正文档范围；
- 其他不可重试错误：保留已经展示的 artifact 和 trace，不自动无限重跑。

流已经开始后的失败使用 `response.failed`，最后仍会出现 `[DONE]`。网络异常没有 terminal event 时，
按“断线”处理，不能擅自判断 workflow 已失败。

## 9. 安全与展示建议

1. `agent.activity` 中只展示服务端提供的摘要，不向用户暴露 debug 原始输入输出。
2. reasoning 只作为模型商提供的 summary 展示，使用折叠区域并允许产品关闭。
3. 最终 HTML 使用 sandboxed iframe，图片 URL 由调用方按既定流程转存 OSS。
4. HITL feedback 提交失败时保留本地文本，避免用户重新输入。
5. 以 ID upsert activity、artifact 和 interrupt，避免“继续”重发时产生重复卡片。
6. 同一 session 同时只允许一个创建、恢复或取消操作，按钮提交期间进入 pending 状态。
7. 未知事件和新增可选字段应被忽略，保证协议向后兼容。

### 9.1 浏览器、网关与长连接约束

当前应用自身不校验 `Authorization`，也未配置 CORS；它应作为可信内网服务，由前端团队服务端/BFF 或
统一 API 网关调用，不应把 `8140` 端口作为无鉴权公网浏览器接口。TLS、调用方鉴权、浏览器 CORS 和
用户权限校验由部署网关/BFF 负责。若未来确需浏览器直连，必须先在网关明确允许的 Origin、凭证策略和
鉴权头，不能使用通配 CORS 暴露该服务。

当前 SSE 不发送 heartbeat/comment，模型、文档修复或图片调用期间可能长时间没有 frame。客户端不要设置
覆盖整条工作流的总超时；反向代理必须关闭响应缓冲，并把 idle/read timeout 设置为高于最长单次上游
调用（当前模型和图片调用上限可达 600 秒，建议至少 700 秒，生产可取 900 秒）。连接中断后走第 6 节
恢复流程，不要盲目重放原 POST。

`X-Request-ID` 只用于链路追踪，不是幂等键。建议每次 HTTP 尝试生成新的值并记录到日志；复用同一个值
也不会阻止重复 run/HITL。除幂等 cancel 外，不要由 HTTP 客户端自动重试具有歧义结果的 POST。对于
429/503 等建立 SSE 前错误，只在确认请求未被接受且 `retryable=true` 时退避重试；已建立 SSE 后断线
应使用状态恢复流程。

完整公共协议和 adapter 实现约束见 [Responses-like 扩展协议规范](responses-like-protocol.md)。
所有 HTTP 路径、字段和响应示例见 [前端 API 参考](api.md)。
