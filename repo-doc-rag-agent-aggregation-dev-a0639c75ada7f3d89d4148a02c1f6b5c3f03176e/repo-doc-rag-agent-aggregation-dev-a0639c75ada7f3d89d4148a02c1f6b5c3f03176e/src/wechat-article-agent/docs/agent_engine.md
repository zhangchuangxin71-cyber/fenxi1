# 通用受限 ReAct Agent Engine 实现设计

> 文档状态：已拍板，作为后续 Agent Engine 实现的唯一设计依据
>
> 更新日期：2026-08-21
>
> 第一阶段范围：实现通用 Engine 底座，并只在 `render_html` 节点中接入排版 Skill 固定资产
>
> 第一阶段明确不做：MCP、其它业务节点接入、Engine 内部 checkpoint、临时文件工作区、任意 shell 和开源 Agent CLI

本文从历史调研文档中提取已经确认的决策，并补齐实现所需的契约、边界、目录、阶段计划和验收标准。后续实现、测试和代码审查只以本文为准；`optimization.md` 只保留为历史调研，不再作为实现依据。

## 1. 已确认的方案

### 1.1 核心选择

Agent Engine 采用：

```text
带预算控制的真正 ReAct 执行器
        +
项目内经过审查和适配的固定 Skill 资产
```

Engine 必须真实执行以下循环，而不是把固定流水线包装成 Agent：

```text
model -> tool/skill -> result -> model -> ... -> final
```

模型在当前节点的白名单内自主决定是否读取 Skill、先调用哪个工具、是否根据工具结果返工以及何时提交最终结果。最大轮数、工具次数、上下文、超时和重复失败限制的是运行边界，不改变“模型决定下一步”的性质。

Skill 不直接作为未经审查的运行时黑盒执行。所有第一阶段排版 Skill 都在构建阶段迁移为项目固定资产，保存到：

```text
src/wechat-article-agent/app/skills/rendering/
```

固定资产可以是：

- 改写后的 `SKILL.md` 和 references；
- 主题、组件、token、布局规则和校验规则；
- 将原有固定脚本改写为进程内 strict 工具后的实现；
- 项目自己的输入输出 schema、事实不变量和冲突裁决 policy。

`gzh-design` 和 `xiaowan-wechat-layout-skill` 的使用许可已经取得，不再以许可证作为阻断项。迁移时仍必须记录来源仓库、固定 commit、修改说明和资产 hash，保证可追溯和可回滚。

### 1.2 外层边界

Engine 是 LangGraph 节点内部的执行组件，不替换外层图：

```text
LangGraph render_html 节点
  -> 读取已批准的 Markdown、图片 artifact、任务书
  -> 构造 render_html 的只读 AgentRequest
  -> 启动通用 AgentRunner
  -> Runner 按节点白名单披露 Skill 和工具
  -> 内存中规划、确定性渲染、校验和有限返工
  -> 返回已验证 HTML 与 user_facing_message
  -> render_html 节点写入原有 final_html artifact
  -> 按原有 Graph edge 继续
```

Engine 不得：

1. 修改 LangGraph 外层节点、edge、HITL 或 orchestrator；
2. 跨过外层审批，自己创建或恢复 HITL interrupt；
3. 创建 revision、写 PostgreSQL、修改 checkpoint 或维护自己的恢复游标；
4. 修改已批准 Markdown、图片 URL、图片标注、事实数字或 artifact 元数据；
5. 读写临时文件、执行任意 shell、下载 Skill、运行时安装依赖或访问未经白名单的网络地址；
6. 让模型直接生成并提交任意 HTML/CSS；
7. 将中间 plan、候选 HTML、工具结果或 ReAct 游标作为业务结果保存。

取消、超时、预算耗尽或失败时，所有 Engine 中间状态只存在于本次调用的内存中并被丢弃。只有完整通过校验的结果才可以交给外层节点写入 artifact。

### 1.3 第一阶段业务边界

第一阶段只改造 `render_html`。任务书、大纲、文章、图片和文档研读节点不接入 Engine，但 Engine 的 API、ToolRegistry、Skill loader、事件和上下文机制必须保持节点无关。未来其它节点只能增加自己的白名单、Skill 资产、工具和完成 schema，不应修改 Runner 核心。

本阶段不把“通用 Engine 已经存在”解释为“所有节点已经拥有 Agent 能力”。只有注册了 `NodeAgentProfile` 并通过
本文验收的节点才能启用 Engine；其它节点继续走当前 LangGraph 节点实现。`skill_driven` 是排版默认模式，
仍可通过配置切换到 `llm_decide` 或 `deterministic`。

## 2. 术语和不变量

| 术语 | 含义 |
|---|---|
| Engine | 运行一次受限 ReAct 循环的通用底座 |
| Runner | 驱动 `model -> tool -> result -> model` 的执行器 |
| Skill | 可读的规则、参考资料、主题或组件资产；本项目第一阶段均为构建时固定资产 |
| Tool | 具有 strict 输入输出 schema、权限、超时和调用预算的进程内函数 |
| Plan | 模型提出的结构化主题/组件/章节/图片布局计划，不是最终 HTML |
| Candidate | renderer 根据 Plan 生成的内存候选结果，必须经过 hash 和 validator 校验 |
| 外层 run | LangGraph 的一次业务执行，负责 checkpoint、HITL 和 artifact |
| Engine run | 外层节点内部的一次短生命周期执行，不可独立恢复 |

实现必须始终保持以下通用不变量：

- Engine 不能因为 Skill 文档中的自然语言要求而获得额外权限；权限只由代码中的 ToolSpec 和节点 allowlist 决定。
- 成功状态必须同时满足：completion schema 合法、没有未处理的工具错误，并通过当前 `NodeAgentProfile` 的完成校验；Runner 本身不能硬编码排版完成条件。
- 模型只能引用当前 Engine run 的内存对象；凡是完成结果引用了内存对象，都必须校验对象归属和内容 hash。
- 任何降级结果必须带有可观测的原因，不得将 Engine 失败伪装成 skill 成功。
- Engine 取消后没有可恢复的内部状态；外层下一次请求由 orchestrator 按现有业务规则重新决定入口。

`render_html` Profile 另有以下专属不变量：事实来源是当前批准的 artifact；`generation_basis` 不是
`general_knowledge` 时，排版只允许改变展示结构，不得补写或改写事实；成功时 candidate 引用和 hash 必须匹配，
并通过正文、图片、安全和布局 validator。未来其它节点需要定义自己的完成 schema 和 validator，不能继承这些
HTML 专属条件，也不能要求修改 Runner。

## 3. 外层与 Engine 的数据流

### 3.1 `AgentRequest`

建议以 Pydantic 或等价的严格运行时契约实现。字段语义如下：

```python
class AgentRequest(BaseModel):
    node_name: str
    run_id: str
    task: str
    system_prompt: str
    input_context: Mapping[str, Any]
    skill_allowlist: tuple[str, ...]
    tool_allowlist: tuple[str, ...]
    completion_model: type[BaseModel]
    budget: AgentBudget
    cancellation: CancellationToken
    event_callback: EngineEventCallback | None = None
    debug: bool = False
```

上述代码用于表达字段契约。实际实现建议将包含 callback、类型对象和 cancellation token 的 `AgentRequest`、
`AgentBudget` 写成冻结的 `dataclass`；模型可见输入、ToolSpec 输入输出、ModelTurn 和 AgentResult 使用 Pydantic。
不要为了让不可序列化的运行时对象进入 Pydantic 而开启宽泛的任意类型或跳过校验。

要求：

- `node_name` 是权限边界的一部分，不能由模型或请求体任意修改；
- `input_context` 只放完成当前节点任务必需的摘要、索引和不可变引用；完整 Markdown、图片列表和任务书保存在本次内存上下文中，由受控工具按引用读取；
- `completion_model` 必须是节点定义的 strict schema，不能让模型选择 schema；
- `skill_allowlist` 和 `tool_allowlist` 必须由外层节点代码填写，不能由用户输入或 Skill 内容扩大；
- `debug` 只影响 trace 采集，不改变业务结果、工具权限和预算。

### 3.2 `AgentResult`

```python
class AgentResult(BaseModel):
    status: Literal["completed", "degraded", "failed"]
    output: dict[str, Any] | None
    user_facing_message: str | None
    candidate_id: str | None
    candidate_hash: str | None
    steps: int
    tool_calls: int
    compressed: bool
    fallback_reason: str | None
```

`cancelled` 不作为正常返回状态：收到取消时应传播 `CancelledError`，由外层取消当前 run；不得返回可以被误提交的部分结果。

`candidate_id` 和 `candidate_hash` 是可选的通用内存对象引用，不是业务数据库 ID。`render_html` 必须使用这两个
字段；未来只返回小型结构化结果的节点可以不使用。凡是使用内存对象引用，外层提交前都必须再次确认对象仍属于当前
Runner、hash 匹配且已通过节点 validator。

### 3.3 `RunContext` 和内存对象表

`RunContext` 至少包含：

```python
class RunContext:
    node_name: str
    run_id: str
    cancellation: CancellationToken
    objects: MemoryObjectStore
    metadata: Mapping[str, Any]
```

`MemoryObjectStore` 用于保存过大的工具结果、候选产物和节点内部中间对象。每个对象分配不可预测的 `object_id`
与规范化内容 hash；模型只接收有界预览和引用。对象表以一次 Engine run 为生命周期，不得跨 run 复用。Engine
正常结束、失败或取消时都必须在 `finally` 路径清空对象表，不能把对象写入磁盘、checkpoint 或 PostgreSQL。

“内容编辑在内存中完成”不等于禁止项目包含静态 `SKILL.md`。固定 Skill 文件随镜像发布，服务启动时由 loader
只读加载、校验并形成不可变快照；请求执行期间只能读取这个快照。用户正文、任务书、图片信息、tool result、候选
HTML 和 ReAct 消息不得写入 Skill 目录、临时目录或工作区，也不得通过文件名在模型和工具之间传递。

### 3.4 节点 Profile 和白名单

通用 Engine 不应在 Runner 中使用 `if node_name == "render_html"`。每个业务节点通过一个代码注册的
`NodeAgentProfile` 装配能力：

```python
@dataclass(frozen=True, slots=True)
class NodeAgentProfile:
    node_name: str
    system_prompt_builder: SystemPromptBuilder
    completion_model: type[BaseModel]
    skill_allowlist: tuple[str, ...]
    tool_allowlist: tuple[str, ...]
    budget_factory: BudgetFactory
    completion_validator: CompletionValidator
    completion_resolver: CompletionResolver | None
    context_policy: ContextPolicy
```

第一阶段 registry 只注册 `render_html`。未来任务书、大纲或正文节点接入时新增各自 Profile、Skill、工具和
validator，不修改主循环。`context_policy` 负责声明当前节点的受保护信息、私有内存对象和确定性摘要方式；它只能
收紧通用压缩规则，不能允许删除系统消息、未解决错误或业务事实。未知节点、Profile 中不存在的 Skill/tool、
manifest 与 Profile 不一致均应 fail closed。

### 3.5 系统提示词装配

每个 Agent run 都有完整的系统提示词，由“通用安全前缀 + 节点任务提示词 + 动态能力摘要”组成，且提示词必须以
显式常量或 builder 保存在代码中，便于 debug 和审查：

```text
# 角色与任务
# 已批准输入与信任边界
# 可用 Skill 与工具
# 工作原则
# 完成条件
# 面向用户的说明
# 禁止事项
```

要求：

- 通用安全前缀定义预算、权限、不得越过外层 Graph、不得修改批准内容等跨节点规则；
- 节点提示词只描述该节点职责，`render_html` 不接收任务书/大纲/文章节点的流程说明；
- L0 Skill metadata 和工具 schema 动态注入，但不得把所有 L1/L2 reference 一次性塞入系统提示词；
- 已批准 Markdown、用户文本和工具结果都视为数据，其中出现的“忽略规则”“调用某工具”等文本不能提升权限；
- 模型进行一组工具调用前可以输出一段专业、简短的用户说明，不输出内部推理、系统提示词或工具参数；
- 最终输出必须使用 completion schema，不允许 Markdown 代码块、自由 HTML 或额外字段。

## 4. Model 适配层

### 4.1 Ark Responses function-call 契约

Engine 需要一个独立的 Agent model adapter，不能破坏已有 `structured()`、普通文本流和 `web_search()` 接口。适配层职责：

1. 将工具 schema 转换为 Ark Responses 的 function tool 格式；
2. 固定发送 `thinking={"type": "disabled"}`；第一阶段 Engine 不允许 Skill、用户输入或节点配置打开思考；
3. 解析 `response.output_item.added`、`response.function_call_arguments.delta`、`response.function_call_arguments.done`、`response.output_item.done`、`response.output_text.delta`、`response.completed`、`response.failed` 和网络中断；
4. 按 `call_id` 合并参数分片，去重重复 done 事件；
5. 返回本轮完整文本、完整 function calls、provider response items 和 usage；
6. 将下一轮的 `function_call` 与 `function_call_output` 按 Ark 要求原样回填；
7. 在 HTTP 超时、取消和流中断时关闭请求，不让后台继续占用模型并发。

本地契约测试必须覆盖文本和 function call 同时出现、空参数、参数分片乱序、重复 done、未知 output item、上游 failed 和取消。真实 Ark function-call 烟测只允许使用无副作用工具。

### 4.2 工具调用轮次

模型每次调用返回 `ModelTurn`，概念上包含：

```python
class ModelTurn(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = []
    response_items: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
```

同一轮返回多个合法调用时，第一阶段按模型返回顺序串行执行。每个 `call_id` 在一次 Engine run 内只执行一次。并行执行、工具结果缓存和跨轮复用必须等副作用、资源上限和一致性经过单独评估后再引入。

## 5. Skill 注册、渐进披露和 ToolRegistry

### 5.1 固定 Skill manifest

启动时加载 `app/skills/rendering/integration.yaml` 或等价 manifest；请求期间禁止从 Git、网络或用户输入发现 Skill。

每个 Skill 至少声明：

```yaml
id: gzh_design
version: project-adapted-v1
allowed_nodes: [render_html]
entrypoint: SKILL.md
source:
  repository: https://github.com/...
  commit: <固定 commit>
  license: <已确认许可>
  adapted_at: 2026-08-21
capabilities: [theme_policy, component_policy, html_validation]
reference_allowlist:
  - references/*.md
limits:
  max_file_bytes: 65536
  max_total_bytes: 262144
```

loader 必须在服务启动阶段校验：Skill ID 唯一、路径位于注册根目录内、引用路径不逃逸、文件大小和总大小不超过限制、
声明的 source/hash 与实际文件一致、引用的工具已注册且节点白名单一致。校验成功后形成只读内存快照；任一生产启用
资产校验失败应使 readiness 失败，不能等到用户请求时才暴露半可用状态。生产镜像内只打包固定资产，不在请求中
重新读磁盘、clone、下载或安装依赖。

### 5.2 L0/L1/L2 渐进披露

| 层级 | 注入内容 | 触发方式 |
|---|---|---|
| L0 | ID、摘要、能力、允许节点、可用工具和来源 hash | Engine 启动时，供模型知道可用能力 |
| L1 | 适配后的 `SKILL.md` 主流程、完成条件和冲突裁决 | 模型调用 `load_skill` 或节点策略要求时，从内存快照读取 |
| L2 | 当前主题、组件、validator checklist 等具体 reference | 模型明确选择相关 reference 时，从内存快照读取 |

渐进披露是上下文控制，不是权限控制。模型即使读到 Skill 文本，也只能调用当前 ToolRegistry 白名单中的工具。L2 每次读取都要校验 reference allowlist 和大小预算。

### 5.3 `ToolSpec`

```python
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    allowed_nodes: frozenset[str]
    timeout_seconds: float
    max_calls: int
    max_retries: int
    side_effect: Literal["read_only", "memory_only"]
    retryable_error_codes: frozenset[str]
    semantic_argument_fields: tuple[str, ...] | None
    handler: Callable[..., Awaitable[BaseModel]]
```

工具执行器必须负责：

- strict 输入校验，禁止未知字段；
- node allowlist 和 tool allowlist 校验；
- 每工具调用数和总工具数校验；
- 独立 wall-clock timeout 和取消传播；
- 仅对明确标记为 retryable 的临时错误做有限重试和退避；
- 输出 schema 校验和有界截断；
- 使用 `semantic_argument_fields` 生成重复失败指纹；未声明时使用完整参数，文案字段不得默认被当作语义参数；
- tool started/completed/failed 事件与 debug trace；
- 禁止任意 shell、任意文件写入、任意网络 URL 和隐藏副作用。

### 5.4 `render_html` 白名单

第一阶段只披露以下工具，工具顺序由模型决定，但工具集合和最终完成条件由代码固定：

1. `load_rendering_skill_asset`：读取 GZH/Xiaowan 的 L0/L1/L2 固定资产；
2. `analyze_markdown_layout`：把已批准 Markdown 暴露为只读节点树；
3. `get_skill_theme_catalog`：返回六套 GZH 主题及其适用场景；
4. `get_skill_component_catalog`：返回所选主题允许的组件和变体；
5. `validate_skill_layout_plan`：校验组件、章节节点和装饰预算；
6. `assemble_skill_html`：从固定组件 registry 在内存中装配候选 HTML；
7. `validate_skill_html`：执行 GZH 安全规则、正文冻结、图片证据和 Xiaowan policy。

禁止模型自由书写 HTML/CSS、调用旧 `LayoutEngine` 或八主题 registry、改 Markdown、生成/下载图片或写 artifact。

## 6. Runner 的真正 ReAct 循环

### 6.1 主循环

Runner 的控制逻辑应满足以下伪代码：

```text
创建本次 RunContext 和内存对象表
发送 engine_started
披露节点允许的 Skill L0

while 未达到完成条件:
    检查取消、总耗时和最大步骤
    ContextBudget.preflight(当前消息, 工具 schema, 预留输出空间)
    调用 model(turn)

    if turn 有 function calls:
        发送模型解释文本（如有）
        将 provider response item 写入上下文
        对每个 call_id:
            校验 tool 名称、参数和单工具预算
            tool_started
            执行工具或有限重试
            tool_completed/tool_failed
            将 function_call_output 写入上下文
        continue

    校验最终 completion schema
    调用当前 NodeAgentProfile 的 completion_validator
    若输出引用内存对象，校验对象归属和 hash
    不满足时将明确校验错误回填模型，消耗下一轮预算
    满足时返回 completed

达到预算/超时/上下文边界/重复失败 -> degraded
收到取消 -> 传播取消，不返回结果
```

### 6.2 完成条件

Engine 不能因为模型输出了任意 JSON 就结束。通用完成条件至少包括：

1. 输出符合节点 completion schema；
2. 没有未处理的工具错误或未完成的必要步骤；
3. 当前 `NodeAgentProfile.completion_validator` 成功；
4. 输出中出现的所有内存对象引用都属于当前 run，且 hash 匹配；
5. `completion_resolver` 只能解析已经验证的对象，不能再执行模型调用、工具调用或产生副作用。

`render_html` 的 completion validator 必须进一步确认：candidate 的 renderer 状态有效；
`validate_skill_layout_plan` 与 `validate_skill_html` 已成功；正文、数字、图片 URL、图片位置和标注与输入不变量一致。
未来其它节点通过各自 Profile 声明专属条件，不在 Runner 中增加节点名称分支。

completion schema 错误允许在剩余预算内回填一次或多次结构化校验错误，让模型修正；不得用代码拼接、猜测或补齐模型缺失字段。修复次数仍受最大步骤和总超时约束。

### 6.3 call_id 和重复事件

响应流可能重复发送 `output_item.done` 或连接重试后再次收到同一事件。Runner 必须维护本次 run 的 `seen_call_ids`：

- 同一 `call_id` 的完全相同参数只执行一次；
- 同一 `call_id` 的参数不一致视为协议错误，不能执行第二次；
- 工具结果只回填一次；
- 不能因为重复 SSE 事件增加工具调用预算或重复发送用户活动。

## 7. 上下文控制

### 7.1 输入分层

Engine 不把完整外层对话历史放入上下文。`render_html` 的模型输入分为：

- system prompt：角色、排版任务、事实和安全边界；
- task：本轮要完成的排版目标；
- input context：任务书摘要、章节标题路径、图片 metadata、默认主题和候选主题摘要；
- tool history：模型已经发出的 function call 和工具结果；
- private memory context：完整 Markdown、图片列表和任务书，只可由受控工具读取，不直接作为任意用户文本拼接。

### 7.2 预检和压缩

每轮模型调用前执行 `ContextBudget.preflight()`，估算 system prompt、消息、Skill reference、工具 schema、工具结果和预留输出空间。有效上限取 `AGENT_ENGINE_MAX_CONTEXT_TOKENS` 与模型实际上下文窗口的较小值。估算器不强依赖某一家 tokenizer：所有消息和 schema 先做稳定 JSON 序列化，第一版基线可取
`ceil(max(UTF-8 字节数 / 3, Unicode 字符数 / 2) * 1.2) + 消息固定开销`；如果 provider 返回上一轮 usage，
则用实际/估算比例向上校准本次估算，不能向下调低安全系数。最终还要预留
`AGENT_ENGINE_CONTEXT_RESERVE_TOKENS`。这不是精确计费器，而是请求前的安全闸门；预算临界时应提前压缩或降级，
不能依赖上游截断。

第一版采用确定性的“早期摘要 + 最近轮次保留”策略，不另起摘要 Agent。压缩只能在消息边界执行，算法必须是纯函数，
同一输入在相同预算下得到相同结果：

1. 保留第一条 system message、当前 task/input 和全部未解决的错误/校验反馈；不删除、改写或移动它们；
2. 删除重复工具 schema、已经完成且重复的解释文本和重复 ToolResult，但只能删除明确标记为可重建的项；
3. 早期完整的 function call/function output 对压缩为 `{round, tool_name, success, short_observation, object_id}`，
   摘要放在原消息对的相同位置；
4. 最近 `recent_full_rounds` 个完整 function call/function output 对保持原样，不能留下孤立的 function call 或
   function output；只允许压缩成对的 provider 消息，不得把后续用户修正消息移到摘要之前；
5. 完整 Markdown 保留在只读内存对象中，模型只接收章节/段落索引和有界片段；
6. 图片 URL、图片标注、事实数字、artifact ID、validator ERROR、未完成调用和最终约束不得删除、改写或摘要成不可验证内容；
7. 工具结果超过 `AGENT_ENGINE_MAX_TOOL_RESULT_CHARS` 时，模型只接收截断预览和对象引用，完整值继续留在本次内存对象表；
8. debug 开启时记录压缩前后 token 估算和摘要；debug 关闭时不保留大文本副本。

压缩按 `max_compactions` 计数；每次压缩都必须让估算值下降，否则立即返回 `degraded`，避免无效循环。压缩后仍超限时，Engine 立即返回 `degraded`，不向 Ark 发起一个必然被截断的请求。未来若引入语义摘要，必须是独立 strict 调用、固定 `thinking=False`、有事实保真测试的新功能，不能默默替换确定性策略。

## 8. 预算、错误、超时和取消

### 8.1 环境配置

以下配置由 `.env.example` 说明，并在每个 Engine run 开始时冻结成 `AgentBudget`：

```text
AGENT_ENGINE_MAX_STEPS=12
AGENT_ENGINE_MAX_TOOL_CALLS=16
AGENT_ENGINE_MAX_CONTEXT_TOKENS=131072
AGENT_ENGINE_CONTEXT_RESERVE_TOKENS=8192
AGENT_ENGINE_RECENT_FULL_ROUNDS=3
AGENT_ENGINE_MAX_COMPACTIONS=2
AGENT_ENGINE_CALL_TIMEOUT_SECONDS=120
AGENT_ENGINE_TOTAL_TIMEOUT_SECONDS=300
AGENT_ENGINE_MAX_SAME_TOOL_FAILURES=2
AGENT_ENGINE_MAX_TOOL_RESULT_CHARS=24000
```

这些值是第一版起点，不是允许无限放大的默认承诺。生产调整必须通过烟测、并发和成本观察后进行。

预算包括：

- 模型决策轮数；
- 工具总调用数和每工具调用数；
- 单模型调用 wall-clock；
- 单工具调用 wall-clock；
- Engine 总 wall-clock；
- 上下文 token 估算和压缩次数。

一次 Engine run 中，临时网络重试不能重置总预算，也不能无限增加最大轮数。

预算记账采用以下统一口径：一次模型决策计一个 `step`；模型发出的每个新语义调用计一个 `tool_call`；同一
`call_id` 的重复 SSE 不计费也不执行；工具执行器内部重试记录为 `tool_attempt`，不增加逻辑 tool_call，但仍消耗
总 wall-clock、上游限流和每工具 retry 预算。所有计数在 debug trace 中可见。

预算检查顺序固定为：外层取消信号、Engine 总超时、上下文预检、最大模型步骤、工具总次数、单工具次数、单次
模型/工具超时。一次非法工具名或参数仍计一个 `tool_call`，否则模型可以用无效调用绕过预算；completion 修复调用
仍计一个 `step`。上下文压缩、协议重试、ToolError 回填和模式降级都不能重置本次 Engine 的计数。进入
`llm_decide` 等外层降级模式时创建该模式自己的有限预算，但不得带入上一个模式的剩余上下文或候选对象。

### 8.2 相同工具调用失败检测

将工具名、递归规范化后的语义参数和稳定错误代码组成 fingerprint：

```text
(tool_name, normalized_semantic_arguments_hash, stable_error_code)
```

规范化必须递归排序对象 key，保留数组顺序，将数值和布尔值按 JSON 类型处理，并忽略 ToolSpec 明确标记的非语义
字段（例如 `user_facing_message`），防止模型只改解释文案就绕过循环检测。错误指纹使用代码/类别，不能使用可能包含
时间戳或 request ID 的完整错误文本。

同一 fingerprint 自上一次该工具成功以来累计达到 `AGENT_ENGINE_MAX_SAME_TOOL_FAILURES`，即认定为重复失败并
提前降级；中间调用其它工具不能清零，否则交替调用两个失败工具可以逃逸。该工具成功后清除它的失败指纹；真正改变
语义参数或错误类别会形成新 fingerprint，不应被误判。即使参数不断变化，仍受单工具次数、工具总次数和最大步骤
约束，不会无限循环。

### 8.3 错误矩阵

| 错误类别 | 示例 | Engine 行为 | 外层行为 |
|---|---|---|---|
| 临时可重试 | 网络超时、429、上游 5xx、短暂连接失败 | 在单调用 `max_retries` 和总预算内退避重试 | 仍失败则进入排版降级 |
| 协议可修复 | 非法 JSON、未知工具、缺 required 字段、ToolResult schema 错误 | 回填明确 ToolError，让模型在剩余步骤内纠正 | 修复失败则降级 |
| 相同调用反复失败 | 同参同错连续达到阈值 | 立即 `degraded`，不再重复调用 | 进入下一种排版模式 |
| 最大步骤/工具数 | 模型一直规划或工具过多 | `degraded`，不伪装成功 | 进入下一种排版模式 |
| 上下文仍超限 | 压缩后仍超过窗口 | `degraded`，不再请求模型 | 进入下一种排版模式 |
| 不可修正输入 | 缺少批准 Markdown、图片数据损坏、引用失效 | 返回明确失败，不生成候选 | 由外层业务错误策略处理 |
| 用户取消 | 外层 cancel、连接取消传播到 run | 立即取消子任务，清空内存结果 | 不写 artifact/checkpoint，下一次重新路由 |

### 8.4 排版降级链

排版节点的模式和降级顺序固定为：

```text
skill_driven
  -> llm_decide
  -> deterministic（HTML_LAYOUT_DEFAULT_THEME）
  -> legacy HtmlRenderer
  -> HTML_LAYOUT_FAILED
```

`skill_driven` 失败时不能再次启动一个无预算的 Agent。`llm_decide` 仍只负责选择固定主题，不能生成整篇 HTML。`deterministic` 和 `legacy` 继续使用现有代码路径，最终仍只返回 `final_html`。

外层 `render_html` 节点按以下顺序处理 Engine 结果：

1. `completed`：检查候选对象仍属于当前 Engine run、hash 与 validator 结果匹配，然后一次性提交 `final_html`；
2. `degraded`：记录 `fallback_reason`，丢弃当前 Engine 的候选对象，不把部分结果写入 artifact，转入下一种排版模式；
3. `failed`：区分“输入不可修正”和“执行失败”。输入不可修正时返回明确业务错误；执行失败时按同一降级链处理；
4. 任一模式成功后停止后续 renderer，不重复覆盖已经验证的结果；所有模式失败才返回 `HTML_LAYOUT_FAILED`。

降级必须是一次请求内的有界操作：每个下一模式使用自己的调用预算，但不得重试已经判定为不可修正的输入，也不得
把上一个模式的内部消息、工具结果或候选 HTML 带入下一个模式。外层 artifact 写入与本次节点成功状态必须在同一条
成功路径上完成，不能先保存候选再异步校验。

### 8.5 超时和取消传播

模型请求、工具调用和整个 Runner 都有独立 wall-clock timeout。取消必须传播到：

1. Ark HTTP streaming 请求；
2. 当前模型决策 task；
3. 当前工具 task；
4. 等待队列中的模型/工具 task；
5. Engine 内存对象表。

只断开 SSE 而让后台继续生成不算取消。取消后的候选、工具结果、游标和 debug 原始输入都不能进入 artifact、checkpoint 或产品事件；用户下一次请求由外层 orchestrator 决定从哪个业务阶段重新开始。

## 9. SSE、用户体验和 debug

### 9.1 产品事件

Engine 复用现有 Responses-like 事件，不新增与前端绑定的 Engine 私有协议：

```text
response.output_text.delta  # 模型面向用户的简短解释文本
response.reasoning.delta    # 若未来允许某阶段思考，按既有协议转发；第一阶段 Engine 固定关闭思考
agent.activity              # kind=skill 或 kind=tool 的可见活动
agent.artifact              # 外层提交的 final_html
```

事件要求：

- 一组同一意图的工具调用只发送一段简短、专业的解释文本；
- Skill 加载和具体工具调用使用稳定 activity ID，开始/完成/失败更新同一活动；
- `agent.activity.kind=skill` 表示读取或应用 Skill policy，`kind=tool` 表示具体工具；
- 事件内容不得泄露系统提示词、完整 Markdown、内部错误堆栈、密钥或未授权文档内容；
- Engine 不创建 HITL，任何用户审批仍由外层 LangGraph 节点负责。

### 9.2 debug trace

debug 开启且全局 debug 开关允许时，记录到现有 debug trace：

- 每轮模型请求的 phase、工具 schema、输入和原始输出；
- function call 的 `call_id`、名称、完整参数和 provider item；
- 工具执行的开始/结束时间、参数、结果、错误、重试和耗时；
- Skill source、commit/hash、L0/L1/L2 层级和 reference；
- 上下文 token 估算、压缩前后大小、预算消耗和 fallback 原因。

debug 关闭时：

- 不构造或保存完整 prompt、Skill reference 和完整 tool result；
- 只生成前端正常体验所需的小型 `agent.activity` 摘要；
- 不把 debug 数据写入 checkpoint、artifact 或产品事件；
- 请求体中的 `debug=true` 不能绕过服务端全局关闭开关。

### 9.3 `agent.activity` 字段约定

Engine 不新增协议名称，但必须将内部事件映射为现有 `agent.activity` 的通用字段。字段名以当前
`responses-like-protocol.md` 为准，最小语义如下：

```json
{
  "type": "agent.activity",
  "activity_id": "eng_<run_id>_<call_id>",
  "kind": "skill",
  "status": "running",
  "name": "load_rendering_skill_asset",
  "title": "读取排版规范",
  "summary": "正在读取当前节点允许使用的排版规范。"
}
```

工具活动将 `kind` 改为 `tool`，`name` 使用注册的稳定工具名；开始、完成和失败只更新同一个
`activity_id`，不能因为重复 provider SSE 产生第二张卡。`summary` 面向用户，只能是短文本，不能包含完整参数、
Markdown、HTML、URL 中的敏感查询或内部堆栈。工具参数和结果仅在 debug 全局开关开启时写入 trace。

模型在工具调用前输出的解释文本继续映射为 `response.output_text.delta`。它与工具 activity 是两条独立事件流：
解释文本可以打字机式增量展示，工具卡在收到 `running` 后更新为最终状态。若模型没有输出解释文本，Engine 不得
用内部推理或工具参数替代用户文案。

## 10. 排版 Skill 固定资产方案

### 10.1 资产职责分工

首阶段不把所有 Skill 作为平级黑盒交给模型，而采用“一个主资产 + policy/reference + 项目工具”的组合：

| 资产 | 迁移职责 | 第一阶段边界 |
|---|---|---|
| `gzh-design` | 主题索引、组件语义、token、正文节点结构、公众号 HTML 规则和可自动化 validator | 不直接运行其 Agent 主流程；不让模型自由拼接整篇 HTML |
| `xiaowan-wechat-layout-lite` | 正文冻结、图片证据、强调密度、移动端视觉和发布前检查清单 | 改写为项目 `preflight/postflight` policy；人工粘贴和截图反馈只做烟测 |
| WeWrite / aiworkskills 的排版资产 | Python 转换、主题契约、sanitize、validator 行为作为交叉参考 | 不并行维护第二套 renderer，不复制发布或文件工作流 |
| Baoyu 排版资产 | 图片占位、脚注和主题覆盖行为作为测试参考 | 不引入 Bun、Chrome/CDP、发布 API 或浏览器自动化 |
| 其它写作/配图 Skill | 记录未来接入候选，暂不进入 `render_html` 白名单 | 不在第一阶段影响 Engine 或排版结果 |

多个来源存在冲突时，优先级固定为：

```text
项目事实和安全不变量
  > render_html 节点 policy
  > 用户已批准 artifact
  > integration manifest
  > Skill 原文和示例
```

### 10.2 必须保留的不变量

- 已批准正文只读，不能增删事实、段落、数字、署名或 CTA；
- 图片只消费已有 URL、标注和插入位置，不下载、不合成、不改变图片事实；
- `generation_basis != general_knowledge` 时，排版不得补充素材之外的事实；
- 主题、组件、颜色和间距只能来自固定 registry 或严格 plan schema；
- 输出必须经过 sanitizer、HTML validator、正文完整性检查、图片一致性检查和移动端 policy；
- 产品仍只保存和返回 `final_html`，不新增 body_html/preview_html artifact 契约。

### 10.3 Skill 资产目录

```text
src/wechat-article-agent/app/
  agent_engine/
    contracts.py       # AgentRequest/Result、ModelTurn、ToolCall、Budget
    model.py           # Ark function-call SSE 适配，固定 thinking=False
    runner.py          # 真正的 model -> tool -> result -> model 循环
    context.py         # token 估算、预检、确定性压缩、内存对象表
    policy.py          # allowlist、预算、超时、取消、错误分类
    registry.py        # Tool/Skill 注册、来源 hash 和节点白名单
    skill_loader.py    # L0/L1/L2 读取、路径和大小限制
    events.py          # Engine 事件到现有 Responses-like 事件的映射
    errors.py          # retryable/degraded/failed/cancelled 错误
    tools/
      base.py
      registry.py
  rendering/
    wechat_layout/      # deterministic/llm_decide 和外层降级路径
    skill_driven/
      adapter.py        # render_html 的 NodeAgentProfile、输入装配和完成校验
      tools.py          # GZH/Xiaowan 排版 ToolSpec
      assembler.py      # 固定组件的内存装配器
      component_registry.py
      gzh_validator.py
      xiaowan_policy.py
  skills/
    rendering/
      integration.yaml
      gzh_design/
        SKILL.md
        references/
        assets/
      xiaowan_layout/
        SKILL.md
        references/
        assets/
```

`app/agent_engine/` 不得出现公众号主题名、组件 HTML 或文章业务字段；排版节点到通用 Engine 的适配放在现有
`app/rendering/wechat_layout/` 内。`app/skills/rendering/` 只保存固定 Skill 资产，不得承载 Runner、LangGraph
状态或数据库访问。

## 11. 分阶段实现计划

### E0：Ark function-call 模型适配

1. 在不改变现有结构化调用的情况下，增加独立 Agent model adapter；固定 `thinking=False`。
2. 完成 function-call SSE 事件解析、参数分片合并、重复事件去重、失败/取消/超时传播。
3. 完成 `function_call` 和 `function_call_output` 下一轮回填。
4. 添加本地 fake Responses SSE 契约测试。
5. 用无副作用 echo/plan 工具完成一次真实 Ark function-call 烟测，记录请求/响应字段和兼容结论。

**阶段门槛**：现有 `structured()`、普通文本流、web search 和全量旧测试不回归；所有取消路径都能终止 HTTP 请求。

### E1：通用契约、Registry 和 Runner

1. 实现 `AgentRequest`、`AgentResult`、`AgentBudget`、`RunContext`、`CancellationToken`。
2. 实现 strict `ToolSpec`、节点/工具 allowlist、调用次数、超时、有限重试和错误分类。
3. 实现 `SkillLoader` 的 manifest、来源 hash、L0/L1/L2、reference allowlist 和大小限制。
4. 实现真正 ReAct 主循环、串行多调用、call_id 去重、完成 schema 和候选 hash 校验。
5. 实现相同工具调用 fingerprint、预算耗尽和重复失败降级。

**阶段门槛**：fake model 可以改变工具顺序，Runner 不依赖硬编码排版顺序；失败不会调用 renderer 或提交 artifact。

### E2：上下文和可观测

1. 实现上下文 token 保守估算、输出预留和请求前预检。
2. 实现成对保留最近 function call/output 的确定性压缩。
3. 实现有界工具结果、内存对象引用和执行结束清理。
4. 接入 `agent.activity kind=skill/tool`、解释文本、debug LLM/tool trace 和预算信息。
5. 验证 debug 关闭时不构造大体积 prompt、reference 和工具结果。

**阶段门槛**：上下文超限在发起 Ark 请求前降级；取消/失败不留下可恢复内部游标。

### E3：固定 Skill 迁移

按以下顺序迁移，避免先把多个冲突 renderer 同时接入：

1. `gzh-design`：主题索引、组件语义、token、正文节点结构和 validator 规则；按项目 schema 重写为只读资产和工具。
2. `xiaowan`：`layout-standard`、正文冻结、图片证据、强调预算、移动端检查和 release checklist；改写成阻断级/警告级 policy。
3. 第一阶段不把 WeWrite、aiworkskills、Baoyu 或其它 Skill 登记到运行时白名单，避免多套 renderer 与 policy 冲突；后续需经过独立 eval 才能新增。

每次迁移必须同时提交：来源 commit、授权记录、改造说明、冲突裁决、输入输出契约、固定 HTML snapshot 和回滚说明。

**阶段门槛**：所有生产可加载资产都已固定版本、通过路径/大小校验和 snapshot 测试；没有未经 ToolSpec 包装的
上游脚本、文件操作或运行时依赖进入 Engine。

### E4：`render_html` 竖切

1. 以 E3 迁移的 `gzh-design` 主题/组件规则为主资产。
2. 以 E3 迁移的 `xiaowan` 正文冻结、图片证据和移动端 policy 为辅助规则。
3. 接入 `load_rendering_skill_asset`、Markdown 分析、主题/组件目录、计划校验、组件装配和 HTML 校验七个工具。
4. 模型只输出 strict layout plan，HTML 仅由 `skill_driven` 专用组件装配器生成，不调用旧代码主题 renderer。
5. validator 失败时允许模型在剩余预算内重新提交计划。
6. Engine 成功返回验证后的候选；否则按 `skill_driven -> llm_decide -> deterministic -> legacy` 降级。
7. 外层保存原有 `final_html`，不增加新的持久化表和 checkpoint 字段。

**阶段门槛**：固定输入的正文、图片 URL、图片位置、事实数字和 artifact 字段与现有模式一致；开发面板能同时看到节点活动、Skill/tool 活动、解释文本和 debug trace。

## 12. 测试和验收标准

### 12.1 Ark 协议契约测试

必须覆盖：

- function-call added、argument delta/done、item done 和 completed；
- 参数分片合并、乱序/重复 done、空参数和多个 call；
- 文本解释与工具调用共存；
- reasoning/usage/failed/error 事件；
- 上游 429、5xx、网络中断、HTTP 超时和 asyncio cancellation；
- 未知工具、非法 JSON、缺少 required 字段和 ToolResult 回填；
- `thinking={"type":"disabled"}` 始终存在。

### 12.2 Engine 单元测试

必须覆盖：

1. 模型先读 Skill、先取主题、先校验等多种合法顺序都能完成；
2. 非法工具名、额外参数、未知主题/组件和无效 candidate 不调用 renderer；
3. strict completion 失败时只在剩余预算内修复，不拼接 JSON；
4. 同参同错连续达到阈值只降级一次，不把不同参数误判为循环；
5. 达到最大步骤、工具次数、上下文和总超时后停止当前模式；
6. 模型等待、工具执行、结果回填三个时点取消都能取消子任务并清空对象表；
7. call_id 去重、重复工具结果不重复执行；
8. debug 开关分别验证完整 trace 与不产生大体积原始数据；
9. fallback 不提交候选，不修改 artifact。

### 12.3 Skill 和排版集成测试

- manifest 重复 ID、路径逃逸、未允许 reference、hash/大小限制必须失败；
- L0/L1/L2 逐级读取，未明确需要时不把完整 Skill 注入上下文；
- `gzh-design` 与 `xiaowan` policy 冲突时按本文优先级裁决；
- 固定 `tests/rendering_data/input` 对所有启用主题运行 deterministic、llm_decide 和 skill_driven；
- 正文、数字、图片 URL、图片位置、图片标注和 HTML artifact 字段不变；
- HTML 通过安全、结构、正文完整性、图片一致性和移动端 policy；
- 失败按完整降级链产生可解释的 `fallback_reason`。

### 12.4 开发面板与真实烟测

必须完成：

1. 一次真实 Ark 无副作用 function-call 循环；
2. 一次真实 `skill_driven` 排版，至少发生一次 Skill/tool 活动；
3. 一次 validator 失败后的有限返工或受控降级；
4. debug 开启时开发面板 trace 能看到 LLM 原始输入输出、Skill/tool 参数、结果、耗时和 fallback；
5. debug 关闭时只看到产品 artifact、节点活动和聚合 tool/skill 活动，不看到原始 trace；
6. 取消、超时、断线后外层重新请求不会恢复 Engine 内部游标或提交半成品；
7. 375px 和 390px 预览检查，以及至少一次公众号后台粘贴观察；
8. 真实输入输出和最终 HTML 保存到 `docs/smoke-artifacts/`，失败案例也要记录原因、事件序列和降级模式。

### 12.5 生产启用门槛

以下条件用于持续验证默认的 `HTML_LAYOUT_RENDERER=skill_driven`，不是切换回旧模式的前置条件：

- E0--E4 自动测试和全量旧回归通过；
- 没有正文、图片事实、artifact 完整性和安全回归；
- 并发约 30 的压力下，Engine 总耗时、模型调用数和内存对象表有界；
- cancel、timeout、fallback 不写中间 artifact/checkpoint；
- debug 关闭时不产生大体积 trace；
- 新增依赖（如有）已写入 `pyproject.toml`、锁文件、Dockerfile、镜像源和部署说明，并完成干净容器构建静态检查；
- 开发面板、Responses-like adapter、SSE 和真实公网配置验收通过。

## 13. 依赖、部署和安全门槛

第一阶段优先复用当前 Python 依赖和确定性 renderer，不引入 MCP、Bun、Node.js、Chrome/CDP 或浏览器自动化。迁移 Skill 时优先重写为项目内 Python/内存工具；只有经过评估确实能显著提升质量、且无等价现有依赖时，才允许加入低风险依赖。

任何新依赖都必须同步：

1. `pyproject.toml` 和 lock 文件；
2. 服务 Dockerfile/Runtime Dockerfile；
3. Docker build 的镜像源和超时配置；
4. 生产与本地 `.env.example`、部署脚本和环境变量文档；
5. 干净容器构建静态检查和依赖许可证记录。

生产镜像不得在启动或请求期间联网下载 Skill、npm 包、模型插件或浏览器资源。Skill 目录必须以固定版本进入镜像，工具只能访问明确的内存输入和应用内数据。

## 14. 实施后检查清单

实现每个阶段合并前，逐项回答：

- 是否仍是模型决定工具顺序的真实 ReAct，而不是固定流程伪装？
- 是否存在任何没有 node/tool allowlist 的工具？
- 是否存在任意 shell、文件写入、外部下载或隐藏副作用？
- 是否固定关闭 Engine thinking？
- 是否在请求前完成上下文预检，且压缩没有丢失事实和图片信息？
- 是否所有预算、重试、连续失败、超时和取消都有测试？
- 是否 Engine 失败时没有写 artifact/checkpoint？
- 是否 debug 关闭时没有构造大体积原始 trace？
- 是否 `render_html` 仍只返回原有 `final_html` 并沿用外层 Graph edge？
- 是否新增 Skill 都有来源、hash、适配说明、冲突裁决和 snapshot？

## 15. 已确认项和未决项

以下项目已明确，不需要再次拍板：

1. 使用“带预算控制的真正 ReAct 执行器 + 项目内迁移的固定 Skill 资产”；
2. Engine 是通用底座，第一阶段只接入 `render_html`；
3. Skill 统一保存于 `app/skills/`，首阶段位于 `app/skills/rendering/`；
4. 第一阶段不引入 MCP，不建立 Engine 内部 checkpoint，不读写临时文件；
5. 模型、工具和 Skill 使用节点白名单；
6. Engine thinking 第一阶段硬编码关闭；
7. 上下文采用请求前预检和确定性压缩；
8. 有最大步骤、工具次数、上下文、单次/总超时和连续相同失败预算；
9. 取消不保存中间结果，下一次由外层 orchestrator 重新决定；
10. 排版失败链为 `skill_driven -> llm_decide -> deterministic -> legacy`；
11. 产品接口继续只返回 `final_html`，不新增内部 Engine 持久化协议；
12. `gzh-design` 负责主题/组件主资产，`xiaowan` 负责正文冻结和验收 policy，其他 Skill 只迁移不冲突的可用资产；
13. 真实 Ark、开发面板、固定数据、取消/超时和部署静态检查均属于实现后的验收范围。

当前没有阻塞下一轮实现的设计确认项。实现时可以根据真实 Ark SSE contract test 调整内部模块名、字段名和预算默认值，但不得改变本文已经确认的外层边界、ReAct 性质、固定 Skill 资产、取消语义、降级链和验收门槛。若真实协议与本文示例字段不一致，应更新 adapter 的契约测试和本节示例，而不是在业务节点中添加临时解析分支。
