# `skill_driven` 排版重构设计与实现计划

> 文档状态：已实现，作为 `skill_driven` 排版实现与验收依据
>
> 更新日期：2026-08-21
>
> 本文只规定排版 Skill 迁移和 `skill_driven` 节点内部实现。通用 Agent Engine、LangGraph 外层图、HITL、artifact、checkpoint、Responses-like 协议和 `llm_decide`/`deterministic` 模式不在本文重新设计。

## 1. 重构结论

当前 `skill_driven` 实现存在根本性偏差：Agent 虽然完成了多轮工具调用，也读取了 Skill，但最终仍调用原有 `LayoutEngine`，只是在八个项目主题中选择一个 `theme_id`。这与 `gzh-design-skill` 的核心能力不一致，也不能产生作者演示中的丰富组件效果。

本次必须推翻当前排版 Skill 相关的业务实现，保留并复用以下通用基础设施：

- `app/agent_engine/` 下的受限 ReAct Runner、Ark function-call 适配、上下文预算、取消、工具白名单、Skill Loader 和 debug 事件；
- Markdown 解析、图片 artifact 的只读输入契约、HTML 安全基础工具，以及现有回归测试基础设施；
- `skill_driven -> llm_decide -> deterministic -> legacy` 的外层降级语义。

必须移除或不再由 `skill_driven` 使用：

- 现有八主题 `ThemeRegistry`、`professional-clean` 默认主题选择和 `LayoutEngine.render()`；
- 现有 `LayoutPlanInput(theme_id, component_profile, heading_paths, image_position_ids)` 作为 Skill 排版的完成契约；
- 将 Skill 仅用于“选一个主题，再调用旧 renderer”的逻辑；
- Skill 成功时将旧代码排版结果伪装为 Agent+Skill 结果的行为。

新的成功链路必须是：

```text
已批准 Markdown + 图片 artifact
  -> 受限 Agent 读取 gzh-design/xiaowan 固定资产
  -> 识别文章结构和文章类型
  -> 选择 Skill 主题、文章骨架和组件配方
  -> 输出富结构 SkillLayoutPlan
  -> 项目内组件装配器从固定组件资产装配内联 HTML
  -> GZH 兼容校验 + Xiaowan 正文/图片/移动端 policy
  -> Agent 在预算内根据校验错误返工
  -> 返回通过校验的 fancy final_html
```

Agent 不直接凭空生成整篇 HTML。HTML 必须由固定组件资产和确定性装配器产生，但 Agent 必须真正决定章节级、段落级和图片级的组件组合。这样既保留 Skill 演示中的丰富视觉能力，又避免模型自由书写 HTML 导致事实、标签和样式不可控。

## 2. 上游资产和迁移范围

本项目已经取得 `gzh-design-skill` 和 `xiaowan-wechat-layout-skill` 的使用授权。迁移仍需记录来源仓库、固定 commit、适配说明和资产 hash，保证可追溯、可回滚和结果可复现。

### 2.1 `gzh-design-skill`：主排版资产

固定来源：`isjiamu/gzh-design-skill@ba1f4175519b481cb3566616c9e5178705067904`。

必须迁移并适配的资产：

1. `references/theme-index.md`：主题清单、适用场景和每个主题组件库的入口。
2. 首批六套主题组件库：
   - `theme-moyu-green.md`
   - `theme-red-white.md`
   - `theme-graphite-minimal.md`
   - `theme-zen-whitespace.md`
   - `theme-moyu-ticket.md`
   - `theme-olive-journal.md`
3. `references/common-components.md`：代码块、图片/GIF、小标签、金句、提示条等跨主题增量组件。
4. 各主题中的完整文章模板骨架、文章类型到组件组合配方、Markdown 映射规则和视觉层级。
5. `scripts/validate_gzh_html.py` 的检查规则，改写成项目内存 validator；不在请求时执行未登记的上游脚本。
6. `scripts/component_lint.py` 中与危险标签、外部样式、非法属性和组件结构相关的阻断规则。
7. `references/format-normalize.md` 中与 Markdown 结构归一化有关的规则；本项目第一版只接收已批准 Markdown，不迁移 docx/PDF/文件读取流程。

不迁移：

- 自定义主题生成器和运行时写入主题目录的能力；
- `extract_docx.py` 等文件输入工具；
- 预览页面复制按钮、文件写入和外部发布流程；
- 任何 shell、网络下载、公众号后台发布或运行时安装行为。

### 2.2 `xiaowan-wechat-layout-skill`：流程和验收资产

固定来源：`cyberxiaowan/xiaowan-wechat-layout-skill@5a96543a5f47c0f82f74e4ce5041fbe6f6b57c45`。

必须迁移并改写为项目 policy：

- `references/workflow.md`：`BRIEFED -> FROZEN -> ASSETS_READY -> STRUCTURED -> HTML_READY -> PASTE_READY` 状态语义；
- `references/layout-standard.md`：首屏、章节层级、留白、组件密度和移动端布局要求；
- `references/release-checklist.md`：脚本、危险 URL、外部 stylesheet、复杂 CSS、正文修改、图片失配等阻断规则；
- `references/feedback-guide.md`：问题分类和后续调整方向，第一版只作为 debug/验收信息，不建立长期学习存储。

Xiaowan 不提供主题 renderer。它不能单独生成 fancy HTML，必须作为 GZH 组件资产之上的内容冻结、图片证据、装饰预算和发布前验收层。

## 3. 新目录边界

排版 Skill 相关实现必须放在独立子目录，不污染现有确定性主题代码：

```text
app/
  agent_engine/                         # 保留，通用受限 ReAct Engine
  skills/rendering/                     # 固定、可读、可审计的 Skill 资产
    integration.yaml
    THIRD_PARTY_NOTICES.md
    gzh_design/
      SKILL.md
      references/
        theme-index.md
        common-components.md
        theme-*.md
    xiaowan_layout/
      SKILL.md
      references/
        workflow.md
        layout-standard.md
        release-checklist.md
        feedback-guide.md
  rendering/
    wechat_layout/                          # deterministic/llm_decide 与降级路径
    skill_driven/                           # 与旧代码排版并列的全新 Skill 实现
      __init__.py
      contracts.py                       # SkillLayoutPlan、组件计划、候选和报告
      asset_catalog.py                   # 主题/组件/配方清单和启动快照
      markdown_model.py                  # Markdown 节点、段落、图片和事实位置
      component_registry.py              # 固定组件模板、语义、参数和允许组合
      assembler.py                       # 内存组件装配器，只能使用 registry 组件
      gzh_validator.py                   # GZH validator 的项目内实现
      xiaowan_policy.py                  # 正文冻结、图片证据、移动端和装饰预算
      tools.py                            # Skill 排版 ToolSpec
      adapter.py                          # 调用 AgentRunner 的 render_html 适配层
      prompts.py                         # Skill 排版系统提示词和错误回填文本
  tests/
    rendering_data/
      input/
      skill_driven/
        output/
        screenshots/
```

`app/rendering/wechat_layout/engine.py`、现有八主题 registry 和旧 renderer 仍保留给 `deterministic`/`llm_decide`/`legacy` 降级使用，但新的 `skill_driven` 代码不得导入它们。

## 4. Agent 与组件装配契约

### 4.1 Skill 的渐进披露

`render_html` Profile 只允许以下固定 Skill：

```text
gzh_design
xiaowan_layout
```

推荐披露顺序不是业务硬编码顺序，而是模型在白名单内决定；但完成前必须满足：

1. 已读取 GZH 的主题索引和所选主题组件库；
2. 已读取通用增量组件库；
3. 已读取 Xiaowan 的布局标准和 release checklist；
4. 当前计划引用的每个组件均来自本次启动快照和 allowlist；
5. 所有组件都能被项目 `ComponentRegistry` 解析。

Skill Loader 继续使用 L0/L1/L2：L0 为 metadata，L1 为工作流，L2 为主题组件或 policy reference。组件库必须在服务启动时读取、校验和 hash；请求期间只从内存快照读取，不从 GitHub 或文件系统下载。

### 4.2 富 `SkillLayoutPlan`

旧的 `theme_id + component_profile` 计划必须被新的富计划替换。计划不包含模型生成的 HTML，而是包含严格 allowlist 中的结构化组件引用：

```json
{
  "theme_id": "moyu-green",
  "article_type": "tutorial",
  "hero": {
    "component_id": "hero-cover",
    "title_source": "markdown_title",
    "intro_source": "first_quote"
  },
  "toc": {
    "enabled": true,
    "component_id": "toc-scroll",
    "section_ids": ["section_01", "section_02", "section_03"]
  },
  "sections": [
    {
      "section_id": "section_01",
      "heading_path": ["章节一"],
      "heading_component_id": "numbered-heading",
      "body_component_id": "body-paragraph",
      "decorations": [
        {"node_id": "paragraph_003", "component_id": "key-point-card"}
      ],
      "image_component_ids": ["image-caption-frame"]
    }
  ],
  "closing": {"component_id": "end-signature"},
  "user_facing_message": "已根据文章结构选择主题组件，并开始进行正文与图片一致性校验。"
}
```

字段约束：

- `theme_id`、`article_type`、所有 `component_id` 使用动态 enum，禁止模型创造名称；
- `title_source`、`intro_source`、`heading_path`、`node_id` 只能引用解析后的 Markdown 节点；
- 计划必须覆盖全部章节和图片位置，不能通过漏掉节点来规避正文保真校验；
- 装饰组件按主题配方和 Xiaowan 装饰预算限制数量，不能每段随机添加卡片；
- 目录文字只能来自已有标题；作者签名、CTA、关注提示等原文不存在的业务文字不得因组件模板而自动添加；
  结尾组件在没有签名来源时只能使用纯装饰 END/分隔组件；
- `user_facing_message` 只包含专业、简短的排版进度说明；
- `generation_basis != general_knowledge` 时，计划不能产生任何新的文字事实、数字、图片 URL 或说明文本。

### 4.3 工具职责

新的 Skill 排版工具至少包括：

| 工具 | 作用 | 是否真正生成 HTML |
|---|---|---:|
| `load_rendering_skill_asset` | 读取已校验的工作流、主题库、通用组件和 policy | 否 |
| `analyze_markdown_layout` | 将批准 Markdown 转为只读节点树、段落 ID、图片位置和文章结构摘要 | 否 |
| `get_skill_theme_catalog` | 返回 GZH 主题、适用场景、组件库 hash 和配方索引 | 否 |
| `get_component_catalog` | 返回当前主题允许的组件语义、参数和组合限制 | 否 |
| `validate_skill_layout_plan` | 校验计划是否覆盖输入、组件是否存在、配方和预算是否满足 | 否 |
| `assemble_skill_html` | 使用固定组件模板和只读节点引用装配内联 HTML | 是，唯一装配入口 |
| `validate_skill_html` | 执行 GZH 结构/安全规则、正文和图片不变量、Xiaowan policy | 否 |

`assemble_skill_html` 不接受任意 HTML、CSS、模板字符串、URL 或文件路径作为模型参数。它只接受已经通过 `validate_skill_layout_plan` 的 component ID 和输入节点 ID，并从启动时的 `ComponentRegistry` 取模板。组件模板中的动态内容只能来自批准 Markdown、图片 artifact 和受控主题 token。

### 4.4 真正的 Agent 价值

Agent 的决策必须能影响最终 HTML，而不只是影响日志。最低要求是 Agent 可以决定：

- 六套 GZH 主题中的主题和对应完整骨架；
- 文章类型对应的组件配方；
- 是否使用引言卡、目录导航、章节编号、金句块、提示条、数据卡、步骤标签、timeline、图片说明框和结尾签名；
- 哪些已存在的 Markdown 节点使用允许的强调组件；
- 图片如何选择已有插入位置对应的图片容器组件。

若 Agent 只返回主题名、没有章节/组件计划，或最终 HTML 的组件结构与默认模板没有差异，则必须视为 `SKILL_LAYOUT_NO_EFFECT` 并进入失败降级，不能把旧 renderer 结果标记为 Skill 成功。

## 5. Fancy 视觉目标和质量边界

“能够调用 Skill”不是验收标准，“生成明显更丰富且仍然可发布的页面”才是本次验收标准。

### 5.1 必须出现的视觉层级

对于有标题、引言和至少三个章节的文章，正常 `skill_driven` 结果至少应具备：

1. 主题专属首屏/标题结构，而不是普通 H1；
2. 章节之间的主题分隔和章节编号或主题标题组件；
3. 主题专属目录/导读组件；
4. 至少一种主题专属强调组件；
5. 至少一种引用、提示、数据、步骤或列表组件，按文章类型选择；
6. 已有图片使用主题图片容器、说明或证据组件；
7. 主题专属结尾组件；
8. 组件之间具有统一的颜色、间距、字体层级和装饰预算。

短文章、没有引用/列表/图片的输入可以减少对应组件，但不得退化为只有普通段落和普通标题。

### 5.2 可量化验收指标

对固定长文章 fixture，Skill HTML 必须同时满足：

- 与 `professional-clean` deterministic HTML 的正文可见文本完全一致；
- 图片 URL、图片数量、插入位置和标注完全一致；
- 至少使用 5 种不同的 Skill 组件语义，其中至少 3 种来自所选 GZH 主题组件库；
- 至少包含一个首屏组件、一个章节组件、一个强调/信息组件、一个图片或结尾组件；
- HTML 中不得出现任意脚本、事件属性、外部 stylesheet、未 allowlist URL、危险 CSS 或未转义输入；
- GZH validator 的阻断级 ERROR 为 0，Xiaowan policy 的正文冻结和图片证据检查通过；
- 390px 预览不得出现横向溢出，章节标题、卡片和图片说明不得重叠或截断；
- 与旧 `professional-clean` 结果的结构化组件签名必须不同，且差异不是只改变颜色；
- 至少三篇不同文章类型的 fixture 达到上述指标。

视觉质量不能只由字符串计数判定。必须保存 deterministic 和 skill_driven 的 HTML、桌面截图、390px 截图，并人工观察：首屏层级、章节节奏、卡片密度、移动端断行、图片与文字关系和整体主题一致性。若截图仍然看起来与 `professional-clean` 相同，即使自动化测试通过也不能验收。

## 6. 降级与失败语义

### 6.1 Skill 模式内部失败

以下情况不得写入 `final_html`：

- Skill 资产缺失、hash 不匹配或组件库无法加载；
- Agent 计划 schema 不合法、引用未知组件、遗漏 Markdown 节点或超出装饰预算；
- 组件装配失败、正文/图片校验失败、GZH ERROR 或 Xiaowan 阻断级错误；
- Agent 预算、上下文、超时、取消或连续相同工具失败。

Validator 失败时，允许 Agent 在剩余预算内修正计划并重新装配；修正不得修改批准 Markdown。达到预算后，整个 Skill 模式失败，丢弃本次所有内存候选和工具结果。

### 6.2 外层降级

Skill 模式失败后由外层 `render_html` 进入：

```text
skill_driven
  -> llm_decide
  -> deterministic(HTML_LAYOUT_DEFAULT_THEME)
  -> legacy HtmlRenderer
  -> HTML_LAYOUT_FAILED
```

降级结果必须记录 `fallback_reason` 和来源模式。只要 Skill 模式成功，就不能再用旧 `LayoutEngine` 覆盖它，也不能在成功结果中混入旧主题组件。

## 7. 代码实现阶段计划

### R0：冻结当前行为并建立对照样本

1. 保存现有 deterministic、llm_decide 和当前 skill_driven 输出作为历史对照；
2. 从固定测试数据和真实数据库准备至少三篇结构完整文章，包含标题、引言、三章、列表/引用/数字和图片；
3. 生成组件签名脚本，能够统计主题、组件 ID、章节组件、图片容器和正文可见文本；
4. 明确当前 Skill 实现不得作为新模式的成功基线。

### R1：迁移完整 GZH 组件资产

1. 按来源 commit 将主题索引、六套主题库、通用组件库和 validator 规则放入 `app/skills/rendering/gzh_design/references/`；
2. 将 Markdown/HTML 示例从“模型可自由复制的文本”整理为带 ID、语义、参数 schema、模板和组合约束的 manifest；
3. 将模板中的动态插槽替换为安全占位符，例如 `node_ref`、`image_ref`、`theme_token`，禁止任意 HTML 注入；
4. 保留组件源片段的来源和 hash，建立 `THIRD_PARTY_NOTICES.md` 和迁移对照表；
5. 用本地 Python 代码实现 GZH validator 的阻断规则，不在生产运行时调用上游脚本。

### R2：实现 Skill 组件 Registry 和内存装配器

1. 启动时加载并校验全部主题/组件 manifest，重复 ID、缺模板、路径逃逸和 hash 错误直接 readiness 失败；
2. 实现 `ComponentRegistry` 的主题、组件、语义、参数和组合白名单；
3. 实现 Markdown 节点树和不可变 node ID，所有正文内容从 node reference 读取；
4. 实现首屏、章节、正文、强调、引用、列表、步骤、数据、图片和结尾组件的确定性装配；
5. 实现统一内联样式、`span leaf` 文字包装、URL 安全、HTML escape 和组件嵌套限制；
6. 组件装配只在内存中完成，不写临时 HTML 文件。

### R3：实现 Xiaowan policy

1. 将正文冻结、图片证据、首屏和章节结构变成阻断级检查；
2. 将装饰密度、强调次数、固定宽度、横向溢出和图片说明长度变成可解释的 warning/error；
3. 将 release checklist 与 GZH validator 合并为统一 `SkillValidationReport`，保留来源 policy ID；
4. 每个阻断错误都返回给 Agent 可修正的字段级错误，不把大段 HTML 塞回模型。

### R4：重写 `skill_driven` Agent 适配

1. 新建 `skill_driven/adapter.py`，不导入旧 `LayoutEngine`、旧主题 registry 或 `professional-clean`；
2. 新建富 `SkillLayoutPlan` strict schema 和动态 enum；
3. 注册本文规定的 Skill/tool，并让 Agent 自主决定合法的 Skill 读取、主题、文章配方和组件顺序；
4. 将 `assemble_skill_html` 和 `validate_skill_html` 接入 Agent 循环；
5. 只有通过全部 validator 且组件签名达到最低要求的 candidate 才能完成；
6. Agent 失败由外层降级，不允许 Skill 适配器内部悄悄调用旧代码排版器。

### R5：事件、debug 和前端面板

1. 用户侧继续输出 `agent.activity kind=skill/tool` 和 `response.output_text.delta`；
2. Skill 读取、组件计划、装配、校验和返工各显示一个可更新活动；
3. debug 开启时记录组件 manifest hash、计划、工具参数、校验报告、组件签名和 fallback 原因；
4. debug 关闭时只返回活动摘要、最终 `final_html` 和产品级 artifact，不返回完整组件 HTML、系统提示词或内部输入输出；
5. 面板增加 Skill 组件调用 trace，能够展开看到组件计划和 validator 结果。

## 8. 测试计划

### 8.1 资产和启动校验

- 六套 GZH 主题、通用组件库和 Xiaowan policy 全部能从启动快照读取；
- manifest 重复 ID、组件模板缺失、路径逃逸、超限文件、hash 不匹配必须 fail-closed；
- 运行期间修改源文件不改变本次请求的资产快照；
- readiness 不通过时不能启动可处理用户请求的 Skill 模式。

### 8.2 组件 Registry 和装配器

- 每种基础组件都有模板、输入 schema、输出 HTML 和最小 fixture；
- 未知 component ID、错误 node ID、跨主题组件、非法插槽、任意 HTML/CSS、外部 stylesheet 和危险 URL 必须拒绝；
- 正文 node 顺序、可见文本、数字、图片 URL、位置和 caption 在装配前后保持一致；
- 同一输入、主题和计划重复装配结果 hash 一致；
- 组件签名能识别首屏、章节、强调、图片、结尾等结构。

### 8.3 Agent ReAct

- fake model 可按不同顺序读取 Skill、主题、组件并完成；
- 模型必须能从校验错误中修正组件计划；
- 模型直接返回旧 `theme_id` 计划但没有 section/component 计划时，必须返回 `SKILL_LAYOUT_PLAN_TOO_THIN`；
- 模型试图直接输出 HTML、未知组件或跨主题组件时，不调用装配器；
- 工具超时、预算耗尽、重复失败和取消均丢弃候选，外层收到明确 fallback reason；
- Engine thinking 始终关闭，工具/Skill activity 和解释文本正常输出。

### 8.4 固定数据和截图验收

每次烟测固定读取 `tests/rendering_data/skill_driven/input`，由
`tests/manual/render_skill_article_cases.py` 为三篇文章生成 Skill 与 deterministic 对照：

```text
docs/smoke-artifacts/skill-rendering-case-<case>.html
docs/smoke-artifacts/skill-rendering-case-<case>-desktop.png
docs/smoke-artifacts/skill-rendering-case-<case>-mobile.png
```

至少覆盖：教程/清单、政策或制度解读、文化/人物或观点文章。每篇同时保存 deterministic 对照结果。

此外，必须使用 GZH 上游 `assets/sample-article.md` 或等价的项目固定副本，分别运行全部六套主题。六套主题都要
成功生成 HTML、桌面截图和 390px 截图，并与对应上游 gallery 逐项核对首屏、章节标题、导读、主题强调、列表/引用、
图片和结尾组件的层级。适配后不要求像素级一致，但不能只保留主题颜色或普通标题；任一主题无法体现其组件库特征，
都视为该主题尚未完成迁移。

自动验收：

- 正文可见文本、图片 URL、图片数量、位置和标注完全一致；
- 每篇至少 5 种组件语义、至少 3 种主题专属组件；
- 首屏、目录/导读、章节组件、强调/信息组件、图片/结尾组件满足文章适用性要求；
- GZH ERROR=0，Xiaowan 阻断错误=0，安全校验通过；
- 390px 无横向溢出、无重叠、无截断；
- Skill 与 deterministic 的组件签名显著不同，不允许只改变颜色；
- Skill HTML 不得与 `professional-clean` 结构完全相同。

人工验收：

1. 对比截图，确认首屏、章节节奏、组件层次、图片容器和结尾视觉明显更丰富；
2. 确认页面不是把每段都包成卡片的无序堆砌，仍保持统一主题和留白；
3. 确认手机端正文易读，fancy 效果没有造成横向滚动或遮挡；
4. 至少将一篇结果粘贴到公众号编辑器并在手机预览观察，记录兼容性 warning。

### 8.5 完整业务烟测

- 本地 `.env` 设置 `HTML_LAYOUT_RENDERER=skill_driven`；
- 真实 Ark 完成至少一次组件规划、装配和校验循环；
- 发生一次 validator 返工或受控降级的可重复测试；
- 真实完整工作流审批到最终 HTML，确认 artifact 仍只保存 `final_html`；
- debug 开启/关闭各跑一次，确认 trace 和产品响应边界正确；
- 取消、超时、断线和降级不写入半成品 artifact/checkpoint；
- 将最终 HTML、对照 HTML、截图、组件签名、事件序列和失败案例保存到 `docs/smoke-artifacts/`。

## 9. 降级、性能和安全边界

- Skill 资产全部在启动时加载和 hash，生产请求不联网、不 clone、不安装依赖；
- 所有组件和 policy 是只读固定资产，用户 Markdown 只能进入安全插槽；
- 单次 Agent 继续使用现有最大步骤、工具数、上下文、超时和重复失败预算；
- 组件模板和校验结果保留有界大小，超出上下文时通过摘要和引用传递，不把整套组件库重复注入模型；
- Engine 取消、超时和失败时清空内存候选，不写 artifact；
- Skill 模式不引入 MCP、Node/Bun、Chrome/CDP 或文件工作区；只有后续评估证明必要时才单独更新依赖和部署脚本；
- `llm_decide` 只作为 Skill 失败后的旧主题选择模式，不能被 Skill 模式调用作为内部“成功”路径；
- 所有 fallback 都记录来源模式、错误码和可追踪原因。

## 10. 实现完成判定

只有同时满足以下条件，才可以把本次 `skill_driven` 视为完成：

1. `skill_driven` 代码路径不再导入或调用旧 `LayoutEngine`/八主题 registry；
2. GZH 六套主题组件库和通用组件库已完成固定资产迁移，Xiaowan policy 已经真正参与校验；
3. Agent 输出的是富 `SkillLayoutPlan`，而不是只有主题名的旧计划；
4. 最终 HTML 由固定组件资产装配生成，且组件签名证明 Agent 的布局决策影响了结果；
5. 至少三篇固定文章 fixture 达到组件数量、正文保真、图片一致性、安全和 390px 视觉指标；真实 Ark 文章另有独立端到端烟测；
6. GZH 六套主题均完成 sample article 页面和上游 gallery 组件层级对照；
7. 人工截图观察确认比 `professional-clean` 明显更丰富、更有层次，且不是杂乱堆砌；
8. Skill 失败仍按既定外层降级链交付，不修改原有产品 artifact 契约；
9. 全量回归、真实 Ark 烟测、开发面板验收和 smoke artifacts 均完成。

## 11. 实现结果与烟测索引

本方案已按本文实现。当前代码入口为 `app/rendering/skill_driven/`，运行时 Skill 白名单只有
`gzh_design` 与 `xiaowan_layout`；旧 `app/rendering/wechat_layout/` 只服务
`deterministic`、`llm_decide` 和外层降级，二者没有调用关系。

实现验证记录在 `docs/skill-rendering-smoke-report.md`。其中包含六套主题画廊、三类文章 fixture、
真实 Ark Agent 循环、完整工作流和开发面板回放、`llm_decide` 回归烟测的路径与指标。

实现中实际确认的技术边界：

- 未引入 Node/Bun/Chrome/CDP 或 MCP；Skill 资产全部是启动时校验的 Markdown/reference，HTML 在内存装配；
- Skill Agent 的 HTML 不允许由模型直接书写，模型只提交 strict `SkillLayoutPlan`；
- Skill 失败才允许外层进入 `llm_decide`，不能用旧 renderer 冒充 Skill 成功；
- 低复杂度 fixture 若所有节点都被标记为强调，validator 会拒绝全卡片化计划，要求保留普通正文组件。

## 12. 当前是否需要拍板

本方案不需要新的产品拍板即可开始实现。以下事项已按现有决策确定：

- 只保留通用 Agent Engine，彻底重写 `skill_driven` 排版适配；
- `gzh-design` 是主组件资产，`xiaowan` 是冻结正文、图片证据和移动端验收 policy；
- 成功结果必须显著区别于 `professional-clean`，不能以“读取过 Skill”作为完成标准；
- 仍只返回和持久化原有 `final_html`，不新增产品 artifact 字段；
- Engine 内部不建立 checkpoint、不写临时文件、不使用 MCP；
- 失败遵守 `skill_driven -> llm_decide -> deterministic -> legacy` 外层降级链。

实现中唯一允许的技术性取舍是：根据组件源文件的实际结构，将 HTML 示例规范化为安全模板和参数 schema；这不改变视觉目标或外层业务契约，也不需要再次确认。
