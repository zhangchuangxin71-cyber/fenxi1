# WeChat Article Agent V1 实现设计

> 状态：实现前设计基线
>
> 更新时间：2026-08-12
>
> 实现目录：`src/wechat-article-agent`
>
> 对外协议：Responses-like SSE
>
> 内部运行时：LangGraph Agent Server Protocol v2 + PostgreSQL checkpointer

## 1. 目标与范围

本服务根据前端传入的完整对话历史和参考文档，生成微信公众号文章，最终交付带图片的 HTML。
服务需要同时满足：

1. 文档研读前在信息不足时进行一次尽量完整的澄清。
2. 任务书、大纲和 Markdown 正文生成后分别等待用户审批。
3. 用户可以接受、完全重新生成或提供意见后修改当前产物。
4. 已经生成 HTML 后，用户可以从受影响的最早阶段开始修改，而不是总从文档研读重跑。
5. SSE 断开、页面刷新或服务短暂异常后，可以从 checkpoint 恢复业务状态，并重新展示最新
   artifact 和待审批卡片。
6. 前端能看到当前阶段、重要工具状态、模型输出文本、模型 reasoning 和待用户操作的 HITL 表单。
7. 微信文章 Agent 和 PPT Agent 共用同一套 Responses-like 扩展事件规范。
8. 本地开发面板能模拟产品体验，并在 debug 双开关开启时查看完整 node/LLM/tool/fallback trace。
9. 语言模型、图片模型、检索、数据库和 renderer 都有明确的有限重试、降级与终止规则。
10. 生产默认端口为 8140，本地开发端口为 8240，并能集成根 Compose 和部署脚本。
11. 正式 cancel 接口能够停止运行中或等待 HITL 的 run，防止取消后继续调用模型、工具或写入产物。

V1 明确不实现：

- 按章节多轮生成正文。
- 通用 Skill 系统。
- 跨会话长期记忆。
- 审批记录的逐条来源、冲突关系和覆盖链。
- 对模型 reasoning 做摘要、改写或隐藏。
- LLM 参与 HTML 排版。
- 将正文生成时临时补搜的结果回写素材库。
- 完整兼容 OpenAI Responses API 的所有字段和所有事件。
- 持久化或重放断线期间的文本、reasoning、节点和工具 activity 事件。

## 2. 既有平台边界

现有平台包含五个服务：

| 服务 | 端口 | 本项目使用方式 |
| --- | ---: | --- |
| `mineru-api` | 8135 | 不直接调用；由入库服务和 raw 修复链路使用 |
| `repo-doc-ingestion` | 8100 | 不直接调用；文档已经由前端完成入库 |
| `rag-retrieval-service` | 8120 | 调用 `meta/raw/route/retrieve` |
| `rag-knowledge-chat` | 8130 | V1 不调用 |
| `rag-report-agent` | 8115 | V1 不调用，只参考部分 SSE 和模型网关经验 |

核心文档表是 `documents`、`document_bindings`、`doc_pages` 和 `doc_nodes`。微信文章服务不得绕过
检索服务直接读取这些表。

`rag-retrieval-service` 当前只按调用方显式传入的 `doc_ids/temp_doc_ids` 读取文档，
`user_id`、`kb_id` 和 `session_id` 不作为 SQL 可见性条件。这是现有系统的既定边界：前端调用者
负责父子用户及共享文档的权限判断，并保证传入的文档 ID 都在当前用户的允许范围内；微信文章
服务不重复实现文档 ACL。

### 2.1 四个检索接口

#### `POST /rag/v1/documents/meta`

批量读取正式和临时文档的名称、摘要、页数、节点数和临时属性。文档研读首先调用此接口。
返回 `missing_doc_ids` 时不得静默忽略，应终止本轮并返回可理解的错误。

#### `POST /rag/v1/documents/route`

根据文档名和 `doc_description` 对一组或多组标准进行文档级路由，不读取正文。文档研读使用
该接口得到相关文档集合。V1 将 `accept_doc_ids` 和 `possible_doc_ids` 都作为候选相关文档，
以召回优先；`reject_doc_ids` 排除。

调用方必须记录每组的 `degraded`、`decision_source` 和 `warnings`，不能只读取文档 ID。

#### `POST /rag/v1/retrieve`

根据 query 完成分类、文档路由、标题树导航、逐页判定和结果合并，最终返回 page 原文。
调用方必须同时检查：

- HTTP 状态；
- `warnings`；
- `coverage.complete/truncated`；
- `chunks[].chunk_meta.content_truncated`；
- `usage`。

HTTP 200 可能仍然表示部分覆盖或降级结果。

#### `POST /rag/v1/documents/raw`

读取完整 MinerU 解析结果。已有 raw 时返回 200；缺失时返回 202 和 `status_url/retry_after`。
V1 的 raw 工具负责按 `retry_after` 轮询状态，并设置总超时和最大轮询次数。不可重试的 422
不再重试 raw，而是按第 19.6 节判断是否可降级到 `/retrieve`；可重试的 503 先按统一退避策略
处理，耗尽后也尝试该降级路径。

## 3. 总体架构

```text
Frontend
   |
   | POST /v1/responses
   v
Responses-like Adapter / Admission（图外）
   |- 请求校验、同 session 串行控制
   |- 对话历史预处理
   |- session_id -> thread_id
   |- 查询 checkpoint 中的 pending interrupt
   |- create / revise / HITL resume / pending preflight
   v
GraphRuntimeClient
   |
   | LangGraph Agent Server Protocol v2
   v
LangGraph + PostgreSQL Checkpointer
   |- intent_router
   |- session_memory || orchestrator
   |- 单向业务状态机
   |- HITL interrupt
   v
AgentEventNormalizer
   |- LangGraph 节点、custom、messages、tools 事件
   |- Ark Responses 文本和 reasoning 增量
   v
Responses-like SSE
   |- 标准 Responses 生命周期/文本/reasoning 事件
   |- agent.activity
   |- agent.artifact
   `- agent.interrupt
```

adapter、GraphRuntimeClient、AgentEventNormalizer 与具体文章图解耦。PPT Agent 可以复用同一套
外部事件 schema 和 adapter 核心，只替换业务图、artifact 字段和 HITL 表单内容。

## 4. 标识符和会话语义

### 4.1 标识符

| 标识符 | 含义 | 生命周期 |
| --- | --- | --- |
| `session_id` | 前端会话 ID，前端保证全局唯一 | 一个前端会话 |
| `thread_id` | LangGraph checkpoint 线程 | 与 `session_id` 一一对应 |
| `run_id` | 一次 create 或 revise 业务工作流 | 可跨多个 HITL response |
| `response_id` | 一次开始/恢复到结束或下次 interrupt 的 SSE 片段；也是 cancel 的目标 ID | 一个流片段 |
| `revision` | session 内业务产物快照序号 | 新的业务需求修改时递增 |
| `artifact_id` | 一个 revision 的聚合产物快照 ID | 一行包含该 revision 的全部阶段产物 |
| `interrupt_id` | 一次待处理 HITL | 处理后不可再次恢复 |

不维护额外的 session/thread 映射表。`thread_id` 使用固定应用 namespace 对 `session_id` 做
UUIDv5 派生：

```text
thread_id = uuid5(WECHAT_AGENT_THREAD_NAMESPACE, session_id)
```

前端不需要知道或传入 `thread_id`，业务数据库也不保存 session/thread 映射。adapter 每次都
根据 `session_id` 计算相同的 `thread_id`。Agent Server 每次首次执行或恢复产生的内部 run ID
不向前端公开，也不建立历史映射表；为了支持跨请求 cancel，当前 revision 只暂存当前活跃的
`active_runtime_run_id`，进入 waiting/terminal 状态后清空。

### 4.2 串行约束

同一个 `session_id` 同时最多有一个活跃业务 run。adapter 使用基于 `thread_id` 的 PostgreSQL
advisory lock 串行化“读取 thread 状态并向 Agent Server 成功提交运行”的临界区，并要求 Agent Server 对同一
thread 的并发运行使用 reject 策略：

- 正在运行时重复 create：返回 409 `SESSION_RUN_IN_PROGRESS`；
- 存在 pending interrupt 时的普通输入：进入 preflight；
- 旧页面提交已解决或旧 revision 的 interrupt：返回 409 `STALE_INTERRUPT`；
- 重复或并发提交已经处理的 interrupt：当前 checkpoint 不再匹配，返回 409
  `STALE_INTERRUPT`。

V1 不建立请求幂等表，也不承诺用同一请求重放第一次的完整 Response。重复审批由 thread 锁、
`previous_response_id`、`interrupt_id` 和 checkpoint 当前 pending 状态共同拒绝。

advisory lock 必须使用独立数据库连接并保持到 Agent Server 已确认创建或恢复内部 run 后再释放；
只锁住 checkpoint 读取、在提交 run 前释放会留下竞态窗口。锁释放后，运行中的事实以 Agent
Server 的 thread/run 状态为准，不能仅根据 `article_artifacts.status` 推断。

## 5. 对话历史和 session memory

### 5.1 历史所有权

完整对话历史由前端数据库负责持久化，并在每次 `POST /v1/responses` 的 `input` 中完整传入。
微信文章数据库不保存完整对话历史，也不实现跨 session 记忆。

adapter 使用确定性代码预处理历史：

- 删除历史 artifact 全文；
- 删除历史工具调用参数和工具完整结果；
- 保留用户输入、助手必要说明和各阶段历次审批意见；
- 形成适合分类和摘要的紧凑历史视图。

历史预处理不得调用 LLM，也不是 LangGraph node。

### 5.2 Runtime Context 与 Graph State

未压缩的原始历史不写入 Graph State，避免被每个 checkpoint 重复保存。adapter 将原始历史和
预处理历史作为 LangGraph runtime context 传入首次运行。`orchestrator` 读取原始历史；
`session_memory` 读取预处理历史。

interrupt 恢复后，下游使用 checkpoint 中已保存的 session memory、当前 artifact 引用和
本次 resume 值，不依赖再次取得原始历史。启动新的 create/revise run 时，adapter 再次提供
runtime context。

### 5.3 session_memory

`session_memory` 与 `orchestrator` 完全并行，两者互不依赖。session memory 只供后续生成节点
消费，不参与路由决策。

V1 输出保持简单：值是由 LLM 总结的自然语言字符串，不保存逐条来源、覆盖关系或冲突图。
LLM 在总结时负责剔除已经过期或被后续反馈否定的信息。

```json
{
  "docs_research": "用户最终确认文章主题为新能源汽车产业趋势。",
  "task_spec": "受众偏行业从业者，不需要学术论文语气。",
  "outline": "一级标题使用一/二，二级标题使用1/2；第三章增加风险小节。",
  "article": "语言口语化；背景铺垫缩短；各章节篇幅要有主次。",
  "ai_image": "",
  "html_layout": ""
}
```

`ai_image` 和 `html_layout` 是预留字段。V1 的 AI 生图和代码排版不消费历史偏好，因此这两个
字段固定输出空字符串；未来生图交互或 AI 排版变复杂时，可以在不修改 session memory 顶层
契约的前提下开始填充。其他字段也允许空字符串，不要求每个阶段都有历史摘要。

## 6. Admission、pending preflight 和意图路由

### 6.1 请求入口顺序

```text
POST /v1/responses
  -> adapter/admission
       -> 校验 schema、session、model
       -> 预处理完整历史
       -> 由 session_id 派生 thread_id 并读取 checkpoint
       -> 有 pending + context.hitl：校验后 Command(resume=...)
       -> 有 pending + 普通输入：preflight
            -> 重新展示当前审批卡片
            -> 转换为 revise/approve/regenerate 后恢复
            -> supersede
       -> 无 pending + thread 仍在运行：返回运行中状态或 409
       -> artifact 状态为 cancelling：刷新 runtime 状态并返回 cancelling/cancelled，禁止启动新 run
       -> artifact 状态为 cancelled + 普通输入：创建新 revision，由 orchestrator 决定入口
       -> 无 pending + 最近运行失败 + 明确“重试/继续”：按 last_error.recovery_action 恢复
       -> 无 pending + 已完成 + 明确恢复输入：重新发送最终 artifact 快照
       -> 其他无 pending 请求：启动 graph -> intent_router
```

文档可见性由前端保证，adapter 只做 ID 格式、数量和接口契约校验，不重复查询 ACL。

这里的“明确恢复输入”仅指“继续”“重新展示刚才结果”等不包含新业务修改的请求。已完成后带有
新增、删除、改写等需求的普通输入仍启动新 graph，由 intent router 和 orchestrator 创建新的
业务 revision；adapter 不用恢复分支吞掉真实修改请求。

如果最近运行状态是 `failed`，adapter 根据 `last_error.recovery_action` 处理明确“重试/继续”：临时
基础设施错误使用 `resume_checkpoint`，在同一 revision 上生成新 response ID 并清空本次尝试计数；
结构化输出修复或节点循环次数耗尽使用 `new_revision`，交给 orchestrator 重新选择入口；缺少文档、
无相关证据和确定性输入错误使用 `needs_user_fix`，只重新展示明确错误。如果用户同时提出新的业务
修改，则无论错误类型都创建新 revision。失败恢复不得伪装成 HITL `Command(resume=...)`。

### 6.2 pending preflight

只有“普通用户输入 + 当前存在 pending interrupt”时执行。`POST /v1/responses` 中显式携带
`context.hitl` 时不经过分类器，adapter 直接根据 checkpoint 中的 pending 数据执行表单校验并恢复。

轻量分类器使用 strict 结构化输出：

```json
{
  "action": "replay | revise_current | regenerate_current | approve_current | supersede",
  "reason": "面向用户显示的简短说明",
  "feedback": "归一化后的反馈或空字符串",
  "confidence": 0.0
}
```

规则：

| 输入 | 动作 |
| --- | --- |
| “继续”“刚才到哪了” | 不恢复 graph，重新发送当前 artifact 快照和 pending `agent.interrupt` |
| 针对当前待审 artifact 的反馈 | 转换为合法 resume decision |
| 明确改变主题、受众或更上游需求 | supersede 旧 run，创建新 revise run |
| 无法可靠判断 | replay，不擅自丢弃待审结果 |

supersede 不物理删除旧 checkpoint 和 artifact。adapter 用 `Command(resume={type:
"supersede"})` 让旧图进入终态 `superseded`，随后在同一 session/thread 上创建新的 `run_id` 和
revision，并用本次普通输入启动新图。旧数据等待 TTL 清理。

新 revision 的初始化输入必须显式覆盖所有 run 级字段，包括 `run_id/revision/current_user_input/
session_memory/entry_stage/route_reason/relevant_doc_ids/generation_attempts/clarification_round/status`。
当前 artifact 快照从业务数据库重新装载；不得依赖旧 checkpoint 中碰巧残留的字段。

### 6.3 intent_router

无 pending 时，主图第一个节点识别：

```text
wechat_article | knowledge_chat | ppt_generation | chitchat | other
```

V1 只有 `wechat_article` 继续进入文章流程。其他意图由代码生成固定提示，告知前端切换对应
Agent，不调用其他服务。分类器读取 runtime context 中的原始历史，使用 strict 输出。

## 7. 主图编排

```text
START
  -> intent_router
       -> non_wechat_response -> END
       -> wechat_article
            +-> session_memory -----+
            |                       |
            +-> orchestrator -------+-> entry_dispatch
                                         |- docs_research
                                         |- task_spec
                                         |- outline
                                         `- article

docs_research -> task_spec -> outline -> article -> ai_image -> html_layout -> END
```

LangGraph 中使用多起点到单 join 的边，保证 `entry_dispatch` 等待 `session_memory` 和
`orchestrator` 两个并行节点都完成。

### 7.1 orchestrator

orchestrator 每个 create/revise run 只执行一次。它读取 runtime context 中未被 session memory
压缩的原始历史，以及数据库中当前已批准 artifact 的轻量元数据，输出：

```json
{
  "target": "docs_research | task_spec | outline | article",
  "reason": "用户改变了文章主题，需要重新研读参考文档。"
}
```

`reason` 是面向用户的解释文本，通过标准 `response.output_text.delta` 输出，不是内部
chain-of-thought。`target` 只决定本轮的入口阶段；进入后必须按单向状态机执行，orchestrator
不再介入后续节点。

典型路由：

| 变化 | 起点 |
| --- | --- |
| 主题、核心目标或参考范围变化 | `docs_research` |
| 受众、目标、语气等任务书级变化 | `task_spec` |
| 章节结构变化，如新增第三章小节 | `outline` |
| 仅正文表达、篇幅、措辞变化 | `article` |

路由到上游阶段后，创建新的 artifact revision：复制仍然有效的上游字段，清空受影响阶段及其
所有下游字段，然后由单向状态机逐步填充。旧 revision 在服务内部保持可查询直至 TTL 清理；
V1 不因此增加对外的 artifact 查询接口。

### 7.2 最小 Graph State

checkpoint 只保存运行恢复所需的小对象和 artifact 引用，不保存完整对话历史、raw 文档、素材
正文、Markdown 全文或 HTML 全文。

```python
class WechatArticleState(TypedDict, total=False):
    session_id: str
    user_id: str
    kb_id: str
    run_id: str
    response_id: str
    revision: int
    intent: str
    entry_stage: str
    route_reason: str
    current_user_input: str
    session_memory: dict[str, str]
    doc_ids: list[str]
    temp_doc_ids: list[str]
    relevant_doc_ids: list[str]
    document_coverage_mode: Literal["best_effort", "all_required"]
    research_direction_options: list[dict[str, str]]
    research_direction: dict[str, str]
    artifact_id: str
    current_stage: str
    generation_attempts: dict[str, int]
    clarification_round: int
    status: str
```

节点需要素材、任务书、大纲、正文、图片或 HTML 时，根据唯一 `artifact_id` 从
`article_artifacts` 加载当前 revision 的聚合快照。

首次运行时，adapter 将当前请求已校验的 `user_id/kb_id` 和新生成的 `response_id` 一并放入初始
Graph State。它们是下游检索调用所需的小型运行参数，不参与数据库权限判断，也不代替前端传入的
完整历史。显式恢复时，
adapter 先生成新 `response_id`，再将其放入经过校验的 `Command(resume=...)` 值；
`review_interrupt` 恢复后把新值返回为 Graph State 更新。下一个 review interrupt 将 State 中的
当前 `response_id` 放入 payload，下一次显式审批使用 `previous_response_id` 与它校验。该关联
完全保存在 checkpoint，不新增 response 业务表，也不要求 adapter 直接修改 checkpoint 内部表。

## 8. HITL 子图

### 8.1 通用 artifact review 子图

`task_spec`、`outline` 和 `article` 共用相同语义：

```text
prepare_context
  -> generate_candidate
  -> persist_candidate
  -> review_interrupt
       |- approve -> mark_approved -> next_stage
       |- regenerate -> generate_candidate
       |- revise -> generate_candidate
       `- supersede -> END(superseded)
```

实现可以使用共享的 node factory 和 review service，不要求把三个阶段强行编译成完全相同的
子图实例。各阶段仍拥有自己的 prompt、artifact 字段 schema 和上下文装配器。

`review_interrupt` 节点只执行以下操作：

1. 根据已经持久化的当前 artifact 字段构造 JSON 可序列化表单；
2. 将 `interrupt_id/response_id/stage/artifact_id/revision/form` 放入 interrupt payload；
3. 调用一次 `interrupt(payload)`；
4. 对恢复值做 schema 校验，返回 decision，并将恢复值携带的新 `response_id` 更新到 Graph State。

第 4 项适用于 approve/revise/regenerate 等会产生新 SSE response 的用户审批。cancel endpoint 发送的
内部 `{type: "cancel"}` 是唯一例外：它保留当前 `response_id`，不要求或写入新 ID，只返回 cancelled
路由。两种 resume payload 使用可辨识联合 schema，不能让公开审批请求伪造内部控制类型。

生成模型调用、artifact 内容写入、Seedream 调用等副作用不得与 `interrupt()` 放在同一个节点。
LangGraph 恢复时会从 interrupt 所在节点开头重新执行，因此 review node 不执行 artifact 写入
或其他非幂等副作用；pending 状态由 checkpoint 中的 interrupt 保存，不另建业务表。

审批动作：

| 动作 | 含义 |
| --- | --- |
| `approve` | 批准当前版本，进入下一阶段 |
| `regenerate` | 不沿用当前内容，结合已有约束重新完整生成；反馈可选 |
| `revise` | 结合当前版本和必填反馈生成完整新版本 |
| `supersede` | 普通输入被 preflight 判定为上游变更，结束旧 run |
| `cancel` | 仅供 adapter 的 cancel endpoint 使用；不生成新候选，直接进入 `END(cancelled)` |

同一个业务 revision 内，regenerate/revise 允许覆盖当前尚未批准的阶段字段；V1 不保存每一个
被拒绝的候选版本。用户批准后再产生新的上游需求修改时，才创建新的聚合 artifact revision。

### 8.2 文档研读澄清子图

```text
load_document_meta
  -> plan_research_direction
  -> clarification_interrupt(A/B/C + custom about/target)
       |- A/B/C -> persist_research_direction -> route_documents
       `- custom -> validate_custom_research_direction
                      |- sufficient -> persist_research_direction -> route_documents
                      |- ambiguous and under limit -> clarification_interrupt
                      `- at limit -> persist last custom direction -> route_documents
```

文档研读方向只使用两个字段：`about` 表示纳入研读的文档语义范围，`target` 表示需要重点提取的
内容。模型一次生成 A/B/C 三个有实质区别的推荐方向，前端以单选卡展示，并提供包含 about/target
两个输入框的“其他”选项。受众、语气、篇幅和传播目标留给任务书节点确认。

`plan_research_direction` 还必须在开始文档路由和素材提取前，根据用户要求确定
`document_coverage_mode`：普通选材、概括和观点文章为 `best_effort`；逐篇比较、逐篇总结或明确
要求覆盖每一篇指定文档时为 `all_required`。

用户点击 A/B/C 时直接采用服务端选项，不再校验或追问。只有用户提交自定义方向时才调用
`validate_custom_research_direction`；仍然含糊时重新生成同结构的三个方向卡。`clarification_round`
只统计自定义提交，进入 checkpoint 用于观测和防止无意循环；达到 `CLARIFICATION_MAX_ROUNDS`
后采用用户最后一次 about/target 强制开始研读，不返回失败。

确认后的 `research_direction` 同时写入 Graph State 和当前 artifact revision 的 JSONB 字段，并显式
传给文档路由、素材 query planner 和任务书节点。新 revision 从任务书或更下游阶段进入时，随已批准
的素材库一起继承该方向。

`clarification_interrupt` 与 review interrupt 遵守相同的 Response 关联规则：恢复 payload 必须携带
adapter 新生成的 `response_id`，`merge_clarification` 校验恢复值后将它写回 Graph State。下一次
澄清或 review 卡片使用更新后的 `response_id`，adapter 不直接修改 checkpointer 内部表。

文档研读不要求在素材库生成后再进行一次人工审批；其 HITL 入口是信息不足时的澄清。
`clarification_interrupt` 同样接受仅供 adapter 使用的内部 `cancel` 控制值并直接结束为 cancelled；
公开 HITL 表单不把 cancel 伪装成 clarification answer，前端需要取消时调用正式 cancel endpoint。

## 9. 文档研读实现

### 9.1 输入

- 当前用户输入；
- `session_memory.docs_research`；
- 本轮 `doc_ids/temp_doc_ids`；
- `meta` 返回的文档摘要；
- 研读充分性/澄清阶段已经确定的 `document_coverage_mode`；
- 当前 artifact revision 中已存在的 `material_library`（修改流程中可存在）。

### 9.2 文档路由

sufficiency 通过后，根据确认后的主题和写作目标构造 `route.criteria`。V1 默认
`keyword_prefilter=false`，让全部请求范围文档进入 LLM 路由，避免关键词硬预筛降低召回；
文档数量或实测延迟需要时可通过配置开启。

```text
relevant_doc_ids = accept_doc_ids U possible_doc_ids
```

若相关集合为空，进入一次面向用户的澄清/告知，而不是生成没有参考依据的文章。

### 9.3 素材库更新

按以下顺序执行：

1. 文档级过滤：代码删除来源不在 `relevant_doc_ids` 的当前素材。
2. 素材级过滤：LLM 根据当前需求和素材摘要选择仍应保留的 `material_id`。
3. 计算 `remain_doc_ids`，提取 `relevant_doc_ids - remain_doc_ids` 中的文档。
4. 文档之间有界并行执行单文档素材提取。
5. 更新当前 `article_artifacts.material_library`。

“某文档的素材被全部删除后，下次再次提取该文档”是 V1 的预期行为。素材库表示当前需求的
有效快照；一旦重新路由到文档研读，就允许按新需求重新处理文档，不维护额外的历史提取台账。

### 9.4 短文档提取

1. 调用 raw，并处理 202 修复轮询。
2. 从 `raw_mineru.md_content` 提取规范化全文；缺失时按页码拼接 `pages[].content`。
3. 计算规范化全文字符数。
4. 小于 `SHORT_DOCUMENT_MAX_CHARS` 时，将全文作为一个 `full_text` chunk。
5. `summary` 直接使用 `doc_description`。

不把 `md_content`、`content_list`、`middle_json` 三份重复内容同时写入模型上下文。即使字符阈值
判定为短文档，在送入模型前仍执行统一 context budget 检查；超过硬窗口时自动走长文档路径。

### 9.5 长文档提取

1. LLM 根据文档摘要、当前需求和 memory 生成 N 组 queries。
2. 每组 query 内的问题围绕同一个信息方面，每项可独立检索。
3. 对同一文档的各 query group 有界并行调用 `/retrieve`。
4. 每组返回结果形成一个 material；summary 使用格式化后的 query group。
5. 原文保留 `chunk_id/document_id/page_number/path/content/hint/chunk_meta` 等来源字段。
6. coverage 不完整或 warning 不为空时写入 material metadata，供正文节点判断证据质量。

### 9.6 素材结构

```json
{
  "material_id": "mat_01...",
  "source_doc_id": "doc-1",
  "summary": "该素材回答：1. 去年营业额；2. 同比增长率",
  "active": true,
  "metadata": {
    "extraction_mode": "raw | retrieve",
    "queries": [],
    "coverage_complete": true,
    "warnings": []
  },
  "orig_chunks": [
    {
      "chunk_id": "doc-1:page:12",
      "type": "full_text | page",
      "page_number": 12,
      "path": "document:doc-1:page:12",
      "content": "..."
    }
  ]
}
```

素材正文存业务 artifact 表，不进入 Graph State，也不进入公开 activity 事件。

## 10. 任务书、大纲和正文

### 10.1 task_spec

输入：

- `session_memory.task_spec`；
- 用户本次输入；
- material summaries；
- 当前 artifact 的 `document_coverage_mode`（只读约束）；
- 当前反馈；
- 当前 revision 中尚未批准的 task spec（HITL revise 时）。

使用 strict 结构化输出，提交契约为：

```json
{
  "user_facing_message": "我已经根据材料整理出一份面向行业从业者的写作任务书。",
  "artifact": {
    "topic": "",
    "audience": "",
    "goal": "",
    "tone": "",
    "length": "",
    "other_requirements": []
  }
}
```

adapter 只转发 `user_facing_message`，数据库只把 `artifact` 写入当前聚合 artifact 的 `task_spec`
字段，然后进入通用 review interrupt。任务书可以用自然语言表述覆盖要求，但不得改写独立的
`document_coverage_mode` 控制字段；V1 不允许任务书节点补搜。

### 10.2 outline

输入 task spec、material summaries、`session_memory.outline` 和当前反馈。模型使用 strict 提交契约
`{user_facing_message, markdown}` 返回 Markdown，不要求模型返回结构化章节 ID。

如图片定位或前端渲染需要章节结构，服务端使用 Markdown parser 从 heading AST 中确定性提取，
不得用正则或要求模型额外生成一份可能不一致的 JSON 大纲。

生成后更新当前聚合 artifact 的 `outline` 字段，并进入通用 review interrupt。V1 不允许大纲
节点补搜。

### 10.3 article

V1 以跑通完整链路为目标，不按章节多轮写作。article 使用一个有界工具调用循环，最终一次性
生成完整 Markdown：

```text
article_prepare_context
  -> article_model
       |- retrieve tool requested -> article_retrieve -> context_check -> article_model
       `- final Markdown -> persist_article -> review_interrupt
```

输入：

- 已批准 task spec；
- 已批准 outline；
- 当前 material library；
- `session_memory.article`；
- 当前反馈；
- 当前 revision 中尚未批准的正文（HITL revise 时）。

文章补搜必须调用 strict `retrieve_article_evidence` 工具，参数为
`{user_facing_message, queries}`，其中 `queries` 是可独立检索的非空字符串数组。adapter 在工具参数
完整校验后转发 `user_facing_message`，工具在全部 `relevant_doc_ids` 范围调用 `/retrieve`。最多调用
`ARTICLE_MAX_RETRIEVAL_ROUNDS` 次。每轮模型调用前执行 context budget 检查；超过预算时，优先
裁剪重复或低价值工具结果，再使用模型压缩工具历史，不能静默截断 task spec、outline 或用户反馈。

工具运行状态由代码生成 `agent.activity`，不能依赖模型一定输出说明。模型最终必须调用 strict
`submit_article` 工具，参数为
`{user_facing_message, markdown}`；数据库只保存 `markdown`。revise/regenerate 也提交完整正文并
覆盖当前 revision 的候选字段，不在 V1 实现 patch 工具。模型没有调用该工具、参数 schema 不合法
或提交空 Markdown 都不视为生成成功。

临时检索结果只在当前 article 调用循环内使用，不回写 material library。

## 11. 图片和 HTML

### 11.1 ai_image

只在当前聚合 artifact 的 `article_markdown` 获得批准后执行：

1. 使用 Markdown AST 提取 heading 和段落位置。
2. LLM 选择不超过 `IMAGE_MAX_COUNT` 个位置并生成 Seedream prompt。
3. 有界并行调用 Seedream。
4. 将图片 URL、图片标注和文章插入位置写入当前 revision 的 `images` 字段。

图片位置使用当前 revision 的文章 Markdown 中由代码提取的 heading path/ordinal 标识。业务修改
创建新 revision 时，上一 revision 的图片仍保留在旧行，当前 revision 的 `images` 从 null 开始；
新节点可以通过 `parent_artifact_id` 读取旧图片作为参考，重新判断是否复用其 URL、删除或改变
位置，然后只把本轮最终图片集合写入当前 revision。

V1 的持久化契约只保存 `url/caption/insertion_position`：不保存图片二进制、base64、生成 prompt、
宽高或本服务 OSS 地址。Seedream prompt 只存在于当次模型/工具调用内；debug 开启时可以出现在
当次内存 trace 中，但不能进入 artifact。前端调用者负责及时下载 URL 并转存自己的 OSS。

```json
[
  {
    "url": "https://provider.example/image.png",
    "caption": "新能源汽车产业链示意图",
    "insertion_position": {
      "heading_path": ["二、产业变化", "1. 上游材料"],
      "paragraph_ordinal": 2
    }
  }
]
```

HTML 排版同样只把该 URL 写入 `<img src="...">`，不下载、不代理、不转码图片。URL 可用期限由
上游调用契约和三天 artifact TTL 共同约束；最终 HTML 交付后的 URL 下载与 OSS 替换由前端负责。

### 11.2 html_layout

V1 完全由代码执行，不调用 LLM：

1. 使用 Markdown parser 生成 AST/HTML。
2. 插入当前 revision `images` 字段中的图片。
3. 应用固定微信公众号主题模板和内联 CSS。
4. 图片样式至少包含 `max-width:100%; height:auto`。
5. 将完整 HTML 写入当前聚合 artifact 的 `final_html` 字段。

HTML renderer 必须确定性、可测试；相同 Markdown、图片集合和主题版本应得到相同 HTML。
第一版只提供少量预置主题，不接收任意脚本或任意 CSS。

## 12. 聚合 Artifact 和 revision

### 12.1 一行表示一个业务 revision

V1 不为素材库、任务书、大纲、文章、图片和 HTML 分别建表或分别维护 artifact version。一行
`article_artifacts` 表示某个 session 的一次完整业务 revision，各阶段直接读写自己认识的原始
字段：

```json
{
  "artifact_id": "art_01...",
  "session_id": "session-1",
  "run_id": "run_01...",
  "current_response_id": "resp_01...",
  "active_runtime_run_id": "runtime-run-id-or-null",
  "revision": 3,
  "parent_artifact_id": "art_00...",
  "status": "waiting_for_input",
  "current_stage": "outline_review",
  "approved_through_stage": "task_spec",
  "last_error": null,
  "doc_ids": ["doc-1"],
  "temp_doc_ids": [],
  "relevant_doc_ids": ["doc-1"],
  "document_coverage_mode": "best_effort",
  "material_library": [],
  "task_spec": {},
  "outline": {"markdown": "# 标题\n..."},
  "article_markdown": null,
  "images": null,
  "final_html": null
}
```

checkpoint 只保存当前 `artifact_id`，不保存这些大字段。节点从业务表加载当前行，更新对应字段。

### 12.2 revision 创建和字段失效

首次生成创建 revision 1。已有 revision 后，用户提出会 supersede 当前运行或修改已完成结果的
新业务需求时，orchestrator 选择入口阶段，服务创建下一 revision，复制仍然有效的上游字段，
并清空该入口及其全部下游字段：

| 入口阶段 | 从上一 revision 复制 | 清空并重新生成 |
| --- | --- | --- |
| `docs_research` | 无 | 全部字段 |
| `task_spec` | `material_library`、`document_coverage_mode` | task spec、outline、article、images、HTML |
| `outline` | material library、document coverage mode、task spec | outline、article、images、HTML |
| `article` | material library、document coverage mode、task spec、outline | article、images、HTML |

`doc_ids/temp_doc_ids` 是该 revision 的参考范围，必须随 revision 保存，供后续修改省略文档字段时
继承；`relevant_doc_ids` 是当前文档研读结果。进入 `docs_research` 时重新计算
`relevant_doc_ids`，从更下游阶段进入时继承它。`run_id` 是当前 revision 的产品工作流标识，
用于断线后重新发送最终 artifact 和日志关联；它不是 Agent Server 的内部 run ID。

`parent_artifact_id` 指向直接上一 revision，便于三天内查看完整版本链。旧 revision 不被覆盖。
如果直接父 revision 是在 orchestrator 初始化或任何批准前就 cancelled/failed/superseded 的空壳，
下一次路由和字段继承必须沿 `parent_artifact_id` 向上找到最近一个
`approved_through_stage != none` 的祖先作为有效内容基线；新行的 `parent_artifact_id` 仍指向直接
上一 revision，不跳过空壳，以保留审计链。回溯只决定可读取的批准基线，实际复制范围仍同时受
本轮入口阶段和祖先 `approved_through_stage` 限制，不能继承任何未批准候选。

同一个 revision 的 HITL regenerate/revise 允许覆盖尚未批准的当前字段，不保存每一次被拒绝的
候选版本。V1 的版本化粒度是“业务 revision”，不是“每次模型候选”。

### 12.3 三类数据的责任边界

| 数据 | 持久化方 | 内容 |
| --- | --- | --- |
| UI 对话历史 | 前端数据库 | 用户与 UI 展示的完整历史 |
| LangGraph checkpoint | 新数据库中的 PostgreSQL checkpointer | 小型图状态、interrupt、执行游标和当前 artifact ID |
| 业务 artifact | 新数据库中的 `article_artifacts` | 当前 revision 的素材、任务书、大纲、文章、图片和 HTML |

V1 不持久化产品 SSE 事件，因此没有 run event 数据。连接正常时仍实时输出所有流式事件；断线后
只恢复业务状态、artifact 和 pending interrupt。

## 13. PostgreSQL 持久化设计

### 13.1 独立 database

主管明确要求为微信公众号 Agent 新建独立 PostgreSQL database，例如
`wechat_article_agent`。不得在现有 pageindex/rag database 中创建微信 Agent 的 checkpoint 或
业务表。服务使用单独的 `DATABASE_URL` 连接新 database。

新 database 中只有两类表：

1. LangGraph PostgreSQL checkpointer 由官方 setup 自动创建和管理的内部表；物理表数量由所选
   checkpointer 版本决定，业务代码不得合并、修改或直接依赖其内部 schema。
2. 微信文章服务自建的一张 `article_artifacts` 业务表。

### 13.2 `article_artifacts`

```sql
CREATE TABLE article_artifacts (
    artifact_id         text PRIMARY KEY,
    session_id          text NOT NULL,
    run_id              text NOT NULL,
    current_response_id text NOT NULL,
    active_runtime_run_id text NULL,
    revision            integer NOT NULL,
    parent_artifact_id  text NULL REFERENCES article_artifacts(artifact_id),
    status              text NOT NULL,
    current_stage       text NOT NULL,
    approved_through_stage text NOT NULL DEFAULT 'none',
    last_error          jsonb NULL,

    doc_ids             jsonb NOT NULL DEFAULT '[]'::jsonb,
    temp_doc_ids        jsonb NOT NULL DEFAULT '[]'::jsonb,
    relevant_doc_ids    jsonb NOT NULL DEFAULT '[]'::jsonb,
    document_coverage_mode text NOT NULL DEFAULT 'best_effort',

    material_library    jsonb NULL,
    task_spec           jsonb NULL,
    outline             jsonb NULL,
    article_markdown    text NULL,
    images              jsonb NULL,
    final_html           text NULL,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz NOT NULL,

    UNIQUE (session_id, revision)
);

CREATE INDEX idx_article_artifacts_session_latest
    ON article_artifacts (session_id, revision DESC);
CREATE INDEX idx_article_artifacts_expiry
    ON article_artifacts (expires_at);
```

`status` 第一版允许：

```text
running | waiting_for_input | cancelling | completed | failed | cancelled | superseded
```

`approved_through_stage` 只允许 `none/docs_research/task_spec/outline/article`，表示当前 revision 最后
一个已经完成业务确认的阶段。文档研读完成素材库后推进到 `docs_research`；task spec、outline、
article 只在对应 HITL approve 后推进。regenerate/revise 产生的未批准候选不推进该字段。取消、失败
或 supersede 后创建新 revision 时，只能继承 `approved_through_stage` 覆盖范围内的内容，不能按字段
非空判断有效性。

`last_error` 只保存稳定的 `{code, message, retryable, recovery_action, stage, request_id}`，不保存
traceback、prompt 或 provider 原始响应。进入新的执行尝试时清空，失败时与 `status/current_stage/expires_at` 在同一事务
更新。这样即使错误发生时 SSE 已断开，下一次请求仍能重新展示失败原因。

`material_library/task_spec/outline/images` 以对应节点直接识别的 JSON 原始格式保存，不再增加一层
通用 artifact wrapper。Markdown 和 HTML 使用 text，避免无意义地包入 JSON。

`document_coverage_mode` 只允许 `best_effort/all_required`。普通汇总默认 `best_effort`；用户明确
要求逐篇比较、覆盖全部指定文档或每篇分别总结时，由文档研读的充分性/澄清契约在素材提取前
设置为 `all_required`。task spec 只能读取和表述该值，不能在提取完成后改变它。该确定性字段供
第 19.6 节判断单篇提取失败是否允许继续，不能只靠错误发生后临时猜测用户意图。

### 13.3 写入和并发规则

- 创建新业务 revision 时使用事务读取上一行并插入新行。
- 路由和入口初始化遇到直接父 revision 没有任何批准内容时，沿父链回溯最近批准祖先；父链缺失
  或成环返回 `ARTIFACT_CHAIN_INVALID`，不得退化为凭字段非空猜测。
- 更新阶段字段时必须同时匹配 `artifact_id + session_id + revision`。
- 失败节点的显式重试沿用同一 `artifact_id/revision`；只有真实业务需求变化才创建下一 revision。
- checkpoint 中的 `artifact_id/revision/current_stage` 必须与业务行一致；旧页面的
  `previous_response_id/interrupt_id/revision` 不匹配当前 checkpoint 时返回 409。
- 同 session 的启动或恢复使用 advisory lock 和 Agent Server 的 thread 并发拒绝策略。
- `run_id/doc_ids/temp_doc_ids/relevant_doc_ids` 与 revision 同行保存，不从前端历史或旧 checkpoint
  反向猜测；`run_id` 只作产品关联，不用于调用 Agent Server run endpoint。
- review interrupt 节点不写业务表；生成节点先完成字段写入，再进入独立 interrupt 节点。
- `mark_approved` 节点以条件更新推进 `approved_through_stage`，并与相应 stage/status 更新放在同一
  事务；不能在收到 approve 请求时由 adapter 提前标记批准。
- Seedream 节点重放时先检查当前 `images` 字段和稳定图片位置标识，避免重复生成已经成功的图片。
- adapter 提交 Agent Server run 后，在同一 admission 临界区写入当前 `current_response_id` 和
  `active_runtime_run_id`；到达 interrupt 或任何终态时清空 `active_runtime_run_id`。cancel、终态回写
  和新 run 提交都使用 `artifact_id + revision + current_response_id` 条件更新，避免旧回调覆盖新状态。
- Agent Server 已创建 runtime run、但控制字段写入失败时，adapter 必须立即对刚创建的 runtime run
  发起补偿 cancel，并把本次提交视为失败；不能释放锁后留下数据库无法定位的孤儿活跃 run。补偿
  cancel 失败需要记录高优先级结构化日志和指标，由同 thread reject 阻止第二个 run。
- 所有 node artifact 写入额外要求当前 `status NOT IN ('cancelling','cancelled')`。影响行数为零时重新
  读取状态：若已取消则丢弃迟到结果并正常结束 cancellation；其他不一致按
  `STATE_ARTIFACT_MISMATCH` 失败。任何自然 completed/failed 回写都不能覆盖先确认的 cancelled。

V1 不建立 session、run、response、pending interaction、event 或 idempotency 业务表。严格的请求
结果重放不是 V1 目标；重复审批返回 409 即可。

## 14. TTL 和清理

checkpoint 和 `article_artifacts` 统一保留三天。`article_artifacts.expires_at` 使用滑动过期：

```text
expires_at = last_meaningful_activity_at + 3 days
```

有意义的活动包括创建 revision、恢复 interrupt、到达新的 interrupt、生成最终 HTML、失败和取消；
不为每个 token 或 activity delta 更新 `expires_at`。

Agent Server/checkpointer 必须同时启用三天 thread TTL，覆盖 graph 在首个业务 revision 成功落库前
失败而产生的孤儿 checkpoint。每次创建 revision、恢复 interrupt、到达新 interrupt 和进入终态时，
通过 Agent Server 支持的 thread TTL/更新能力刷新过期时间；业务代码不得直接更新 checkpointer
内部表。如果所选 Agent Server 版本的 TTL 不是滑动语义，启动兼容性测试必须先发现差异，再由
GraphRuntimeClient 在上述有意义活动点显式刷新，不能假设配置天然满足要求。

定时清理按 session/thread 分批执行：

1. 从 `article_artifacts` 找出该 session 最新 revision 也已过期的 session，使用
   `FOR UPDATE SKIP LOCKED` 小批量认领。
2. 通过 Agent Server/checkpointer 支持的 thread 删除能力清理对应 thread 的全部 checkpoint。
3. 删除该 session 的全部 `article_artifacts` revision；自引用外键可使用 deferred constraint，或
   先清空 `parent_artifact_id` 后删除。

取消和 supersede 不立即物理删除，仍保留到 TTL。清理任务使用较小 batch 和固定间隔，避免在
低配 PostgreSQL 上形成长事务、集中 WAL 或 vacuum 压力。

## 15. 模型网关和可观测工具

### 15.1 ArkResponsesChatModel

实现 LangChain 可观测的 `ArkResponsesChatModel`：

1. 调用火山方舟 `/api/v3/responses`，`stream=true`、`store=false`。
2. 不依赖上游 `previous_response_id`；会话上下文由本服务装配。
3. 把文本、reasoning 和 function call 增量转换为 LangChain `AIMessageChunk`/content blocks。
4. 保留同一响应中用户可见文本与 function call 的顺序。
5. 支持 strict 结构化输出和 strict tool schema。
6. 在网关统一处理模型公平调度、异常流量限流、超时、有限重试、熔断、usage、缓存命中信息和
   provider request ID。
7. 只有 task spec、outline、article 三个 artifact 生成 phase 可以按配置开启 thinking；其他
   phase 由网关强制关闭，调用节点不能自行覆盖。

禁止在 node 内直接使用 `httpx` 调模型，否则 LangGraph 无法得到完整消息增量和工具调用事件。

#### 并发和排队

HTTP/Agent run 入口参考检索服务的 admission controller，设置 `APP_MAX_ACTIVE_RUNS=30`、
`APP_MAX_QUEUED_RUNS=90` 和 `APP_ADMISSION_WAIT_TIMEOUT_SECONDS=120`。队列满或等待超时返回
可重试的 503。一次 run 到达 interrupt 或终态后释放 admission 槽位；等待用户审批期间不占用
活跃槽位。

参考现有检索服务的 `_FairScheduler`，模型网关采用进程级公平队列，而不是让某个文档研究 run
一次占满全部模型并发：

- `ARK_MAX_CONCURRENCY=30`：单进程所有普通 LLM 调用的全局在途上限；
- `ARK_MAX_QUEUED_CALLS=180`：排队上限，队列满时返回可重试的 503；
- `ARK_PER_RUN_MAX_IN_FLIGHT=6`：一个产品 run 同时在途调用上限；
- `ARK_QUEUE_WAIT_TIMEOUT_SECONDS=120`：超过排队等待时间返回可重试的 503；
- 按 `run_id` 轮转调度，避免单个长文档研读 run 饿死其他 session。

这里的 30 是 LLM provider 调用并发，不等于 30 个完整文章 run。若方舟 endpoint 实际配额更低，
生产配置必须下调。服务按单 worker 部署时该进程级队列即可满足 V1；未来多 worker/多副本需要把
全局配额移到共享限流组件，不能假设各进程的 30 会自动合并。

Seedream 继续使用独立并发池和熔断器，不占用普通 LLM 的 30 个槽位。

#### 异常流量限流

V1 不把 RPM 当作正常用户配额。入口请求和模型调用分别使用现有检索服务同类的进程内 token
bucket，只用于拦截脚本异常流量：

- `REQUEST_RATE_LIMIT_PER_MINUTE=1200`、`REQUEST_RATE_LIMIT_BURST=300`；
- `ARK_RATE_LIMIT_PER_MINUTE=3000`、`ARK_RATE_LIMIT_BURST=600`；
- key 使用已经认证的调用方/user ID；超限返回 429 和 `retry_after`；
- bucket 空闲一段时间后回收，避免无限保存 user key。

这些默认值明显高于约 30 并发下的正常交互，不应影响人工使用。真正保护模型容量的是公平队列
和并发上限，RPM 只处理异常脚本。

#### 超时和有限重试

超时分层处理：

1. `ARK_CONNECT_TIMEOUT_SECONDS=10`：连接和 TLS 建立超时。
2. `ARK_FIRST_EVENT_TIMEOUT_SECONDS=60`：成功建连后等待首个 SSE 事件的超时。
3. `ARK_STREAM_IDLE_TIMEOUT_SECONDS=90`：两个 SSE 事件之间的最大空闲时间；每收到合法事件重置。
4. `ARK_CALL_MAX_SECONDS=600`：一次模型调用包括 reasoning 和正文流的总上限。
5. 图节点或完整 response 另有更大的 deadline，不用无限延长单次 provider call。

`ARK_MAX_RETRIES=2` 表示首次调用后最多额外尝试两次，使用带随机抖动的指数退避，基础间隔由
`ARK_RETRY_BASE_SECONDS` 配置。只重试连接失败、首事件超时、HTTP 408/429/500/502/503/504；
认证错误、请求/schema 错误、内容安全拒绝、上下文超限和其他确定性 4xx 不重试。

流式重试有额外约束：只有尚未向 LangGraph/前端发出任何文本、reasoning 或 function-call delta
时才允许透明重试。一旦已经发出任意 delta，后续断流立即以 `response.failed` 结束，不能自动
重试并产生重复内容。所有透明传输重试都强制关闭推理。结构化输出解析失败不属于传输重试，
按任务性质分为两种可观测策略：任务书、大纲、文章按 `ARK_STRUCTURED_OUTPUT_MAX_REPAIRS` 将无效
输出和校验错误交给模型修复；流程判断、联网搜索和其他结构化调用按
`ARK_STRUCTURED_OUTPUT_MAX_RETRIES` 丢弃无效输出并使用原始输入重新执行。两类重试都强制关闭推理。

所有 Agent 循环都有独立可配置硬上限，例如：

- `ARTICLE_MAX_RETRIEVAL_ROUNDS`；
- `MATERIAL_QUERY_GROUPS_PER_DOCUMENT`；
- `CLARIFICATION_MAX_ROUNDS`；
- `ARK_STRUCTURED_OUTPUT_MAX_REPAIRS`。
- `ARK_STRUCTURED_OUTPUT_MAX_RETRIES`。

达到上限后节点必须成功进入其明确定义的结束/降级路径，或发送稳定错误码并失败；禁止无界重试。

#### 熔断

直接复用现有检索服务 `LLMCircuitBreaker` 的 closed/open/half-open 状态机：

- `ARK_CIRCUIT_BREAKER_ENABLED=true`；
- `ARK_CIRCUIT_FAILURE_THRESHOLD=3`：连续三次基础设施类失败后 open；
- `ARK_CIRCUIT_RECOVERY_SECONDS=30`：冷却后只允许一个 half-open 探针；
- 探针成功关闭熔断，失败重新 open；
- connect、timeout、429 和 provider 5xx 计入失败；
- invalid response、schema 校验失败、内容安全拒绝和调用方取消不计入 provider 熔断。

熔断器按 provider endpoint/model 维度维护，避免一个模型故障阻断所有模型；进程重启后状态重置是
V1 可接受行为。circuit open 时不进入 provider 队列，立即返回可重试的
`ARK_CIRCUIT_OPEN`。

#### `context_management` 和 caching

这两项属于 Ark provider 优化，不改变 Graph State、artifact revision 或 HITL 语义：

- `ARK_CONTEXT_MANAGEMENT_ENABLED=false`、`ARK_CACHING_ENABLED=false` 默认关闭；
- 实现前使用当前生产 endpoint/model 做真实 capability probe，验证具体请求字段、SSE、usage、
  strict tools 和 thinking 组合；不根据其他模型或 OpenAI 字段名猜测；
- provider 只在 probe 通过且配置开启时发送相应字段；若方舟明确返回“字段/能力不支持”，本次调用
  移除优化字段后重试一次普通无状态请求，并在进程内将该能力标记为不可用，避免每次重复探测；
- context management 只用于单次 article 工具循环等重复携带大前缀的模型调用，不代替本服务的
  context budget、裁剪、压缩和 checkpoint；
- caching 优先利用相同 system prompt、task spec、outline、material 前缀，动态用户反馈和工具结果
  放在稳定前缀之后；prompt 构造必须确定性，避免无意义地破坏缓存键；
- 记录 provider usage 中实际返回的 cached token/cache hit 信息；未返回时记为 unknown，不自行
  推算命中率；
- `store=false` 和“不使用 Ark previous_response_id”的既定边界保持不变，除非 capability probe
  证明某项上下文管理能力要求改变该边界，并经单独设计评审确认。

capability 优化失败不能导致业务失败；关闭优化后仍使用当前显式上下文完成同一模型调用。

### 15.2 Reasoning 输出

V1 不摘要、不隐藏、不改写上游模型实际返回的 reasoning 文本。adapter 只做事件和 ID 映射，
不把系统 prompt 或完整模型请求作为 reasoning 额外发送。

Reasoning 使用 Responses 标准事件，不占用三个自定义事件。根据上游实际支持情况映射：

```text
response.reasoning_summary_part.added
response.reasoning_summary_text.delta
response.reasoning_summary_text.done
response.reasoning_summary_part.done
response.reasoning_text.delta
response.reasoning_text.done
```

普通面向用户文本仍使用 `response.output_text.delta/done`。adapter 不得把 reasoning delta 混入
普通 output text。火山方舟的字段或事件若与 OpenAI 不同，由 `ArkResponsesChatModel` 适配；
实现前以真实方舟流做 contract test，不凭字段名猜测。

thinking policy 由 Gateway 根据 phase allowlist 强制执行：

| phase | thinking |
| --- | --- |
| `task_spec_generate` | `ARK_THINKING_TASK_SPEC` 配置 |
| `outline_generate` | `ARK_THINKING_OUTLINE` 配置 |
| `article_generate` | `ARK_THINKING_ARTICLE` 配置 |
| 其他所有 phase | 强制 disabled |

“其他所有 phase”包括 intent router、pending preflight、session memory、orchestrator、研读充分性
判断、文档/素材过滤、query 生成、task spec/outline/article 的辅助校验或压缩、图片位置和 prompt
生成。配置只接受当前方舟契约支持的枚举值；即使节点误传 enabled，Gateway 也必须覆盖为 disabled。

### 15.3 工具

需要展示生命周期的能力封装为 LangChain Tool，并由 ToolNode 或等价 Runnable 执行：

- `get_document_meta`
- `route_documents`
- `get_document_raw`
- `retrieve_document_materials`
- `retrieve_article_evidence`
- `generate_image`

artifact repository、checkpoint、HTML renderer 等确定性内部能力保持普通服务/node，不伪装成
模型工具。工具事件对前端只发送裁剪后的参数和结果摘要。

### 15.4 提示词和结构化输出规范

所有 LLM system prompt 都是可审查的正式代码资产，集中放在 `app/prompts/` 中，以有语义的显式
常量或模板导出，例如 `DOCS_RESEARCH_SUFFICIENCY_SYSTEM_PROMPT`。禁止把关键 system prompt
零散拼接在 node、HTTP client 或匿名 lambda 中，也禁止只把 prompt 放在外部平台而不在仓库中
保留可读版本。

每个 system prompt 按任务需要使用清晰的 Markdown 分区。标题不要求所有 prompt 完全一致，但
至少应覆盖适用的以下部分：

```text
# 角色与任务
# 输入说明
# 执行步骤或判断标准
# 输出契约
# 示例
# 禁止事项
```

动态业务内容通过命名清楚的 user/runtime message 注入，不用字符串替换修改 system prompt 的规则
语义。prompt 模板需要单元测试关键分区、变量完整性和 schema 名称；较大改动应能通过 Git diff
直接审查。

输出遵守“能结构化就不解析自然语言”的规则：

- 分类、路由、充分性判断、session memory、query 规划、图片位置选择等使用 strict JSON Schema；
- task spec 使用 strict 结构化 artifact；
- outline 和 article 虽然 artifact 内容是 Markdown，最终提交仍使用 strict schema/tool，例如
  `{user_facing_message, markdown}`，而不是从普通文本中猜测 Markdown 与解释的边界；
- 所有模型工具 schema 都设置 strict，并设置 `additionalProperties=false`；
- schema 校验失败走独立的结构化输出 repair 上限，不做正则截取或宽松 JSON 修补；
- 只有契约明确标记为 `user_facing_message`、`reason` 或同类字段的内容才由 adapter 转成
  `response.output_text.delta`；其他内部判断字段不直接展示给用户；
- reasoning 仍按第 15.2 节的标准事件独立转发，不当作解释字段。

当模型需要引用当前运行期已知 ID 时，优先动态生成 strict schema，把合法值限制在本次候选集合，
避免模型凭字符串生成不存在的 ID。例如素材过滤可以为当前 `material_id` 动态生成
`additionalProperties=false`、所有属性均 required 的布尔对象：

```json
{
  "material_001": true,
  "material_002": false
}
```

候选集合过大、不适合展开为属性时，改用 `items.enum=[...]` 的 ID 数组并由代码补全未返回项。
动态 schema 只能包含服务端已经验证的 ID；输出后仍由代码校验集合相等性、重复项和数量上限。
实现前用目标 Doubao 模型为 dynamic schema、strict tools、thinking 的组合建立 contract test。

面向用户解释字段的最低契约：

| phase | 结构化输出中的公开字段 | 用途 |
| --- | --- | --- |
| pending preflight | `reason` | 解释 replay/resume/supersede 判断 |
| orchestrator | `reason` | 解释为什么从某个业务阶段重跑 |
| research sufficiency/clarification | `user_facing_message` | 一次性说明缺什么、推荐选项和为什么要确认 |
| article retrieve tool | `user_facing_message` | 解释为什么还需要补搜，并与 strict queries 同时返回 |
| task spec/outline/article submit | `user_facing_message` | 说明本次生成或修改重点，与完整 artifact 同时提交 |
| ai image planning | `user_facing_message` | 简述选择了哪些配图位置；图片工具状态仍由代码发送 |

adapter 只转发以上 schema 已声明字段。若字段在完整 strict 输出校验后才可获得，adapter 可以再按
小块发送 `response.output_text.delta`，由前端形成稳定打字机效果；不能为了提前展示而流式解析
尚未闭合的 JSON。节点开始、排队、工具运行和降级状态由代码立即发送 `agent.activity`，保证模型
尚未返回时用户仍能看到真实进度。

## 16. Agent Server 事件归一化

LangGraph Agent Server Protocol v2 是内部协议，不直接透传前端。`GraphRuntimeClient` 封装
thread/run/stream/Command 操作，避免 adapter 散落 Agent Server 具体 endpoint。内部 run ID 只在
当前活跃执行期间使用；为支持跨请求 cancel，当前值暂存在 artifact 的
`active_runtime_run_id`，到达 interrupt 或终态立即清空，不建立历史映射。

`AgentEventNormalizer` 是唯一解析内部事件的组件：

| 内部来源 | 归一化结果 |
| --- | --- |
| run/response 生命周期 | Responses lifecycle |
| model message chunks | output text / reasoning 标准事件 |
| graph node start/end/error | `agent.activity(kind=node)` |
| tool start/end/error | `agent.activity(kind=tool)` |
| artifact custom event | `agent.artifact` |
| interrupt/custom pending | `agent.interrupt` |

不要在 Responses adapter 和未来 Chat Completions adapter 中分别解析 LangGraph 原始字段。

## 17. Responses-like API

### 17.1 Endpoint

```text
POST /v1/responses
```

V1 的 create、后续业务 revise、显式 HITL 审批、“继续”和 pending 状态下的普通自由输入全部
使用同一个 endpoint。V1 不提供 `/resume`、Response 查询或事件重放接口；主动取消使用第 17.6 节
单独的资源操作，不能伪装成新的用户消息。

### 17.2 Create/revise 请求

```json
{
  "model": "wechat-article-agent",
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "根据这些文档生成一篇面向金融从业者的公众号文章"
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
  },
  "stream": true
}
```

`input` 是前端管理的完整历史。项目字段只能放在顶层扩展 `context`。`session_id` 必填；
`doc_ids/temp_doc_ids` 首次创建至少一个，后续 revise 可以沿用当前 artifact revision 的参考范围，
也可以显式提供新范围。`context.debug` 默认 false，只有第 20.1 节的服务端开关也开启时才生效；
它是本 response 的 runtime 选项，不写入 Graph State/checkpoint，也不影响后续 resume 的默认值。

### 17.3 显式 HITL 审批请求

```http
POST /v1/responses
Content-Type: application/json
```

```json
{
  "model": "wechat-article-agent",
  "previous_response_id": "resp_01...",
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "将第二节拆成两个小节，并增加风险提示"
        }
      ]
    }
  ],
  "context": {
    "session_id": "frontend-session-1",
    "hitl": {
      "interrupt_id": "int_01...",
      "decision": "revise"
    }
  },
  "stream": true
}
```

`previous_response_id` 表示本次 Response 与上一个 Response 的关联，不等于 `interrupt_id`，也不
用于让后端拼接历史。前端已经在 `input` 中传入完整历史，adapter 不应重复装配前一 Response。

adapter 使用 `session_id` 派生 thread，读取 checkpoint 中唯一 pending interrupt，并校验：

- checkpoint 的 pending `response_id` 等于 `previous_response_id`；
- pending `interrupt_id` 等于 `context.hitl.interrupt_id`；
- revision 和 `artifact_id` 仍然是当前值；
- `decision` 与 interrupt form schema 匹配。

通过后生成新的 `response_id`，将 decision、反馈和新 `response_id` 一起转换为
`Command(resume=...)`。任一条件不匹配返回 409 `STALE_INTERRUPT`。review node 恢复后将新的
`response_id` 写入 Graph State，继续同一个产品 `run_id/thread_id`。

### 17.4 Response、run 和 interrupt

一个业务 run 可以产生多个 response：

```text
run-1
  response-1 -> task_spec interrupt
  response-2 -> outline interrupt
  response-3 -> article interrupt
  response-4 -> final HTML
```

到达 HITL 时，当前流正常发送 `response.completed`，但 metadata 明确表示：

```json
{
  "workflow_status": "waiting_for_input",
  "pending_interrupt_id": "int_01..."
}
```

因此 `response.completed` 只代表当前 SSE response 片段结束，不代表完整 workflow 已完成。

### 17.5 pending 状态的普通输入

请求未携带 `context.hitl` 但 checkpoint 有 pending interrupt 时，进入第 6.2 节 preflight。
“继续”或无法判断时只重新发送当前 artifact 字段和 `agent.interrupt`；当前反馈转成 resume；明确
上游变化先 supersede 旧运行，再创建新 revision。

重新展示审批卡片不是新的图执行，也不创建新的 Response 关联。adapter 复用 checkpoint 中该
pending interrupt 原有的 `response_id/interrupt_id` 重新发送 artifact 和卡片，前端按 ID 原位
upsert。只有用户真正提交 approve/regenerate/revise 并恢复图时，才生成新的 `response_id`。

### 17.6 Cancel endpoint

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

这是 Responses-like 的正式扩展操作，目标是当前正在运行或等待 HITL 的 response。它不创建新的
业务需求、revision 或 response，不要求前端把“取消”追加为用户对话，也不把 cancel 当成
`POST /v1/responses` 的 input。adapter 根据 session 派生 thread，持有相同 advisory lock，并校验：

- 当前 artifact 的 `current_response_id` 等于 path 中的 `response_id`；
- 当前 revision 状态是 `running/waiting_for_input/cancelling`；
- 当前 thread/runtime run 与 artifact 控制字段一致。

行为：

1. `running`：先将 artifact 条件更新为 `cancelling`，再通过 `GraphRuntimeClient` 对
   `active_runtime_run_id` 发起 Agent Server cancel；轮询或读取 runtime 终态确认后写
   `cancelled` 并清空 `active_runtime_run_id`。
2. `waiting_for_input`：先条件更新为 `cancelling`，再使用 pending interrupt 的内部控制恢复值
   `{type: "cancel"}` 创建一次短暂 runtime run，并把它写入 `active_runtime_run_id`；interrupt node
   只返回 cancelled 路由，不执行生成或工具节点，随后 artifact/checkpoint 进入 `cancelled` 并清空
   runtime ID。提交或控制字段写入失败同样执行补偿 cancel。
3. 已经 `cancelled`：幂等返回当前 cancelled 表示；已 completed/failed/superseded 或 response ID
   已过期：返回 409 `RESPONSE_NOT_CANCELLABLE` 或 `STALE_RESPONSE`。

成功响应为普通 JSON，不开启 SSE：

```json
{
  "id": "resp_01...",
  "object": "response",
  "status": "cancelled",
  "run_id": "run_01...",
  "artifact_id": "art_01..."
}
```

如果 Agent Server 已接受取消但尚未确认终态，返回 202 和 `status=cancelling`；前端暂时禁用同
session 的发送、审批以及重复的用户点击。客户端可以按退避策略查询/重试同一个 cancel 请求，
但不能发起新的业务请求。cancel endpoint 本身是幂等操作，同一 response 的重复 cancel 不启动
新 runtime run。

服务还运行一个轻量 cancellation reconciler，按 `CANCEL_RECONCILE_INTERVAL_SECONDS` 小批量扫描
现有 `article_artifacts.status=cancelling` 行：查询对应 runtime run，已终止则条件更新为 cancelled；
仍运行则幂等补发 cancel；runtime 不存在或状态无法解释则记录稳定错误并继续下一轮。这样 202 后
即使客户端不再请求，也不会永久依赖用户点击。reconciler 使用 `FOR UPDATE SKIP LOCKED` 和同一
条件更新规则，不增加 cancel 业务表，也不能把自然 completed/failed 覆盖成 cancelled。

收到取消后，Gateway、retrieval 和 Seedream 的 request-scoped cancellation token 必须停止排队中
任务，并尽力取消在途 HTTP 流。各 node 在昂贵调用前后和 artifact 写入前检查取消状态；取消确认
后不得再调用模型/工具、进入 HITL 或覆盖 artifact 字段。provider 已经完成但取消前尚未持久化的
结果直接丢弃。V1 不承诺撤回 provider 已经计费的调用，但必须避免后续 token 和下游工作。

外部 endpoint 的路径借用 Responses 资源风格，但本项目不声称完全兼容 OpenAI 的 background
response cancel 语义。Agent Server 的具体 cancel endpoint、状态枚举和竞态行为必须用最终部署
版本做 contract test，并只封装在 `GraphRuntimeClient` 中。

## 18. SSE 事件协议

三个自定义事件足以覆盖 V1，统一使用跨 Agent 命名：

```text
agent.activity
agent.artifact
agent.interrupt
```

不得随业务节点数量新增事件类型。微信文章和 PPT 的领域差异放在 payload 字段和枚举值中。

### 18.1 标准事件

V1 至少支持：

```text
response.created
response.output_item.added
response.output_text.delta
response.output_text.done
response.output_item.done
response.completed
response.failed
```

以及第 15.2 节列出的 reasoning 事件。adapter 生成外部 response/item/content ID，不透传方舟
或 Agent Server 的内部 response 生命周期 ID。

### 18.2 `agent.activity`

节点和工具共用：

```json
{
  "type": "agent.activity",
  "schema_version": "1",
  "run_id": "run_01...",
  "response_id": "resp_01...",
  "activity": {
    "id": "call_route_docs_01",
    "kind": "node | tool",
    "name": "route_documents",
    "label": "多文档路由",
    "status": "running | completed | degraded | cancelled | failed",
    "node": "docs_research",
    "input_summary": {"document_count": 18},
    "output_summary": null,
    "error": null
  }
}
```

同一个 `activity.id` 原位更新。只展示有用户意义的节点和重要工具，不发送内部函数、完整文档
正文、完整 Graph State 或未裁剪工具结果。

### 18.3 `agent.artifact`

```json
{
  "type": "agent.artifact",
  "schema_version": "1",
  "run_id": "run_01...",
  "response_id": "resp_01...",
  "artifact": {
    "id": "art_revision_03",
    "revision": 3,
    "stage": "outline",
    "status": "pending_review",
    "content": "# 标题\n...",
    "summary": null
  }
}
```

`artifact.id` 是聚合 artifact ID，`stage` 表示本次事件更新的是哪一个字段，允许
`material_library/task_spec/outline/article_markdown/images/final_html`。审批所需内容应在到达
interrupt 时完整发送；过大的素材库只发送 summary，不提供对外 artifact 查询接口。

### 18.4 `agent.interrupt`

```json
{
  "type": "agent.interrupt",
  "schema_version": "1",
  "run_id": "run_01...",
  "response_id": "resp_01...",
  "interrupt": {
    "id": "int_01...",
    "status": "pending | resolved",
    "stage": "outline_review",
    "artifact_id": "art_revision_03",
    "revision": 3,
    "form": {
      "form_type": "agent_artifact_review",
      "title": "请审核文章大纲",
      "description": "确认后将进入正文生成阶段",
      "fields": []
    }
  }
}
```

文档澄清使用 `form_type=agent_clarification`；审批使用
`form_type=agent_artifact_review`。表单完全 JSON 可序列化，并由前后端共享 versioned schema。

### 18.5 SSE 通用字段

每个公开事件至少包含：

```json
{
  "run_id": "run_01...",
  "response_id": "resp_01...",
  "timestamp": "2026-08-12T08:00:00Z"
}
```

SSE 的 `event:` 与 JSON `type` 相同。事件不分配持久化 sequence，也不写数据库。未知自定义
事件必须被前端忽略，新增可选字段不提升主 schema version；删除字段、改变字段含义或枚举语义
需要提升主版本。

run 在原 SSE 仍连接时被正式 cancel，adapter 结束尚未完成的 activity 为 `cancelled`，发送一次
既有 `response.completed` 后关闭流，其 response payload 明确包含
`status=cancelled/workflow_status=cancelled`；用户主动取消不发送 `response.failed`。这与 HITL 时
`response.completed` 仅代表当前流片段结束的既定语义一致，也不增加第四种自定义 SSE 事件。如果
原 SSE 已断开，cancel endpoint 的 JSON 是唯一即时确认；V1 不持久化或补发该终态事件。

## 19. 断线、恢复、取消和错误

### 19.1 SSE 断线

客户端断开 SSE 不等于取消 Agent run。已经提交给 Agent Server 的后台运行继续执行，直至完成、
失败或到达 interrupt；但 V1 不保存产品 SSE 事件，也不提供接续原实时流的重放接口。

V1 的恢复语义是“恢复业务状态”，不是“恢复中间展示过程”：

- 断线期间的 `response.output_text.delta`、reasoning delta、`agent.activity` 和临时工具状态会丢失，
  不补发；
- 已写入 `article_artifacts` 的完整阶段产物不会丢失；
- LangGraph checkpoint 中的当前节点、图状态和 pending interrupt 不会丢失；
- 前端使用同一个 `session_id` 再次调用 `POST /v1/responses`；
- 若已经到达 HITL，adapter 读取 checkpoint，重新发送当前审批所需的 artifact 内容和同一张
  `agent.interrupt` 卡片，不重新生成候选；
- 若已经完成，adapter 重新发送当前 revision 的最终 artifact 快照；
- 若 Agent Server 报告同一 thread 仍在运行，adapter 返回明确的“仍在运行”状态或 409，V1 不
  启动第二个并发 run，也不承诺重新附着到旧 run 的 token 流。

因此用户可以继续审批或查看最终结果，但看不到断线期间错过的 Codex 式活动时间线。这是 V1 为
降低 PostgreSQL 写入和协议复杂度而接受的限制。

### 19.2 “继续”和自由输入

- “继续”：preflight 复用待审 `response_id`，输出简短 reason，并重新发送当前 artifact 和 pending
  interrupt；
- 当前 artifact 反馈：转换为 resume；
- 上游变化：旧 run supersede 后创建新 revise run；
- 模糊输入：保守 replay。
- 最近 run 已 cancelled 且只输入“继续/重试”：不恢复旧 checkpoint，重新展示 cancelled 状态和最后
  已持久化 artifact；
- 最近 run 已 cancelled 后提出真实新增/修改：创建新 revision，由 orchestrator 选择入口阶段。

### 19.3 服务或 worker 异常

PostgreSQL checkpointer 保存图状态。关键节点使用同步 durability；进程恢复后从最近 checkpoint
继续。由于节点可能重放，artifact 字段更新和外部工具副作用必须可重试；pending 由 checkpoint
中的 interrupt 表达，不另行落业务表。

### 19.4 Cancel

V1 提供第 17.6 节正式 cancel endpoint。取消 SSE 接收与取消 run 是两个不同动作：前者不改变
runtime，后者必须通过 Agent Server 和 request-scoped cancellation token 尽快停止实际工作。
cancelled 与 superseded 的 checkpoint/artifact 不立即物理删除，等待 TTL；它们不可通过“继续”
复活。cancel 后的新业务输入创建新 revision；旧 revision 只能按 `approved_through_stage` 继承取消
前已经批准的上游 artifact，未批准候选和取消过程中迟到的结果都不能成为新 revision 的来源。

### 19.5 错误分类

错误处理遵守以下顺序：先在当前组件内执行有界重试；重试耗尽后只有存在明确定义且不歪曲业务
语义的路径才降级；没有可靠降级时失败。不得把“没有取到证据”伪装成“文档没有相关内容”，也
不得把未完整生成的 artifact 送入 HITL 让用户误以为它可审批。

所有错误携带稳定 `code`、用户可读 `message`、`retryable`、`stage` 和 `request_id`，不得向正式
前端发送 API Key、完整 prompt、完整原文、provider 原始 body 或 Python traceback。HTTP/SSE 边界：

- 在 `response.created` 之前发现请求、认证、并发或 stale interrupt 错误时，直接返回 4xx/5xx JSON；
- 已经发送 `response.created` 后发生错误，发送一次 `response.failed` 后结束 SSE；
- 失败时将同一稳定错误摘要写入 `article_artifacts.last_error`；debug trace 只按第 20 节的双开关返回。

### 19.6 降级、错误和重试矩阵

#### Admission、状态与持久化

| 环节/异常 | 重试或降级 | 最终行为 |
| --- | --- | --- |
| 请求 schema、model、session 非法 | 不重试 | 400；不创建 thread/run/response |
| 同 session 已有运行中 run | 不启动第二个 run | 409 `SESSION_RUN_IN_PROGRESS` |
| stale、重复或并发审批 | 不重试旧审批 | 409 `STALE_INTERRUPT`；前端可重新展示当前状态 |
| cancel 与 run 终态竞态 | 条件更新 response/revision；以 runtime 已确认终态为准 | completed/failed 已先发生则 409；cancel 先确认则 cancelled |
| cancel 请求已接受但未确认 | 保持 `cancelling`，重试同一 cancel | 202；同 session 暂停新 run 和审批 |
| cancelled 后仅输入“继续/重试” | 不允许恢复 cancelled checkpoint | 重发 cancelled 状态和最后 artifact |
| pending preflight 分类失败或低置信度 | 降级为 replay | 不 resume、不 supersede，重放原审批卡片 |
| artifact/checkpointer 短暂连接失败 | 使用数据库连接层有限重试 | 耗尽后 503；禁止只写一边继续 |
| checkpoint 与 artifact revision/stage 不一致 | 不自动修复或猜测 | `STATE_ARTIFACT_MISMATCH`，保留现场并失败 |
| artifact 候选写入失败 | 有限 DB 重试 | 不进入 interrupt，不发送可审批卡片 |
| 最终 HTML 写入失败 | 有限 DB 重试 | 不发送完成事件；保留已批准 Markdown/图片并失败 |
| TTL cleanup 删除 thread 失败 | 下轮 cleanup 重试 | 本轮不删除对应 artifact，避免只剩孤儿 checkpoint |

#### Ark、结构化输出与上下文

| 环节/异常 | 重试或降级 | 最终行为 |
| --- | --- | --- |
| connect、首事件 timeout、408/429/5xx | 仅首个 delta 前按第 15.1 节有限重试 | 耗尽后可重试 `response.failed` |
| 已输出任意 delta 后断流 | 禁止透明重试 | 标记同 revision 可重试，避免重复文本/工具调用 |
| strict 输出不合法 | 新的可观测 repair 调用，最多 `ARK_STRUCTURED_OUTPUT_MAX_REPAIRS` | 按下表节点级规则降级或失败 |
| circuit open | 不进入 provider 队列 | `ARK_CIRCUIT_OPEN`，`retryable=true` |
| context management/cache 不支持 | 去掉优化字段后重试一次普通无状态请求 | 进程内标记能力不可用，业务继续 |
| 上下文超限 | 去重、裁剪低价值检索结果，再压缩工具历史 | 不裁 task spec/outline/反馈；仍超限则 `CONTEXT_BUDGET_EXCEEDED` |
| provider 内容安全拒绝 | 不做相同请求重试 | 保留上游 artifact，提示用户调整需求 |

#### 轻量决策节点

| 节点/异常 | 降级策略 | 限制 |
| --- | --- | --- |
| `session_memory` 最终失败 | 使用包含六个空字符串的固定对象继续 | 记录 degraded activity；不阻断主链路 |
| `intent_router` 最终失败 | 仅当 endpoint、文档范围和明确文章指令均匹配时由规则进入文章 | 仍有歧义则失败，不擅自路由其他 Agent |
| `orchestrator` 最终失败 | 真实业务修改保守从 `docs_research` 开始 | 牺牲成本换正确性，不从下游猜测入口 |
| pending preflight 最终失败 | replay 当前 interrupt | 不丢弃审批、不执行用户自由输入 |
| 研读充分性判断失败 | 不声称信息充分 | 模型服务不可用则失败；恢复后由用户明确重试 |
| 自定义研读方向达到最大澄清轮次 | 采用最后一次 about/target 强制开始研读 | 记录降级说明，不返回错误 |
| query 规划失败 | 以已确认主题/任务书生成一组确定性宽查询 | 只降级一次，并标记 query planning degraded |
| 素材过滤失败 | 保留所有当前有效素材 | 绝不因分类失败删除证据 |

#### 检索和文档研读

| 接口/异常 | 重试或降级 | 最终行为 |
| --- | --- | --- |
| `/meta` timeout/503 | retrieval client 有限重试 | 耗尽后失败，不能在无元信息下路由 |
| `/meta` 返回 missing IDs | 不重试不存在的 ID | 整体失败并返回缺失 ID 列表 |
| `/route` timeout/429/503/无效输出 | 有限重试后把请求范围内全部文档视为 possible | 召回优先继续，记录 degraded/warning |
| `/route` 200 + degraded | 接受 accept/possible 并传播 warning | 不得把 degraded 记为完整路由 |
| `/raw` 202 | 按 `retry_after` 轮询，受最大次数和总时限约束 | 超时后进入 raw -> retrieve 降级 |
| `/raw` 422、修复失败或重试耗尽 | 对该文档调用 `/retrieve`，query 为“请详细、全面总结这篇文档，并保留关键事实、数据和结构” | 设置 `ensure_document_coverage=true` 和足够返回预算；标记 extraction_mode=retrieve_fallback |
| raw 内容超过 context 预算 | 不继续塞完整 raw | 直接改走长文档 `/retrieve` 路径 |
| `/retrieve` 部分 query 失败 | 保留成功组，失败组在轮数内缩小 query 补检 | 有有效素材时 partial 继续并传播 warning |
| retrieve coverage incomplete/truncated | 在上限内以更窄 query 补检 | 仍不完整则保留 coverage 元数据后继续 |
| 单篇文档全部提取失败 | 普通汇总允许排除该文档并告警 | 用户明确要求逐篇比较/全覆盖时整轮失败 |
| 全部相关文档无有效素材 | 无可靠降级 | `NO_USABLE_EVIDENCE`，禁止无依据生成 |
| 正文临时补搜失败 | material library 非空时仅使用已有素材 | 标记 article retrieval degraded；素材库为空则失败 |

#### Artifact 生成、图片与 HTML

| 节点/异常 | 重试或降级 | 最终行为 |
| --- | --- | --- |
| task spec/outline/article 生成最终失败 | 仅按 Ark 和 structured-output 上限重试 | 不用模板伪造候选；保留上游 artifact 并失败 |
| 单张 Seedream 失败 | 独立有限重试，保留其他成功 URL | failed activity；HTML 使用成功图片 |
| 全部 Seedream 失败 | 降级为无图 HTML | 完成但返回明确 warning，不阻断文章交付 |
| 图片 URL/schema 非法 | 丢弃该图片，不写 artifact | 按单张失败处理 |
| 主 HTML renderer 失败 | 用固定最小安全主题再渲染一次 | 成功则 degraded 完成；仍失败则保留 Markdown/图片并失败 |

以上三项是已经确认的 V1 产品基线，不是待实现者选择的建议：

1. 全部 Seedream 失败仍交付无图 HTML，并在 activity、最终 metadata 和 smoke 报告中明确 warning。
2. `document_coverage_mode=best_effort` 时，部分文档提取失败可用剩余证据继续；
   `all_required` 时任何指定文档最终无有效素材都必须失败，不能生成缺项的比较/全覆盖文章。
3. 主 HTML renderer 失败必须尝试一次固定最小安全主题；该 fallback 也失败才终止，已经批准的
   Markdown 和可用图片 URL 必须保留。

### 19.7 失败后的“继续/重试”

只有用户明确输入“继续”或“重试”，且 `last_error.recovery_action=resume_checkpoint` 时，adapter 才在
同一 `artifact_id/revision` 上重新执行失败阶段，并给该次用户主动重试一份新的有限尝试预算。
`new_revision` 创建新 revision 并由 orchestrator 路由；`needs_user_fix` 只重显错误。包含新增、删除、
改写等业务要求的普通输入始终创建新 revision。

失败重试生成新的 `response_id`，但不是 HITL，不创建虚假 `interrupt_id`，也不使用
`Command(resume=...)`。`Command(resume=...)` 只恢复真实 pending interrupt。Agent Server 对 failed
run 的具体 retry/rerun 调用和失败 checkpoint 游标语义必须用最终部署版本做 contract test，再固定
在 `GraphRuntimeClient` 中；不得在业务 node 中猜测 endpoint。

## 20. 本地开发面板和 debug trace

### 20.1 边界和开关

`dev/` 是独立的本地 FastAPI 调试服务和静态前端，不属于产品正式业务接口，也不随生产 Compose
启动。它参考 `src/rag-knowledge-chat/dev` 的组织方式和基础 CSS，但按 Responses-like、HITL 和
artifact 语义重新实现。

debug 采用双开关：服务进程必须设置 `DEBUG_ENABLED=true`，请求还必须显式携带
`context.debug=true`。只有两者同时满足，Agent runtime 才创建 request-scoped `DebugTraceCollector`。
任一条件不满足时：

- collector 为 `None`，node、LLM Gateway 和 tool 只走正常执行路径；
- 不复制 prompt/message/tool result，不构造 debug snapshot，不执行 debug JSON 序列化；
- 不增加 checkpoint、artifact、SSE 或常规日志内容；
- 响应中完全不存在 `debug` 字段。
- adapter 还必须以产品白名单过滤 artifact：非 debug 流只允许 `task_spec`、`outline`、
  `article_markdown`、`images` 和 `final_html`；`session_memory`、`material_library` 仅在双开关均开启时
  供开发面板观察。

`/docs` 和 `/openapi.json` 是 Apifox 等接口工具使用的契约入口，与 trace 开关无关；生产关闭
`DEBUG_ENABLED` 时仍保持可访问。网络访问控制应由网关和部署边界负责，不能通过开启 trace 换取
接口文档可访问性。

debug 数据只保存在当前 response 片段的进程内内存中，不写 PostgreSQL、checkpoint、artifact、
文件或常规日志。到达 HITL、workflow 完成或内部错误时，在终态 `response.completed` 或
`response.failed` 中一次性附带可选 `debug.trace`。内部 runtime 可以在 debug 开启时向 adapter
发送一个终态调试 envelope；adapter 消费后放入终态响应，它不是新的公开 `agent.*` 事件。

一个业务 run 横跨多个 HITL response 时，每个 response 返回自己的 trace 片段，开发面板按
`run_id/response_id` 在浏览器内聚合。SSE 在终态前断开时，该片段 debug 随连接丢失，V1 不持久化
或补发。这符合 debug 只服务当前本地调试的定位。可捕获的 node/LLM/tool 异常必须在
`response.failed` 前先完成 trace snapshot；进程被强制终止、机器宕机或 adapter-runtime 连接完全
中断时无法送达终态 trace，V1 不为此增加 debug 持久化。

### 20.2 Debug trace 契约

终态扩展示例：

```json
{
  "type": "response.completed",
  "response": {"id": "resp_01..."},
  "debug": {
    "schema_version": "1",
    "trace": {
      "request": {},
      "nodes": [],
      "llm_calls": [],
      "tool_calls": [],
      "fallbacks": [],
      "errors": [],
      "timings": {},
      "truncated": false
    }
  }
}
```

采集内容：

| 分类 | 内容 |
| --- | --- |
| request | request/run/response ID、入口类型、经过脱敏的完整请求 |
| nodes | 每个 node 的开始/结束时间、context 装配后的有效输入快照、原始返回值或异常 |
| llm_calls | phase/model/thinking、system prompt、messages、strict schema/tools、重试、usage、provider request ID、provider 原始事件和重组后的原始输出 |
| tool_calls | 工具名、原始参数、原始结果、状态、耗时和错误 |
| fallbacks | 原路径、触发原因、采用的降级路径、warning/error code |
| timings | graph、node、provider、queue、tool 和数据库耗时 |

“原始”表示业务代码实际发送或收到的结构，不代表无限大小或不做安全处理。collector 必须移除
API Key、Authorization、cookie、数据库 DSN 密码和其他 credential；循环引用或不可序列化对象用
稳定摘要表示。使用 `DEBUG_TRACE_MAX_BYTES` 和 `DEBUG_TRACE_VALUE_MAX_CHARS` 限制单次 trace，
超过时按“大型工具结果 -> node 大字段 -> provider 原始 chunk”顺序截断，并记录原始大小、截断
位置和 `truncated=true`。不能因为 debug trace 过大使正常 workflow 失败。

完整 prompt、文档原文和工具结果只可能出现在双开关开启的终态 trace，仍不得进入常规应用日志、
数据库或烟测报告。开发面板后端默认只监听 `127.0.0.1`，不得无认证暴露到公网。

双开关开启时，微信 Agent 的 `/retrieve` 请求设置 `options.include_debug=true`，并把检索响应中的
`debug` 作为该 tool call 的嵌套 trace 展示；关闭时固定传 false。`meta/raw/route` 若有自身 debug
字段同样只在开启时保留。嵌套 trace 仍受本服务统一脱敏和大小上限约束，不能把检索服务返回的
任意大对象无界复制到终态响应。

### 20.3 开发面板后端

`dev/server.py` 使用 FastAPI，默认监听 `127.0.0.1:8245`，提供本地静态页面和以下调试接口：

```text
GET  /                         静态开发面板
GET  /api/config               返回非敏感默认配置
POST /api/random-documents     读取 manifest、随机选 doc_id 并代理调用 meta
POST /api/responses            代理产品 POST /v1/responses 并原样流式转发 SSE
POST /api/responses/{id}/cancel 代理正式 cancel 并返回 JSON
```

默认配置：

```text
Agent URL:     http://127.0.0.1:8240
Retrieval URL: http://127.0.0.1:8220
Manifest file: /workspace/wechat-article-agent/wechat-article-documents.manifest.jsonl
User ID:       wechat-article-agent-dev-user
KB ID:         wechat-article-agent-dev-kb
```

manifest 输入是单个 JSONL 文件，不是知识库面板使用的 manifest 目录。后端逐行校验并仅从
`status=completed`、`doc_id` 非空的记录中无放回随机取样；生成随机文档时用配置的 user/kb/session
调用 `POST /rag/v1/documents/meta`，把 `doc_name/doc_description/doc_type/page_count/node_count/status`
和 manifest 中的文件名展示到预览区。missing ID 明确标红，不生成不可用请求。

面板后端只代理目标 URL，禁止从请求中接受任意非 HTTP(S) 协议、URL credential 或非本机/显式
allowlist host，避免它成为通用 SSRF 代理。上游 API Key 从本地配置读取，不回传浏览器。

### 20.4 三栏前端

左栏按从上到下排列：

1. Agent URL、retrieval URL、manifest 文件、user ID、kb ID、session ID、随机文档数量等配置。
2. 文档预览窗格和“生成随机文档”按钮。
3. “生成请求体”和“格式化”按钮。
4. 可编辑 JSON 请求体，以及“发送请求”“停止接收”“取消 run”三个按钮。

“生成请求体”使用当前预览中的 doc IDs 填入 `context.doc_ids`，设置完整的
`model/input/context/stream`，并默认添加 `context.debug=true`。开发者可以直接编辑用户输入和任意
其他字段。面板在浏览器内维护完整对话历史；点击 HITL 按钮时，使用同一个 `session_id`、完整
`input` 历史、`previous_response_id` 和当前 `interrupt_id` 再次调用同一个 endpoint。

中栏模拟产品前端：

- 按 `response.output_text.delta` 实时追加，形成打字机效果；
- reasoning 使用可折叠区域，与普通解释文本分开；
- `agent.activity` 按 activity ID 原位更新 running/completed/degraded/cancelled/failed 状态和耗时；
- `agent.artifact` 按 stage 原位更新任务书、大纲、Markdown、图片和 HTML；
- `agent.interrupt` 渲染澄清表单或 approve/revise/regenerate 按钮，revise 反馈可编辑；
- 最终 HTML 使用带 `sandbox` 且不允许脚本的 iframe `srcdoc` 直接渲染，同时保留源码查看入口；
- 卡片、工具条和固定格式区域使用稳定尺寸，避免流式内容导致布局跳动或遮挡。

右栏展示两层信息：实时的公开事件时间线和终态 debug trace。trace 至少提供 Node、LLM、Tool、
Fallback/Error、Raw SSE 标签页；LLM 卡片能查看 system prompt、messages、schema/tools、raw output、
usage 和重试；Tool 卡片能查看参数和结果；降级卡片突出原路径、原因和最终路径。

“停止接收”使用浏览器 `AbortController` 中止当前 SSE，并将发送区恢复为可编辑状态。它不调用
cancel：已提交的后台 run 继续运行。之后发送“继续”时，adapter 根据真实状态返回运行中 409、
重放 pending HITL、重发最终 artifact，或在失败且可重试时按第 19.7 节重跑。

“取消 run”使用当前 response ID 调用 `POST /api/responses/{id}/cancel`，由 dev backend 代理正式
cancel endpoint。它在 SSE 正常连接、已经停止接收或正在等待 HITL 时都可使用。点击后 UI 立即
显示 cancelling，禁用 HITL 决策和同 session 新发送；收到 200 cancelled 后将全部 running activity
原位更新为 cancelled，并保留已完成 artifact 供查看。收到 202 时允许再次查询/重试同一 cancel，
不能在浏览器本地假装已取消。

## 21. 配置项和环境模板

### 21.1 配置来源原则

`env/wechat-article.env` 是其他窗口遗留的实际环境文件，不是本项目的配置设计来源。其中 Redis、
idempotency、delivery buffer、旧 TTL 或旧并发变量与当前单表 + checkpointer 方案不兼容。实现时
只允许从中选择性提取仍有效的 secret，例如 `ARK_API_KEY`；所有非 secret 配置必须依据本文、
Settings 模型和重新建立的 `.env.example` 生成，不能复制或兼容遗留变量。

语言模型和 Seedream 共用同一个 `ARK_API_KEY`。`*_RESPONSES_URL` 表示完整 endpoint，而不是只到
`/api/v3` 的 SDK base URL，HTTP client 不得重复追加 `/responses`。所有 `*_MAX_RETRIES` 都表示首次
调用失败后最多额外尝试的次数，不包含首次调用。所有上限都有服务端硬边界，不能完全信任 env
中的异常值。

正式 `AppSettings` 与本地 `DevPanelSettings` 的总体变量集合至少包含：

```text
APP_ENV=production
APP_HOST=0.0.0.0
APP_PORT=8140
DEBUG_ENABLED=false
DEBUG_TRACE_MAX_BYTES=8388608
DEBUG_TRACE_VALUE_MAX_CHARS=1000000
APP_MAX_ACTIVE_RUNS=30
APP_MAX_QUEUED_RUNS=90
APP_ADMISSION_WAIT_TIMEOUT_SECONDS=120
DATABASE_URL
DB_POOL_MIN=1
DB_POOL_MAX=30
DB_CONNECT_TIMEOUT_SECONDS=5
DB_POOL_ACQUIRE_TIMEOUT_SECONDS=10
DB_QUERY_TIMEOUT_SECONDS=60
DB_MAX_RETRIES=2
CANCEL_CONFIRM_TIMEOUT_SECONDS=10
CANCEL_RECONCILE_INTERVAL_SECONDS=15
CANCEL_RECONCILE_BATCH_SIZE=20
WECHAT_AGENT_THREAD_NAMESPACE
AGENT_SERVER_URL
ARTIFACT_TTL_HOURS=72
TTL_CLEANUP_INTERVAL_SECONDS=300
TTL_CLEANUP_BATCH_SIZE=50

ARK_API_KEY
ARK_RESPONSES_URL=https://ark.cn-beijing.volces.com/api/v3/responses
ARK_MODEL_FAST=doubao-seed-2-1-pro-260628
ARK_MODEL_MAIN=doubao-seed-2-1-pro-260628
ARK_CONTEXT_WINDOW
ARK_MAX_CONCURRENCY=30
ARK_MAX_QUEUED_CALLS=180
ARK_PER_RUN_MAX_IN_FLIGHT=6
ARK_QUEUE_WAIT_TIMEOUT_SECONDS=120
ARK_CONNECT_TIMEOUT_SECONDS=10
ARK_FIRST_EVENT_TIMEOUT_SECONDS=60
ARK_STREAM_IDLE_TIMEOUT_SECONDS=90
ARK_CALL_MAX_SECONDS=600
ARK_MAX_RETRIES=2
ARK_RETRY_BASE_SECONDS=1
ARK_STRUCTURED_OUTPUT_MAX_REPAIRS=1
ARK_CIRCUIT_BREAKER_ENABLED=true
ARK_CIRCUIT_FAILURE_THRESHOLD=3
ARK_CIRCUIT_RECOVERY_SECONDS=30
ARK_RATE_LIMIT_PER_MINUTE=3000
ARK_RATE_LIMIT_BURST=600
ARK_RATE_LIMIT_BUCKET_TTL_SECONDS=900
ARK_CONTEXT_MANAGEMENT_ENABLED=false
ARK_CACHING_ENABLED=false
ARK_THINKING_TASK_SPEC
ARK_THINKING_OUTLINE
ARK_THINKING_ARTICLE

REQUEST_RATE_LIMIT_PER_MINUTE=1200
REQUEST_RATE_LIMIT_BURST=300
REQUEST_RATE_LIMIT_BUCKET_TTL_SECONDS=900

RETRIEVAL_BASE_URL
RETRIEVAL_CONNECT_TIMEOUT_SECONDS=10
RETRIEVAL_TIMEOUT_SECONDS=180
RETRIEVAL_MAX_CONCURRENCY
RETRIEVAL_MAX_RETRIES=2
RETRIEVAL_RETRY_BASE_SECONDS=1
RETRIEVAL_CIRCUIT_BREAKER_ENABLED=true
RETRIEVAL_CIRCUIT_FAILURE_THRESHOLD=3
RETRIEVAL_CIRCUIT_RECOVERY_SECONDS=30
RAW_REPAIR_MAX_WAIT_SECONDS=600
SHORT_DOCUMENT_MAX_CHARS
MATERIAL_QUERY_GROUPS_PER_DOCUMENT=3
ARTICLE_MAX_RETRIEVAL_ROUNDS=3
CLARIFICATION_MAX_ROUNDS=3

SEEDREAM_RESPONSES_URL=https://ark.cn-beijing.volces.com/api/v3/images/generations
SEEDREAM_MODEL=doubao-seedream-5-0-260128
SEEDREAM_MAX_CONCURRENCY
SEEDREAM_CONNECT_TIMEOUT_SECONDS=10
SEEDREAM_CALL_MAX_SECONDS=600
SEEDREAM_MAX_RETRIES=2
SEEDREAM_RETRY_BASE_SECONDS=1
SEEDREAM_CIRCUIT_BREAKER_ENABLED=true
SEEDREAM_CIRCUIT_FAILURE_THRESHOLD=3
SEEDREAM_CIRCUIT_RECOVERY_SECONDS=30
IMAGE_MAX_COUNT

DEV_PANEL_HOST=127.0.0.1
DEV_PANEL_PORT=8245
DEV_RETRIEVAL_BASE_URL=http://127.0.0.1:8220
DEV_DATASET_MANIFEST_FILE=/workspace/wechat-article-agent/wechat-article-documents.manifest.jsonl
```

开发面板默认的 Agent 地址、`user_id`、`kb_id` 和 `session_id` 统一写在
`dev/config.yaml`，由面板后端读取后通过 `/api/config` 返回给浏览器，不再使用
`DEV_AGENT_BASE_URL`。

### 21.2 根生产模板

根 `env/wechat-article.env.example` 面向真实 Compose/生产部署，必须使用容器 DNS 和生产端口，容量
默认至少支持约 30 个并发活跃 run：

```text
APP_ENV=production
APP_HOST=0.0.0.0
APP_PORT=8140
DEBUG_ENABLED=false
APP_MAX_ACTIVE_RUNS=30
APP_MAX_QUEUED_RUNS=90
DB_POOL_MAX=30
CANCEL_CONFIRM_TIMEOUT_SECONDS=10
CANCEL_RECONCILE_INTERVAL_SECONDS=15
AGENT_SERVER_URL=http://wechat-article-runtime:8141
RETRIEVAL_BASE_URL=http://rag-retrieval-service:8120
ARK_RESPONSES_URL=https://ark.cn-beijing.volces.com/api/v3/responses
ARK_MODEL_FAST=doubao-seed-2-1-pro-260628
ARK_MODEL_MAIN=doubao-seed-2-1-pro-260628
ARK_MAX_CONCURRENCY=30
SEEDREAM_RESPONSES_URL=https://ark.cn-beijing.volces.com/api/v3/images/generations
SEEDREAM_MODEL=doubao-seedream-5-0-260128
SEEDREAM_MAX_CONCURRENCY=8
DATABASE_URL=postgresql://<user>:<password>@<host>:5432/wechat_article_agent
ARK_API_KEY=<ark-api-key>
```

根模板必须包含第 21.1 节所有正式运行变量及安全占位值，但不包含 `DEV_*`。任何容器间 URL 和
PostgreSQL DSN 不得使用 `localhost/127.0.0.1`。生产值可以按实际 provider 配额下调单项外部调用
并发，但上线验收环境的 `APP_MAX_ACTIVE_RUNS` 不能低于 30；队列容量至少是活跃 run 的三倍。

### 21.3 子项目本地模板

`src/wechat-article-agent/.env.example` 面向当前开发容器/tmux，不依赖 Compose DNS：

```text
APP_ENV=development
APP_HOST=127.0.0.1
APP_PORT=8240
DEBUG_ENABLED=true
APP_MAX_ACTIVE_RUNS=8
APP_MAX_QUEUED_RUNS=24
DB_POOL_MAX=10
CANCEL_CONFIRM_TIMEOUT_SECONDS=10
CANCEL_RECONCILE_INTERVAL_SECONDS=15
AGENT_SERVER_URL=http://127.0.0.1:8242
RETRIEVAL_BASE_URL=http://127.0.0.1:8220
ARK_RESPONSES_URL=https://ark.cn-beijing.volces.com/api/v3/responses
ARK_MODEL_FAST=doubao-seed-2-1-pro-260628
ARK_MODEL_MAIN=doubao-seed-2-1-pro-260628
ARK_MAX_CONCURRENCY=8
SEEDREAM_RESPONSES_URL=https://ark.cn-beijing.volces.com/api/v3/images/generations
SEEDREAM_MODEL=doubao-seedream-5-0-260128
SEEDREAM_MAX_CONCURRENCY=4
DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:5432/wechat_article_agent
ARK_API_KEY=
```

本地模板同样列全所有正式运行变量，并额外包含 `DEV_*`；本地容量较小只是默认值，不改变代码
硬上限。项目 `.env` 仍可软链接到 `../../env/wechat-article.env`：在本地开发机上，该实际文件应以
子项目本地模板重新生成，再安全注入旧文件中提取的 `ARK_API_KEY`。生产部署时，运维使用根生产
模板生成服务器上的实际 env，不能直接复用开发机的本地值。

### 21.4 两份模板验收

实现完成后增加自动化配置契约测试：

- 两份模板的正式变量集合与 `AppSettings` 字段完全一致；只有子项目模板允许多出与
  `DevPanelSettings` 完全一致的 `DEV_*`；正式服务不读取 `DEV_*`；
- 两份模板都不得包含 Redis、idempotency、event replay、delivery buffer 或已经删除的旧变量；
- 根模板端口为 8140，runtime/retrieval 使用 Compose DNS，DSN/内部 URL 均非 localhost；
- 根模板 `APP_MAX_ACTIVE_RUNS>=30`、`APP_MAX_QUEUED_RUNS>=90`、`ARK_MAX_CONCURRENCY>=30`、
  `DB_POOL_MAX>=30`；
- 子项目模板端口为 8240，runtime/retrieval 分别是 `127.0.0.1:8242/8220`，不得使用 Compose DNS；
- 两份模板的模型、Responses URL、TTL、重试、熔断、thinking 和图片变量均能通过 Settings 加载；
- 模板不包含真实 key，实际 env 被 Git 忽略且 key 不会被测试或日志打印；
- 根 `scripts/deploy.sh` 的必填项校验与根模板字段一致，本地启动脚本与子项目模板字段一致。

## 22. 代码结构建议

```text
src/wechat-article-agent/
  app/
    api/
      routes.py
      schemas.py
      sse.py
    adapter/
      admission.py
      history.py
      responses.py
      event_normalizer.py
      runtime_client.py
    graph/
      builder.py
      state.py
      context.py
      routing.py
      nodes/
        intent_router.py
        session_memory.py
        orchestrator.py
        docs_research.py
        task_spec.py
        outline.py
        article.py
        ai_image.py
        html_layout.py
      hitl/
        clarification.py
        review.py
        schemas.py
    artifacts/
      models.py
      repository.py
      service.py
    integrations/
      retrieval.py
      seedream.py
    llm/
      ark_responses.py
      gateway.py
      schemas.py
    prompts/
      intent_router.py
      preflight.py
      session_memory.py
      orchestrator.py
      docs_research.py
      task_spec.py
      outline.py
      article.py
      ai_image.py
    persistence/
      pool.py
      repositories.py
      migrations/
      cleanup.py
      cancellation_reconciler.py
    rendering/
      markdown.py
      themes.py
    debug/
      collector.py
      schemas.py
      sanitizer.py
    config.py
    main.py
  dev/
    config.py
    config.yaml
    dataset.py
    server.py
    static/
      index.html
      styles.css
      app.js
    tests/
  tests/
    unit/
    integration/
    contract/
    e2e/
  docs/
    implement.md
    smoke-test-YYYYMMDD.md
  pyproject.toml
  .env.example
  Dockerfile
```

正式 Docker image 只复制 `app/`，不复制或启动 `dev/`。`dev/` 使用同一个项目虚拟环境单独运行，
从而不会把本地调试服务混进产品进程。

## 23. 实现计划

### 阶段 1：项目骨架与数据库

- 建立 FastAPI/配置/日志/请求 ID/健康检查。
- 从零建立 Settings、根生产模板和子项目本地模板；仅从遗留 env 安全提取 secret，不继承其变量
  设计，并先写第 21.4 节模板契约测试。
- 固定 LangGraph/Agent Server/checkpointer 版本，先做最小 contract spike，验证 create/stream、
  interrupt resume、failed retry、running cancel、pending cancel、thread TTL/delete 和同 thread reject；
  将实际 endpoint/状态封装进 `GraphRuntimeClient`，协议不满足时在业务图开发前解决。
- 为本服务准备独立 PostgreSQL database 和 `DATABASE_URL`，不使用现有 pageindex database。
- 建立唯一业务表 `article_artifacts` 的 migration、repository 和三天 cleanup job。
- 接入 PostgreSQL checkpointer 并验证 session/thread 派生。
- 实现聚合 artifact revision 的复制、字段失效和阶段字段更新。
- 实现 `current_response_id/active_runtime_run_id/approved_through_stage` 控制字段、
  cancelling/cancelled 条件更新和取消后的新 revision 初始化。
- 实现 cancellation reconciler，保证客户端收到 202 后即使不再请求也能最终收敛取消状态。

验收：Agent Server contract spike 的 create/resume/retry/cancel/TTL 能力都有自动化证据；两份 env
模板通过第 21.4 节测试；能够创建两个聚合 revision，按入口阶段和 `approved_through_stage` 正确
复制/清空字段；checkpoint 只保存小状态和 artifact ID；cancelling 可由 reconciler 收敛；
session/thread 能在三天后清理 checkpoint 和 artifact。

### 阶段 2：Responses-like adapter

- 实现统一 `POST /v1/responses` 的 create/revise/HITL resume/pending preflight 分支。
- 实现完整历史的确定性预处理。
- 实现 `previous_response_id + interrupt_id + checkpoint` 的 stale interrupt 检查。
- 实现 advisory lock 和同 thread 并发运行拒绝。
- 实现三个 `agent.*` 事件 schema；事件只实时发送，不持久化。
- 实现 GraphRuntimeClient 和 AgentEventNormalizer 接口，先接 fake runtime。
- 实现正式 cancel endpoint、running/waiting 两条取消路径、幂等/竞态校验和
  `response.completed(status=cancelled)` 映射；用 fake runtime 固定外部语义。

验收：显式审批仍使用 `POST /v1/responses`；同一审批重复点击只有一次能恢复；断线后可以重新
展示当前 artifact/HITL，但不会重放中间 delta/activity；running/waiting cancel 均可停止并幂等，
cancel/approve/complete 竞态只有一个终态；PPT 可复用事件模型而不依赖微信字段。

### 阶段 3：Ark 模型网关

- 实现 `ArkResponsesChatModel`。
- 建立 `app/prompts/`，为每个 LLM phase 定义显式、分区、可审查的 system prompt。
- 映射文本、reasoning、function call、usage 和错误。
- 支持 strict structured output/tool calls、动态 ID schema 和公开解释字段。
- 参考检索服务实现全局公平队列、每 run 在途上限、异常流量 token bucket 和三态熔断。
- 实现分层超时、只在首个 delta 前进行的有限重试，以及结构化输出独立修复上限。
- 在 Gateway 用 phase allowlist 强制仅 task spec/outline/article 三个生成调用可配置 thinking。
- 探测并按配置接入 Ark `context_management` 和 caching；能力不支持时回退普通无状态请求。
- 把 request-scoped cancellation token 传播到公平队列和 provider SSE；cancel 时移除排队调用并关闭
  在途 HTTP 流，且 cancelled 不计入熔断失败。
- 使用真实方舟 `/api/v3/responses` 建立流式 contract tests。

验收：同一模型响应可以先输出文本，再产生工具调用；普通文本和 reasoning delta 顺序正确且不
混流；LangGraph 可以观察模型和工具生命周期；已输出 delta 后断流不会透明重试；其他 LLM
phase 即使误传参数也不能开启 thinking；缓存优化关闭或不受支持时不影响业务调用；cancel 后
排队调用被移除、在途流被关闭，不产生自动重试或新的 usage，迟到输出不进入下游。

### 阶段 4：HITL 竖切

- 实现最小图：fake task spec -> persist -> review interrupt -> resume -> fake outline。
- 实现 approve/regenerate/revise/supersede。
- 实现 pending preflight 的 replay/current feedback/upstream change/ambiguous 四类行为。
- 验证 interrupt 节点重放不会重复生成候选或产生外部副作用。
- 验证运行中 cancel 终止 fake LLM/tool，waiting cancel 只经内部控制路由到 cancelled，cancel 后
  “继续”不复活旧图、真实修改创建新 revision。

验收：页面断开、worker 重启、多标签重复提交、普通输入“继续”和上游变更都得到确定结果。

### 阶段 5：文档研读

- 实现 retrieval client 的 meta/route/raw/status/retrieve 全契约。
- 实现一次尽量问全的 sufficiency + clarification interrupt。
- 实现文档路由、素材级过滤、短/长文档提取和文档级并行。
- 实现 raw 失败后单文档“详细、全面总结” `/retrieve` 降级及 partial coverage 传播。
- 持久化 material library，并传播 coverage/warnings。
- 将 `document_coverage_mode` 从用户需求稳定写入 artifact，并在每个文档工具调用间检查取消状态。

验收：多主题文档会先澄清；raw 202 可恢复轮询；素材删除后重新进入研读可以再次提取；retrieve
部分降级不会被误记为完整证据；best-effort 部分失败可以继续，而 all-required 任一指定文档无
有效素材必须失败。

### 阶段 6：主业务链路

- 实现 intent router。
- 实现完全并行的 session memory 和 orchestrator，并在 join 后按 target 派发。
- 实现 task spec、Markdown outline 和三个 review 流程。
- 实现 V1 article 有界检索循环、context 预算和整篇 Markdown 生成。
- 实现上游变更后的聚合 revision 复制和下游字段失效。

验收：首次生成按顺序执行；“第三章增加小节”从 outline 进入；“语言口语化”从 article 进入；
后续均按单向状态机运行。

### 阶段 7：图片和 HTML

- 实现 Markdown AST、图片位置选择和 Seedream client。
- 实现只保存 `url/caption/insertion_position` 的 `images` 字段，以及部分/全部图片失败降级。
- 实现固定主题 Markdown -> HTML 和内联图片样式。
- 更新聚合 artifact 的 `final_html` 字段并输出最终 Response。
- Seedream 队列和在途请求响应 cancellation token；取消确认后的迟到 URL 不写 artifact。

验收：最终 HTML 可直接展示；超大图片按容器缩放；正文修改后新建 revision 并更新其图片/HTML
字段；HTML 转换不依赖 LLM；全部图片失败交付带 warning 的无图 HTML；主 renderer 失败使用最小
安全主题，二次失败保留 Markdown/图片并明确失败。

### 阶段 8：开发面板与可靠性

- 实现 `DebugTraceCollector` 双开关、脱敏、大小上限和终态一次性返回。
- 实现独立 FastAPI dev backend、manifest 随机文档预览和 Responses SSE 代理。
- 实现三栏前端、Codex 式文本/activity、HITL 交互、HTML sandbox 预览和 trace 面板。
- 实现“停止接收”和“取消 run”两个独立控制及 cancelling/cancelled UI。
- 覆盖第 19.6 节所有重试/降级分支及失败后明确“继续/重试”。
- 覆盖 checkpoint 状态恢复、同 thread 并发和 TTL 测试。

验收：`DEBUG_ENABLED=false` 时不创建 collector、不出现 debug 字段且性能路径无额外序列化；开启
后每个 node、LLM、tool 和 fallback 可在右栏查看。开发面板能完成创建、逐次审批、最终 HTML、
后续 revise、停止接收、取消 run，以及二者之后不同的“继续”语义。

### 阶段 9：真实烟测和发布文件

- 与前端/PPT Agent 对齐公共 JSON Schema 和 TypeScript 类型。
- 使用真实 PostgreSQL、检索服务、Ark 语言模型和 Seedream 完成第 25.2 至 25.4 节烟测，并按
  第 25.5 节生成报告。
- 增加节点耗时、模型 usage、检索 warning、HITL 等待时长和失败率指标。
- 在全部功能与烟测通过后添加独立 Dockerfile、根 Compose/env、`scripts/deploy.sh` 和健康检查。
- 在开发容器中只做 Dockerfile/Compose/shell/env 静态检查，不宣称已完成生产部署测试。

验收：至少完成一次“创建 -> 澄清 -> 任务书修改 -> 大纲批准 -> 文章修改 -> 图片 -> HTML”的
真实端到端流程、一次完成后 revise 流程，以及在每个 HITL 点断线恢复的自动化测试；生成烟测
报告，开发面板能显示对应 trace，所有部署文件通过第 26 节静态检查。

## 24. 测试矩阵

### 单元测试

- 历史预处理只保留必要审批信息。
- thread ID 派生稳定。
- orchestrator target 与单向 edge 映射。
- session memory 输出允许自然语言和空字段。
- session memory 固定包含 `ai_image/html_layout` 两个空字符串预留字段。
- HITL form/decision schema。
- material 过滤及 `relevant-remain` 再提取语义。
- context budget 和检索轮数上限。
- artifact revision 单调递增、父 revision 和阶段字段失效。
- `document_coverage_mode` 在文档提取前确定，后续 task spec 不能篡改；新 revision 按入口阶段正确
  继承或重算该值。
- cancelled/superseded revision 只按 `approved_through_stage` 继承已批准内容，拒绝继承未批准候选。
- cancelled 空壳 revision 之后再次修改时，能沿父链找到最近批准祖先，同时新 revision 仍保留
  指向直接父 revision 的审计关系。
- Markdown heading AST 与图片插入。
- 图片 artifact 只允许 `url/caption/insertion_position`，HTML 不下载或代理 URL。
- 每个 prompt 具有规定分区，模板变量完整，公开解释字段与 adapter 映射一致。
- dynamic ID schema 只接受当前候选 ID，拒绝缺失、重复和未知 ID。
- 三个自定义事件的 schema compatibility。
- debug 双开关短路、credential 脱敏、截断优先级和关闭时无 `debug` 字段。
- 两份 `.env.example` 与 Settings 集合一致、环境 URL/容量正确且没有 Redis/idempotency 等遗留变量。

### Contract 测试

- 方舟 Responses 文本/reasoning/tool call SSE。
- 方舟 strict dynamic schema、公开解释字段和 Markdown artifact submit schema。
- 方舟 thinking phase allowlist、首 delta 前重试、流中断不重试、分层 timeout 和三态熔断。
- 方舟 `context_management`/caching capability probe、启用路径和不支持回退路径。
- Agent Server Protocol v2 stream 和 Command resume。
- retrieval meta/route/raw 200/raw 202/status/retrieve warning。
- Seedream 成功、限流、超时和部分失败。
- Agent Server failed run 的 retry/rerun API、checkpoint 游标和同 revision 恢复语义。
- Agent Server running run cancel、pending interrupt cancel、终态竞态和重复 cancel。
- Ark/retrieval/Seedream cancellation token 能移除排队任务、关闭 mock 在途流，且 cancelled 不触发重试
  或熔断计数。

### 集成测试

- PostgreSQL checkpointer + 业务 repository。
- interrupt 前后 checkpoint state。
- 同一 session 并发新建请求/审批恢复请求。
- 同一 session 的 create/resume/cancel 并发互斥，cancelled 后“继续”不恢复、修改创建新 revision。
- cancellation reconciler 在无客户端再次请求时把 202/cancelling 收敛到 cancelled，并处理补偿
  cancel 与 runtime 状态异常。
- supersede 后旧页面提交返回 409。
- TTL 清理不删除未过期 thread/artifact revision。
- debug 开启时终态一次性返回 node/LLM/tool/fallback trace；关闭时 runtime 不采集。
- dev backend 随机读取 JSONL doc IDs、调用 meta、代理 Responses SSE 和拒绝非法目标 URL。
- 使用 Playwright 驱动开发面板：生成随机文档、编辑并发送请求、观察文本增量/activity 原位更新、
  点击 clarification/review、查看终态 trace 和 sandbox HTML；桌面三栏与窄屏单栏均无重叠。
- 浏览器网络断言 `DEBUG_ENABLED=false` 时，即使请求传 `context.debug=true`，终态也无 `debug`；
  双开关开启时 trace 只在终态出现，不夹在文本 delta 中。

### 端到端场景

1. 多主题文档 + 模糊请求 -> 一次完整澄清。
2. task spec regenerate、outline revise、article approve。
3. 最终 HTML 后要求第三章新增小节 -> outline 起步。
4. 最终 HTML 后只要求口语化 -> article 起步。
5. SSE 在运行中断开，后台到 HITL，下一请求重新展示 artifact 和卡片，但不补发中间事件。
6. HITL 时输入“继续” -> replay；输入改变主题 -> supersede + 新 run。
7. raw 返回 202，worker 重启后继续状态轮询。
8. raw 最终失败 -> `/retrieve` 详细总结降级，trace 和 material 标记 fallback。
9. retrieve 返回 200 + incomplete coverage，artifact 保留降级元数据。
10. `best_effort` 模式下一篇文档最终提取失败，使用剩余证据完成并告警；同样故障在
    `all_required` 模式下必须失败且不得进入任务书生成。
11. 一个 Seedream 请求失败，其他图片和 HTML 正常生成；全部失败时交付无图 HTML。
12. HTML 主主题失败 -> 最小安全主题；二次失败时保留 Markdown/图片并失败。
13. 生成中由开发面板“停止接收”断开 SSE -> 后台继续；立即“继续”返回运行中，稍后重放 HITL
    或最终结果。
14. pending 时输入“继续” -> 相同 response/interrupt ID 原位 replay，不恢复 graph。
15. 临时失败后明确输入“继续/重试” -> 同 revision、新 response ID 和新有限预算重跑失败阶段。
16. 尝试次数耗尽后输入“继续/重试” -> 新 revision；需要修正输入的失败只重显明确错误。
17. 最终 HTML 后发送 revise -> 从 outline 或 article 正确进入并再次完整交付。
18. running 和 waiting_for_input 分别点击“取消 run” -> 进入 cancelled，模型/工具/产物不再推进。
19. cancelled 后任意普通输入创建新 revision 并由 orchestrator 路由，旧 run 和旧 interrupt 不复活。
20. cancel 与完成/审批并发只有一个终态生效，重复 cancel 幂等，旧 response cancel 返回 409。
21. session 三天无活动后 checkpoint 和全部 artifact revision 清理。

取消测试必须记录 cancel 接受时刻之后的 provider/tool 调用数、usage/token 和 artifact更新时间：
允许已经发往 provider 的在途调用返回少量尾部事件，但这些结果必须被丢弃；不得出现新的模型/
检索/Seedream 调用、自动重试、下游节点或 artifact 内容更新。否则 cancel 验收失败。

## 25. 真实烟测和验收报告

### 25.1 环境与密钥

真实烟测在实现和自动化测试通过后执行。固定使用：

```text
Ark Responses URL: https://ark.cn-beijing.volces.com/api/v3/responses
语言模型:          doubao-seed-2-1-pro-260628
图像模型:          doubao-seedream-5-0-260128
本地 Agent:        http://127.0.0.1:8240
本地 Dev panel:    http://127.0.0.1:8245
本地 Retrieval:    http://127.0.0.1:8220
```

语言和图像使用同一个 `ARK_API_KEY`。项目 `.env` 是指向 `../../env/wechat-article.env` 的软链接；
如果共享 env 尚无可用 key，只从本地已有检索或知识库问答 env 中选择性复制 `ARK_API_KEY` 这个
值，不能复制整份旧配置。复制过程不得开启 shell xtrace、打印值或把 key 放进命令历史；目标文件
权限设为 `0600`，且真实 env 必须被 Git 忽略。Key 不得出现在 debug trace、日志、fixture、截图、
HTML 或烟测报告中。

### 25.2 完整生成 smoke

使用 `/workspace/wechat-article-agent/wechat-article-documents.manifest.jsonl` 中 `status=completed` 的
真实 doc IDs，至少跑通一次完整请求。测试脚本或开发面板维护完整 input 历史；遇到 clarification
时提交合理答案，遇到 task spec、outline、article review 时由无人值守测试客户端依次发送真实
HITL `approve`。不能绕过 interrupt 或直接改 checkpoint。最终必须满足：

- 收到预期 Responses 标准事件和三个 `agent.*` 事件；
- 每个审批使用正确 `previous_response_id/interrupt_id`，没有 stale 恢复；
- artifact 依次持久化素材库、任务书、Markdown 大纲、Markdown 正文、图片 URL 和 HTML；
- 至少调用一次真实语言模型和一次真实 Seedream；
- HTML 能在开发面板 sandbox iframe 中显示，图片 `src` 使用保存的 provider URL；
- debug 右栏能查看每个 node、LLM、tool、fallback 和错误；debug 关闭后相同接口不返回 trace。

### 25.3 完成后 revise smoke

在上一完整 run 已完成后，使用相同 session 和完整前端历史发送真实修改，例如“第三章增加一个
风险提示小节”，验证 orchestrator 从 outline 进入，创建新 revision，继承素材库和 task spec，
清空并重新生成 outline 及其下游。测试客户端继续代替用户批准所有 HITL，最终得到新的 HTML。
另选一个仅修改语言风格的请求验证从 article 进入；至少有一个 revise 场景必须使用真实模型完整
跑通并写入报告。

### 25.4 恢复和边界 smoke

至少验证：

1. 在普通流式生成中点击“停止接收”；后台继续时立即发送“继续”得到运行中状态，稍后得到
   pending replay 或最终 artifact，而不是第二个 run。
2. 到达 HITL 后点击开发面板“停止接收”，再发送“继续”；卡片使用原
   `response_id/interrupt_id` 重新展示且图未恢复。
3. 对同一 interrupt 连续提交两次，第二次返回 409 `STALE_INTERRUPT`。
4. 注入一次可重试失败后明确发送“继续/重试”，验证同 revision、新 response ID 重跑失败阶段。
5. pending 时发送上游需求变化，验证 supersede 后创建新 revision。
6. 分别在模型流式生成中和 HITL 等待中点击“取消 run”，验证 cancel endpoint 返回
   cancelling/cancelled，取消确认后无后续 provider/tool 调用或 artifact 更新。
7. cancelled 后发送“继续”，验证不恢复旧 run；再发送真实修改，验证创建新 revision 并正常生成。
8. cancel 与自然完成、HITL approve 并发，验证条件更新和 runtime 终态不会产生双终态或旧回调覆盖。

报告必须分别使用“停止接收”和“取消 run”术语：前者只是 SSE abort，后者调用正式 cancel
endpoint。不得把网络断开描述成后台已取消，也不得只看到 UI 停止就宣称 provider 工作已停止。

### 25.5 报告产物

每次正式 smoke 在 `src/wechat-article-agent/docs/` 新建日期化报告，例如
`smoke-test-20260820.md`。报告至少包含：

- commit、运行时间、非敏感环境摘要、模型名和各服务 URL；
- 使用的脱敏 session/run/response/artifact/revision 标识；
- 文档数量和文件名，不复制文档全文；
- 各阶段耗时、模型 usage、HITL 决策、降级/warning 和最终状态；
- 完整生成、完成后 revise、停止接收后继续、正式 cancel、cancel 后继续/修改和 stale interrupt
  的结果；
- 有价值失败案例的错误码、阶段、复现步骤、根因与处理，不保存 key、完整 prompt 或文档全文；
- 最终 HTML 的文件路径、大小、截图或简短验收说明。

最终 HTML 另存到 `docs/smoke-artifacts/<run-id>.html` 并在报告中使用相对链接，不把数万字符 HTML
全文内嵌进 Markdown。烟测产生的图片仍只引用 URL，不下载到仓库。失败则报告不得写“通过”，应
保留脱敏证据和剩余问题，修复后重新跑并追加复测结果。

## 26. 部署设计

### 26.1 服务和端口

部署只在全部功能、测试、开发面板和真实烟测确认无误后进行。生产对外服务为 FastAPI adapter：

```text
wechat-article-agent   0.0.0.0:8140   对外 Responses-like create/resume、cancel 和 health endpoint
wechat-article-runtime internal:8141  私有 LangGraph Agent Server，不映射宿主机端口
```

两者是同一子项目的两个 Compose 服务/进程。adapter 通过
`AGENT_SERVER_URL=http://wechat-article-runtime:8141` 使用内部 Agent Server Protocol v2。不要在
一个容器里用 shell 同时守护两个长期进程。runtime 只加入私有 Compose network，不对公网暴露。

本地 tmux 使用 adapter `8240`、dev panel `8245`、runtime `8242`；检索服务是
`http://127.0.0.1:8220`。生产 adapter/runtime 访问检索统一使用
`http://rag-retrieval-service:8120`，不得在容器 env 中配置 localhost。

### 26.2 子项目文件

模仿检索和知识库问答子项目，最终增加：

- `src/wechat-article-agent/Dockerfile`：adapter 单 worker image，端口 8140，非 root 用户，healthcheck；
- runtime 所需独立 Dockerfile/官方构建配置：启动固定版本 Agent Server，端口 8141；
- `.dockerignore`、第 21.3 节定义的本地 `.env.example`、README 本地启动说明；
- `src/wechat-article-agent/.env -> ../../env/wechat-article.env` 软链接，仅供本地开发；
- 本地独立 compose 可选，但不能与根 Compose 的生产端口和网络语义冲突。

正式 adapter image 只复制运行所需的 `app/`、migration 和锁文件，不复制 `dev/`、真实 `.env`、
测试数据、烟测 HTML 或 API Key。进程保持一个 worker，因为公平调度、RPM bucket 和 circuit breaker
在 V1 是进程内状态；需要水平扩展时先设计共享限流。

### 26.3 根项目集成

在根项目新增第 21.2 节定义的完整 `env/wechat-article.env.example`。该文件是生产部署模板，不能
从遗留实际 env 反向生成，也不能省略容量、重试、熔断、TTL、thinking 或数据库配置。

根 `docker-compose.yaml` 增加 adapter 和私有 runtime，adapter 映射 `8140:8140`，依赖 runtime 和
健康的 retrieval；两者连接 `rag-platform-network`，需要访问现有数据库网络时再按实际 DSN 加入
对应私网。独立 PostgreSQL database 通过 `DATABASE_URL` 指向生产数据库，不与 pageindex database
混用。

根 `scripts/deploy.sh` 同步增加两个 Compose service、`env/wechat-article.env` 文件校验、
`DATABASE_URL/ARK_API_KEY` 非空校验、容器 DSN 禁止 localhost、build/start/health 等待和 endpoint
输出。服务计数、日志提示和 README 也同步更新；不得只改 Compose 而遗漏部署脚本数组。

本地 tmux 启动说明和脚本使用子项目 `.env.example` 的 localhost 语义；根 Compose/deploy 只使用
根生产模板的 DNS 语义。两份模板必须通过第 21.4 节自动化验收后，部署文件才算完成。

### 26.4 部署验收边界

开发容器不具备生产部署条件，因此本项目不要求在这里执行真实生产部署。只进行静态检查：

```text
bash -n scripts/deploy.sh
docker compose -f docker-compose.yaml config --quiet
Dockerfile lint（环境有 hadolint 时执行）
env template 与 Settings 字段一致性测试
根生产模板/子项目本地模板的 DNS、localhost、端口、容量和遗留变量反向检查
端口、healthcheck、depends_on、network 和非 root USER 检查
镜像上下文不包含 .env/dev/test dataset/smoke artifacts 的检查
```

静态检查通过不等于部署成功，烟测报告和最终交付必须明确写“生产部署未在开发容器验证”。真正
上线后仍需在生产环境执行 image build、Compose 启动、adapter/runtime/retrieval/DB 健康检查和
一次最小请求。

## 27. 设计依据

- 项目五服务关系：根目录 `README.md`。
- 入库数据结构：`src/repo-doc-ingestion/docs/data_structure.md`。
- 检索契约：`src/rag-retrieval-service/docs/api.md`。
- 检索目录导航和逐页筛选：
  `src/rag-retrieval-service/app/workflows/focused_search/`。
- LangGraph interrupt：<https://docs.langchain.com/oss/python/langgraph/interrupts>
- LangGraph persistence：<https://docs.langchain.com/oss/python/langgraph/persistence>
- LangGraph streaming：<https://docs.langchain.com/oss/python/langgraph/streaming>
- OpenAI Responses streaming 事件参考：
  <https://developers.openai.com/api/docs/guides/streaming-responses>

本文是 V1 实现基线。新增复杂能力优先作为后续版本扩展，不在 V1 中提前引入章节级正文状态、
复杂长期记忆、素材历史台账或新的 SSE 事件类别。
