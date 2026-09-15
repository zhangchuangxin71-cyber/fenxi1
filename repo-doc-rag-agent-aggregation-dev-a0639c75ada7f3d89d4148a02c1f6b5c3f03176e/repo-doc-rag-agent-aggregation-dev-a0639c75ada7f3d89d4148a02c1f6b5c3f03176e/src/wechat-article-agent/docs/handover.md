# 微信公众号文章 Agent 项目交接文档

> 交接日期：2026-08-21  
> 项目目录：`repo-doc-rag-agent-aggregation/src/wechat-article-agent`  
> 说明：本文用于帮助后续维护者快速了解当前系统、代码入口、运行方式和未完成事项。接口的精确字段仍以
> `api.md`、`frontend-integration.md` 和实际代码为准。

## 1. 项目是做什么的

这是一个基于 LangGraph 的微信公众号文章生成服务。用户可以提供参考文档，也可以不提供文档；系统经过素材调研、
任务书、大纲、Markdown 正文、配图和 HTML 排版，最终交付 `final_html`。

工作流中的关键产物会经过 HITL：

- 素材调研开始前确认 `target + about`；
- 任务书、Markdown 大纲和 Markdown 正文支持接受、修改后重做、完全重新生成；
- 用户完成一次生成后可以继续提出修改，系统会创建新的 revision，由 orchestrator 判断从哪个阶段重新开始；
- 网络断开不会自动取消后台 run，待审批内容可以重放；正式 cancel 才会终止本次运行。

当前主链路可以简化为：

```text
POST /v1/responses
  -> adapter 准入、pending/replay/resume/cancel 状态判断
  -> intent_router
  -> session_memory || orchestrator
  -> 素材调研
       -> 文档素材提取 || Ark 联网素材搜索
       -> 规范化、压缩、冲突检查
       -> 必要时冲突 HITL
  -> 任务书 HITL
  -> 大纲 HITL
  -> Markdown 正文 HITL
  -> Seedream 配图
  -> HTML 排版
  -> final_html
```

## 2. 服务和协议边界

项目本地由三个进程组成：

| 进程 | 本地地址 | 生产端口 | 用途 |
|---|---|---|---|
| Responses-like adapter | `127.0.0.1:8240` | `8140` | 唯一公开的产品 API |
| LangGraph Agent Server | `127.0.0.1:8242` | 容器私网 `8141` | thread、run、checkpoint、interrupt |
| 开发面板 | `127.0.0.1:8245` | 不部署 | 本地人工调试和链路观察 |

adapter 和 Agent Server 是两个进程，但属于同一个应用。生产环境只应公开 adapter；内部 Agent Server 通过容器
网络供 adapter 调用，不应直接暴露给前端或公网。

公开接口主要是：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/responses` | 首次生成、后续修改、“继续/重试”和 HITL resume |
| `POST` | `/v1/responses/{response_id}/cancel` | 正式取消后台 run |
| `GET` | `/health/live` | 存活检查 |
| `GET` | `/health/ready` | 数据库等依赖就绪检查 |

对外协议采用 Responses-like SSE。标准文本和 reasoning 事件之外，增加了三种公共事件：

- `agent.activity`：节点、工具和 Skill 状态；
- `agent.artifact`：任务书、大纲、正文、图片和 HTML 等产物；
- `agent.interrupt`：HITL 卡片及其表单。

HITL 没有单独的 resume 接口。前端把卡片对应的 `previous_response_id`、`interrupt_id`、decision 和 feedback
再次提交到 `/v1/responses`。不要把 Agent Server 的 command 接口暴露给前端。

### 2.1 统一 Responses-like 协议

`docs/responses-like-protocol.md` 是本项目与其他长任务 Agent 共用的标准协议文件，也是修改 adapter、SSE 事件或
前端 reducer 时应优先查阅的规范。该协议借用了 OpenAI Responses API 的请求形态、生命周期事件以及文本和
reasoning 增量事件，但加入了项目上下文、HITL 和 `agent.*` 事件，因此只能称为 **Responses-like 扩展协议**，
不能宣称与 OpenAI 官方 Responses API 完全兼容。

协议兼容要求分为两层：

| 层级 | 要求 |
|---|---|
| 事件层 | 强约束。多个 Agent 必须对齐 `agent.activity`、`agent.artifact`、`agent.interrupt` 的事件名、公共字段、枚举含义、ID 和 upsert 规则 |
| HTTP 接口层 | 推荐 profile。其他 Agent 可以实现接口子集或增加查询、下载、历史等接口，但使用同名接口时应保持创建、恢复和取消语义一致 |

三种公共事件的职责如下：

| 事件 | 用途 | 前端更新键 |
|---|---|---|
| `agent.activity` | 节点、工具、Skill、降级和失败状态 | `activity.id` |
| `agent.artifact` | 阶段产物和最终交付物 | `artifact.id + artifact.stage` |
| `agent.interrupt` | 需要用户参与的 artifact 审批或 clarification 表单 | `interrupt.id` |

不要为某个业务节点新增 `node.xxx`、`wechat.xxx` 一类公共 SSE 事件。节点名、artifact stage 和表单字段都应放进
上述三种事件的 payload。工具使用 `agent.activity(kind=tool)`，Skill 使用 `agent.activity(kind=skill)`；完整工具参数、原始模型输入输出等内容
只属于 debug trace，不能进入产品事件。

标准风格事件主要包括：

```text
response.created
response.output_item.added
response.content_part.added
response.output_text.delta
response.output_text.done
response.reasoning_summary_part.added
response.reasoning_summary_text.delta
response.reasoning_summary_text.done
response.completed
response.failed
```

文本和 reasoning 必须以 delta 形式转发，前端按稳定 item ID 累加。`response.completed` 只表示当前 SSE Response
片段结束，不一定表示整个 workflow 完成；到达 HITL 时也会以 `response.completed` 收尾，此时应通过
`response.metadata.workflow_status=waiting_for_input` 和 `pending_interrupt_id` 判断仍在等待用户。

各类 ID 的关系如下：

```text
session_id
  -> 一个内部 LangGraph thread
  -> run_id（一次完整业务工作流，可跨多次 HITL）
       -> response_id 1（运行到第一个 interrupt）
       -> response_id 2（审批恢复后运行到下一个 interrupt）
       -> response_id N（运行到 workflow 终态）
       -> artifact_id + revision（本轮业务产物版本）
       -> interrupt_id（当前待处理交互，处理后即失效）
```

`session_id` 由前端长期维护；`run_id` 和 `response_id` 由服务端生成；审批时前端只需保存卡片对应的
`response_id` 和 `interrupt_id`，并分别作为 `previous_response_id` 与 `context.hitl.interrupt_id` 回传。
`previous_response_id` 不等于 `interrupt_id`。adapter 必须同时校验 session/thread、Response、interrupt、artifact
和 revision，旧审批应返回 409 `STALE_INTERRUPT`，不能尝试恢复当前新版本。

推荐的 HITL 恢复仍走主接口：

```json
{
  "model": "example-long-task-agent",
  "stream": true,
  "previous_response_id": "resp_previous",
  "input": [{"role": "user", "content": "请调整当前候选结果"}],
  "context": {
    "session_id": "client-session-1",
    "hitl": {
      "interrupt_id": "int_current",
      "decision": "revise",
      "feedback": "请调整第二部分的结构"
    }
  }
}
```

`decision` 只有 `approve/revise/regenerate`。clarification 的结构化选择也通过 `decision=revise` 和
`context.hitl.selection` 提交。审批被接受后会产生新的 `response_id`，adapter 再转换为内部 LangGraph
`Command(resume=...)`；前端不直接调用内部 command。

SSE 断线和 cancel 必须严格区分：

- 关闭连接或“停止接收”只停止前端收流，后台 run 继续；
- 正式停止使用 `POST /v1/responses/{response_id}/cancel`；
- pending 时输入“继续”只重放原 artifact 和 interrupt，不 resume、不重新生成；
- 已 cancel 的 checkpoint 不能通过“继续”复活，后续真实请求创建新 revision 并重新路由；
- V1 不持久化 token 事件，因此不承诺补发断线期间的文本、reasoning 或 activity。

前端 reducer 必须容忍重放和重复事件：Response 使用 get-or-create，activity、artifact 和 interrupt 按上述稳定 ID
原位 upsert；重放同一 `response_id` 时不能清空已经接收的文本，也不能创建第二张审批卡。遇到未知事件或未知可选
字段应忽略，不能让整个流解析失败。协议公共字段发生破坏性修改时必须提升 `schema_version`，并同步维护：

- `docs/responses-like-protocol.md`：跨 Agent 标准协议；
- `docs/api.md`：微信公众号 Agent 的具体 HTTP 契约；
- `docs/frontend-integration.md`：前端消费、状态管理和卡片渲染；
- `docs/changes_for_frontend.md`：需要前端关注的增量变化。

## 3. 数据由谁保存

系统的数据分为三层：

| 数据 | 维护方 | 说明 |
|---|---|---|
| 完整对话历史 | 前端调用方 | 前端按 `session_id` 保存，并在每次请求中传入完整历史 |
| LangGraph checkpoint | PostgreSQL checkpointer | 小型 Graph State、执行游标和 pending interrupt |
| 业务 artifact | `article_artifacts` | 素材、任务书、大纲、正文、图片信息和最终 HTML |

后端不重复保存完整聊天消息。adapter 会从前端历史中剔除旧 artifact 全文和工具结果，整理节点需要的历史审批偏好。

业务 artifact 按 revision 保存聚合快照：

- 新业务修改创建新 revision，并通过 `parent_artifact_id` 指向上一版；
- 新 revision 复制仍然有效的上游字段，从 orchestrator 选定的入口开始清空下游字段；
- 同一 revision 内，尚未批准的 regenerate/revise 可以覆盖候选；
- checkpoint 只保存 artifact ID、revision、当前阶段和小型状态，不保存 Markdown/HTML 全文；
- checkpoint 和 artifact 使用三天滑动 TTL。

图片 artifact 只保存：

```text
url + caption + insertion_position
```

图片二进制和 OSS 转存由前端处理。

## 4. 主要代码入口

| 目录或文件 | 作用 |
|---|---|
| `app/main.py` | adapter/FastAPI 应用入口 |
| `app/adapter/` | 请求准入、Responses-like 转换、pending/replay/resume/cancel |
| `app/graph/builder.py` | LangGraph 节点、edge 和主要业务流程 |
| `app/graph/state.py` | 小型 Graph State |
| `app/artifacts/` | artifact model、repository、revision 继承和 TTL |
| `app/persistence/` | PostgreSQL checkpointer 和 migration |
| `app/llm/` | Ark Responses 模型、strict schema、输入清洗和网关 |
| `app/prompts/system.py` | 各 LLM 节点的系统提示词 |
| `app/materials/` | 文档/网络素材模型、规范化、压缩、冲突处理 |
| `app/integrations/` | 检索服务、Seedream 等外部服务 |
| `app/rendering/wechat_layout/` | deterministic/llm_decide 使用的多主题确定性排版器和 validator |
| `app/rendering/skill_driven/` | 基于迁移 GZH/Xiaowan 资产的独立 Skill 排版装配器、validator 和适配层 |
| `app/agent_engine/` | 通用受限 ReAct Engine |
| `app/skills/rendering/` | 已改造并固定版本的排版 Skill 资产 |
| `app/events/` | 内部事件和 Responses-like SSE 规范化 |
| `app/debug/` | 仅 debug 开启时使用的 trace collector |
| `dev/` | FastAPI 开发面板后端和静态前端 |

LangGraph 主图目前集中在 `app/graph/builder.py`。文件较大，修改前建议先结合 `web_search_design.md` 和
`implement.md` 查看节点职责，不要只根据函数名调整 edge。

## 5. 本地运行与验证

项目 `.env` 通常链接到大仓库的 `env/wechat-article.env`。本地默认端口和生产配置不同，配置说明见
`environment-variables.md`。

```bash
cd /workspace/repo-doc-rag-agent-aggregation/src/wechat-article-agent
uv sync

# 内部 LangGraph runtime
uv run langgraph dev --host 127.0.0.1 --port 8242 --no-browser --no-reload

# 对外 adapter
uv run uvicorn app.main:app --host 127.0.0.1 --port 8240

# 本地开发面板
uv run uvicorn dev.server:app --host 127.0.0.1 --port 8245
```

开发面板默认值在 `dev/config.yaml`，可以把 Agent 地址改为公网地址测试已部署环境。正式服务关闭 debug 后，
即使请求体传 `debug=true`，也不应创建完整 trace 或返回内部 artifact。

常用回归命令：

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app dev tests
uv run pytest -q
uv run python tests/rendering_data/render_all.py
```

真实 Ark/完整流程测试脚本在 `tests/manual/`。运行真实测试前检查本地 `.env`，不要把 API key 写入代码、报告或
Git。最近一次 Agent Engine 验收结果为 `204 passed, 1 skipped`，Skill 排版固定数据和 8 套确定性主题全部通过；详情见
`skill-rendering-smoke-report.md`。

## 6. 当前已经完成的能力

### 6.1 素材调研和联网搜索

原“文档研读”已经升级为“素材调研”。用户确认 `target + about` 后，文档素材和网络素材并行处理；没有文档时也
可以使用网络素材或模型通识继续生成。

当前网络搜索依赖 Ark Responses 内置 `web_search`：

- `web_info_overview` 用于理解陌生主题和时效背景，不进入素材库；
- `web_material_worker` 一次调用中处理 factual、resource、creative reference、popular culture 四类素材；
- 网络事实和用户文档统一进入规范化、压缩和冲突检查；
- 会影响文章结论的事实冲突通过 HITL 处理；
- `material_sources` 对外只展示文档名和网络 URL；
- `resource` 已在 schema 和数据库中预留，但当前不会被正文或图片节点消费。

需要注意：Ark annotation 是有限搜索摘要，不等于完整网页正文，也不能保证搜索到登录墙或强反爬网站。

### 6.2 HITL、revision 和恢复

- 意图识别对不明确的公众号生成意图最多进行一次选项确认；
- 素材调研方向使用推荐选项或自由输入；
- 任务书、大纲、正文支持 approve/revise/regenerate；
- 完成后的修改创建新 revision，由 orchestrator 选择最早需要重做的阶段；
- 断开 SSE 不等于 cancel；
- pending 状态下“继续”重放同一审批卡，不恢复执行；
- 正式 cancel 后旧 interrupt 失效，下一次请求创建新 revision 并重新路由；
- stale interrupt 返回 409，前端不得自动重复提交旧审批。

### 6.3 排版和 Agent Engine

当前排版支持 8 套确定性主题，并提供三种运行模式：

| 模式 | 行为 |
|---|---|
| `deterministic` | 使用 `HTML_LAYOUT_DEFAULT_THEME` 直接代码排版 |
| `llm_decide` | LLM 用 strict schema 选择已注册主题，再代码排版 |
| `skill_driven` | 通用 ReAct Engine 读取迁移的 GZH/Xiaowan Skill 资产，调用独立组件装配器和 validator 生成富组件 HTML |

生产和本地 `.env.example` 当前默认是 `skill_driven`。该模式已通过真实 Ark 和开发面板烟测；仍应持续观察
耗时、成本、fallback 比例和不同文章类型的质量。

Agent Engine 已支持：

- 真正的 `model -> tool -> result -> model` 循环；
- 节点、Skill 和 tool 白名单；
- strict 工具输入输出；
- 最大步骤、工具次数、单工具次数、调用/总超时；
- 相同语义工具调用反复失败检测；
- 上下文预检、确定性压缩和有界内存对象表；
- 取消传播和结束清理；
- Skill/tool 产品活动以及 debug trace。

第一阶段只有 `render_html` 接入了 Engine。任务书、大纲和文章仍使用原有节点实现。

## 7. 未完成事项和建议改造方向

### 7.1 专门图片搜索 API

这是优先级较高的后续工作。

目前观察到 Seedream lite 可以使用联网信息增强部分时效性场景，而 Seedream pro 没有同等能力；涉及演员、公众人物等
高版权敏感内容时，Seedream 仍可能拒绝生成。例如“生成关于演员沈腾的介绍”可能在配图阶段失败。该行为取决于模型
平台后续策略，接手后应重新做一次真实探测，不要把当前观察当作长期固定能力。

更稳定的方案是接入专门的图片搜索 API，而不是不断重试被内容安全拒绝的生图请求。建议把当前
`web_material_worker` 拆为：

```text
web_search_queries_planner
  -> strict 输出四类 queries
       factual
       resource/image
       creative_reference
       popular_culture
  -> 按 query 并行调用独立 web/image search tools
  -> 必要时抓取公开页面或图片 metadata
  -> URL 安全检查、过滤、清洗、去重和来源归一化
  -> LLM 基于搜索结果生成各类 summary
  -> 适配为当前 web_material_worker 的 Material[] 输出契约
  -> 接回 material_normalize 之后的原工作流
```

保持最终 `Material` 契约不变很重要，这样数据库、素材冲突、任务书、大纲和文章节点不需要同步重写。建议优先修改：

- `app/graph/builder.py` 中 `web_material_worker` 附近的节点和 edge；
- `app/llm/schemas.py` 中网络素材输出 schema；
- `app/llm/inputs.py` 和 `app/prompts/system.py` 中搜索规划输入/提示词；
- `app/materials/models.py`、`processing.py` 中 resource 规范化；
- `app/integrations/` 中新增搜索 client；
- `app/events/` 中保持现有 tool activity，不新增不必要的协议事件。

如需爬取，应限制域名、响应大小、Content-Type、重定向次数和超时，防止 SSRF、超大文件和恶意页面。不要在 LLM 工具
里直接开放任意 URL 下载或浏览器执行。

### 7.2 使用已经保存的网络图片资源

当前 `resource` 类型网络素材已经可以保存，但不会进入正文生成或配图流程。后续可以在 `generate_images` 前增加一个
“候选网络图片选择”步骤：

1. 从当前 revision 的 material library 读取 resource；
2. 校验 URL、来源、可访问性和基本图片 metadata；
3. 使用多模态模型判断图片是否与文章内容匹配；
4. 为选中的图片生成 caption；
5. 根据 Markdown 标题路径和段落规划 `insertion_position`；
6. 转换为现有图片 artifact 的 `url + caption + insertion_position`；
7. 复用后续确定性排版流程。

这里不需要修改最终图片 artifact 契约。需要额外确认图片版权、来源展示、热链有效期以及前端转存 OSS 的责任，不能仅以
“可以访问”判断为“可以发布”。

### 7.3 将其它节点接入 Skill/Agent Engine

后续任务书、大纲和 Markdown 正文节点可以复用 `app/agent_engine`，但不要直接复制排版节点代码，也不要把第三方
Skill 当作运行时黑盒。

建议每个节点分别完成：

- 调研候选 Skill，拆分各自职责；
- 处理 Skill 之间的冲突、重复规则和优先级；
- 将可用内容迁移到 `app/skills/<capability>/`，固定来源 commit/hash；
- 把稳定脚本改造成 strict、受限、进程内工具；
- 注册该节点自己的 `NodeAgentProfile`、Skill/tool allowlist 和预算；
- 定义完成 schema、事实不变量和 completion validator；
- 保持外层 Graph、HITL、artifact 和 checkpoint 契约不变；
- 为失败、超时和预算耗尽设计回到原节点实现的降级路径。

正文节点可以优先考虑补充检索、事实检查、结构检查和写作风格 Skill。素材足够时应允许模型直接完成，不能为了展示
Agent 能力而强制调用工具。

### 7.4 继续审查 Agent Engine 和 Skill 迁移质量

Agent Engine 和首批 Skill 迁移集中在较短时间内完成，比较仓促，虽然已有单元、契约、并发、真实 Ark 和前端回放测试，但仍有
优化空间。建议后续重点检查：

- `AgentRunner` 是否仍保持业务无关，排版专属逻辑只能放在 adapter/profile；
- token 估算在中文、长 JSON 和长工具结果上的保守程度；
- 压缩是否始终保留未解决错误、用户修正和事实约束；
- 临时错误的 retry 分类是否覆盖真实 provider 错误码；
- 连续失败 fingerprint 是否会误判或被文案字段绕过；
- cancel 时模型流、工具任务和内存对象是否全部释放；
- debug 关闭后是否完全不构造大体积 prompt/tool result；
- Skill L0/L1/L2 是否真正做到按需披露，而不是每次读取全部规则；
- `gzh-design`、`xiaowan` 和交叉参考资产是否还遗漏有价值的 validator/policy；
- 生产并发约 30 时的模型排队、总耗时、内存和 fallback 比例。

设计、测试矩阵和当前边界见 `agent_engine.md`，最新真实排版结果见 `skill-rendering-smoke-report.md`。

### 7.5 模型平台可替换性

目前 LLM 网关围绕 Ark Responses 实现，`web_material_worker` 直接依赖 Ark 内置 `web_search`。如果以后切换到非火山
平台：

- 普通 strict structured output 需要重新验证 schema 兼容性；
- function-call SSE 的事件名、参数分片和 tool result 回填方式可能不同；
- Ark 内置 web search 很可能不可用；
- reasoning、context management 和 caching 的请求字段也可能不同。

建议保持 `LLMGateway` 和 `AgentModel` 内部契约不变，为新 provider 写 adapter；联网搜索最好按 7.1 节拆为独立
Search Tool，减少对模型平台内置工具的依赖。不要把新 provider 的 SSE 分支散落到 Graph 节点中。

### 7.6 LLM 分档

当前大多数 LLM 调用使用同一主模型。后续可以按任务复杂度分档：

- 任务书、大纲、Markdown 正文继续使用质量较高的模型；
- 意图识别、素材充分性判断、session memory、orchestrator、冲突分类和主题选择可评估更快、更便宜的模型；
- 需要生成完整 artifact 的节点不要只按成本切换，必须先用固定质量集比较；
- 分档配置应进入环境变量和网关，不要在节点里散落模型 ID；
- 分别统计各 phase 的成功率、结构化修复率、延迟和 token，再决定是否上线。

### 7.7 微信公众号 HTML 后处理

微信公众号编辑器更接近邮件 HTML：关键样式需要内联在元素的 `style` 属性中，`<head>` 通常会被移除，依赖
`<style>`、class、外部 stylesheet、复杂布局或脚本的效果可能在导入后丢失。

当前代码排版器已经尽量生成公众号友好的内联样式，但产品仍只承诺返回 `final_html`，没有单独的“公众号粘贴后处理”
节点。如果出现“浏览器预览正常，导入公众号后样式丢失”，建议增加一个确定性后处理和校验步骤：

```text
当前排版 HTML
  -> CSS/style 内联与不兼容属性清理
  -> 公众号 DOM 约束转换
  -> 正文和图片完整性复检
  -> final_html
```

这项改造需要先和前端确认编辑链路。过早把结构全部扁平化并内联，可能让前端富文本编辑和局部修改变得困难。可选方案
包括：

- 前端编辑内部结构，提交/复制到公众号前再转换；
- 后端同时维护可编辑中间表示，但对外仍只交付一个明确的最终 HTML；
- 将复杂、不可稳定粘贴的局部样式转为图片或 SVG；
- 对确实需要保留复杂交互的内容托管静态 HTTPS 页面，但这不等价于公众号正文排版。

无论选择哪种方式，都要用真实公众号后台粘贴和手机预览验收，不能只看浏览器 iframe。

## 8. 文档阅读顺序

建议按下面顺序阅读：

1. `README.md`：项目定位、启动方式和部署边界；
2. `docs/api.md`：公开接口、请求体、ID 和错误；
3. `docs/responses-like-protocol.md`：跨 Agent 共用的事件层协议和推荐接口语义；
4. `docs/frontend-integration.md`：SSE reducer、HITL 卡片、断线和取消；
5. `docs/changes_for_frontend.md`：素材调研和联网搜索给前端带来的变化；
6. `docs/implement.md`：完整架构、图编排、持久化和容错设计；
7. `docs/web_search_design.md`：联网素材模型和冲突工作流；
8. `docs/agent_engine.md`：通用 ReAct Engine 的唯一实现依据；
9. `docs/open-source-skill-migration.md`：第三方 Skill 调研和迁移证据；
10. `docs/environment-variables.md`：本地与生产配置；
11. 各 smoke report：真实调用、失败案例和验收结果。

其中下面三份文档直接面向产品前端，应在接口或事件变化时同步维护：

- `docs/api.md`
- `docs/changes_for_frontend.md`
- `docs/frontend-integration.md`

`optimization.md` 已停止维护，只保留历史索引；Agent Engine 后续只参考 `agent_engine.md`。

## 9. 部署和运维注意点

- 生产入口是仓库根目录的 `docker-compose.yaml` 和 `scripts/deploy.sh`；
- adapter 使用 `Dockerfile`，runtime 使用 `Runtime.Dockerfile`；
- 生产环境只映射 adapter 的 `8140`，runtime `8141` 保持私网；
- runtime 当前使用进程内 Agent Server backend，不依赖 Redis；
- Graph checkpoint 仍使用独立 PostgreSQL database；
- runtime 目前保持单副本，短期 run registry 不能直接水平扩容；
- 部署脚本负责初始化微信公众号独立 database 和 `article_artifacts`；
- 新增依赖时同时修改 `pyproject.toml`、lock、Dockerfile、镜像源和环境变量文档；
- 项目 `.env` 中可能包含真实密钥，不得提交；
- `langgraph.json` 需要提交，`.langgraph_api/` 是本地运行数据，不得提交；
- debug 关闭不能影响 `/docs`、Apifox、health 或正常产品接口；
- TTL 删除顺序是先删除 Agent Server thread/checkpoint，再删除该 session 的 artifact revisions。

## 10. 接手后的建议检查清单

建议接手后先完成一次不改代码的基线验证：

1. 按 README 启动 runtime、adapter 和开发面板；
2. 检查 `/health/live`、`/health/ready` 和开发面板配置；
3. 运行全量 pytest、Ruff、format check 和 mypy；
4. 用固定数据生成 8 套排版主题；
5. 用真实 Ark 跑一次最短完整流程，并逐个 approve；
6. 完成后提出一次正文或大纲 revise，确认 revision 和入口路由；
7. 在 pending 时断开 SSE，再输入“继续”，确认原卡片重放；
8. 正式 cancel 后再发送修改要求，确认旧 interrupt 已过期且重新路由；
9. 分别在 debug 开启和关闭时检查 SSE，确认线上不会泄漏原始提示词和工具参数；
10. 检查数据库中的 checkpoint、artifact、parent revision 和 expires_at；
11. 用浏览器检查最终 HTML，再做一次真实公众号后台粘贴；
12. 保存有价值的失败案例，不要只保留成功截图。

## 11. 后续工作的建议优先级

| 优先级 | 工作 | 原因 |
|---|---|---|
| P0 | 基线接管、密钥/部署/前端联系人确认 | 先确保服务能维护、能回滚、能定位问题 |
| P1 | 专门图片搜索 API 与 resource 图片消费 | 直接解决公众人物等生图拒绝和网络图片未使用问题 |
| P1 | 真实公众号 HTML 粘贴兼容验证 | 浏览器预览正常不代表公众号编辑器保留样式 |
| P1 | Agent Engine/Skill 迁移代码审查和生产灰度 | 当前功能已跑通，但实现时间较集中，需要继续硬化 |
| P2 | 文章节点接入检索、事实检查和写作 Skill | 有明显质量收益，但改动范围比排版更大 |
| P2 | 模型分档和 provider 解耦 | 可以降成本并减少 Ark 内置搜索绑定，需要先有质量基线 |
| P3 | 增加更多排版主题 | 当前已有 8 套，不是现阶段主要瓶颈 |

## 12. 最后说明

项目目前已经能够完成从请求、素材调研、多个 HITL 到图片和最终 HTML 的完整链路，也已经具备 revision、恢复、取消、
调试面板、真实 Ark function calling 和第一版 Skill Agent。后续维护时最重要的边界是：

1. 不改变外层 LangGraph/HITL/artifact 契约来迁就某个 Skill；
2. 不把第三方 Skill 当作未经审查的黑盒直接执行；
3. 不把搜索摘要当作已经验证的网页事实；
4. 不把断开 SSE 当作取消；
5. 不让 debug 数据进入产品事件、checkpoint 或 artifact；
6. 不只用浏览器预览判断微信公众号 HTML 兼容性。

在这些边界不变的前提下，图片搜索、其它节点 Skill 化、模型分档和 HTML 后处理都可以逐步接入，而不需要推翻现有
工作流。
