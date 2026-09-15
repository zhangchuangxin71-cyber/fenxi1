# 微信公众号开源 Skill 资产调研与迁移建议

> 调研日期：2026-08-14  
> 目标：筛选可用于当前微信公众号 Agent 的开源资产，形成按现有 LangGraph 节点接入的迁移路线。本文不改变现有图、接口和 artifact 契约，也不包含自动发布能力。

## 1. 当前项目基线

当前主链路已经是单向状态机，并在任务书、大纲、文章三个阶段提供 HITL 审批：

```text
intent_router
  -> session_memory || orchestrator
  -> entry_dispatch
  -> load_document_meta
  -> plan_research_direction
  -> clarification_interrupt
  -> persist_research_direction
  -> route_documents
  -> extract_materials
  -> generate_task_spec -> review_task_spec
  -> generate_outline   -> review_outline
  -> generate_article   -> review_article
  -> generate_images
  -> render_html
  -> END
```

后续业务修改可以由 `orchestrator` 选择合法入口，随后仍按单向状态机向下执行。当前 `render_html` 使用 `MarkdownIt + bleach + 固定 CSS`，输出带 `<style>` 和 `class="wechat-article"` 的完整 HTML；当前代码中没有通用 Skill 注册或选择层。

因此，开源 Skill 的迁移原则是：

1. 现有 Graph、HITL、artifact/version、SSE 和持久化设计继续作为唯一运行时骨架，不能用第三方 Skill 自带的文件工作流替代。
2. 只迁移能提升单个节点质量的提示词规则、结构化契约、主题、确定性算法和测试用例。
3. 同一职责只保留一个主资产；其他资产仅补充主资产缺少的部分，避免把多套互相冲突的提示词同时塞给模型。
4. 第三方脚本不在请求期间临时下载或直接执行。先固定版本、审计许可证和依赖，再改造成项目内受控实现。
5. 用户已批准的上游 artifact、事实证据和本项目节点输出契约，高于任何 Skill 的风格偏好。

## 2. 调研快照与总体结论

| 来源 | 调研版本 | 许可证 | 对本项目最有价值的资产 | 结论 |
|---|---|---|---|---|
| [aiworkskills/wechat-article-skills](https://github.com/aiworkskills/wechat-article-skills/tree/c83831a14a1f708a1a3136ebe75378d2b782f339/skills) | `c83831a` | Apache-2.0 | 中文公众号写作检查、配图 Type × Style、提示词模板、4 套简单排版主题 | 作为中文场景补充资产，不采用其整套编排和文件配置体系 |
| [imraywang/wewrite](https://github.com/imraywang/wewrite/tree/b998de801bf13cb251caa2a4c529f99cdfdd6537) | `b998de8` | MIT | brief/claims 设计、证据约束、编辑审稿、通用配图策略、微信 HTML 转换器、主题和校验器 | 各重复职责中的主要参考；排版第一阶段的主资产 |
| [Ian Xiaohei Illustrations](https://github.com/helloianneo/ian-xiaohei-illustrations/tree/91b560849e8f883922cc2fa8a358a668caa94105/ian-xiaohei-illustrations) | `91b5608` | MIT + NOTICE 署名要求 | 认知锚点选图、原创隐喻、16:9 手绘提示词、图像 QA | 只作为可选正文插画风格，不作为所有文章默认风格 |
| [wechat-writing-style](https://github.com/yaoleifly/wechat-writing-style/blob/cb1f138ed12850e01b50bc387a1c8581bc3d1f38/SKILL.md) | `cb1f138` | MIT | 结论先行、短段落、高信息密度、中文排版和特定领域合规提醒 | 作为可选写作 persona；不能作为全局公众号写作规则 |
| [baoyu-post-to-wechat](https://github.com/JimLiu/baoyu-skills/tree/6b7a2e417500561a5ecdd0b168332f4142584617/skills/baoyu-post-to-wechat) | `6b7a2e4` | MIT | Markdown 转换入口、外链脚注、主题覆盖规则、图片占位处理 | 只用于排版设计交叉验证；不引入其发布、浏览器和 API 代码 |
| [gzh-design-skill](https://github.com/isjiamu/gzh-design-skill/tree/ba1f4175519b481cb3566616c9e5178705067904) | `ba1f417` | AGPL-3.0-or-later | 6 套公众号内联 HTML 组件库、`span leaf` 规则、产物与组件校验器 | 提取兼容规则和组件设计；不是可直接嵌入的独立 renderer，复制资产前需许可证审查 |
| [xiaowan-wechat-layout-lite](https://github.com/cyberxiaowan/xiaowan-wechat-layout-skill/tree/5a96543a5f47c0f82f74e4ce5041fbe6f6b57c45) | `5a96543` | AGPL-3.0-or-later | 冻结正文、手机端视觉脚本、图片证据、真实粘贴检查和反馈路由 | 作为移动端验收与工作流补充；自身不含 renderer，明确依赖 `gzh-design` |

总体选择：

- 任务书、文章和审稿质量以 **WeWrite** 为主。
- 中文表达层以 **WeWrite 的事实约束**为底，按需叠加 `wechat-writing-style` 或 aiworkskills 的非冲突规则。
- 通用配图策略以 **WeWrite** 为主，图像类型和提示词结构由 aiworkskills 补充；Ian 小黑只作为一个显式可选的视觉 preset。
- 第一阶段排版以 **WeWrite converter/theme/validator** 和 **aiworkskills 的纯本地 Python 转换行为**为代码主参考；
  `gzh-design` 补充公众号组件、`span leaf` 和双层校验规则，`xiaowan` 补充手机端验收与反馈纪律，Baoyu
  补充脚注和图片占位行为。不把任何一套通用 Agent 工作流原样作为生产 renderer。

## 3. 按现有节点顺序的资产迁移图

### 3.1 `intent_router` / `session_memory` / `orchestrator` / `entry_dispatch`

这些节点不应引入第三方文章生成编排器。第三方 Skill 中“选题 -> 写作 -> 审稿 -> 配图 -> 排版”的流程，和本项目现有 Graph 的 HITL、断点恢复、artifact revision 语义不同。

可借鉴但无需进入模型提示词的只有两点：

- 各阶段必须读已批准的上游 artifact，不能绕过审批直接产出下游结果。
- 写作、配图、排版是可独立重做的业务能力，不应把“排版”隐式解释成“重新写文章”。

未来 Skill 注册层只向这些节点暴露已启用 Skill 的**元数据**，不把完整 `SKILL.md` 注入路由模型。`orchestrator` 仍然只决定入口阶段，不负责每一步选 Skill。

### 3.2 `load_document_meta` -> `plan_research_direction` -> `clarification_interrupt`

有用资产：

- [WeWrite article brief](https://github.com/imraywang/wewrite/blob/b998de801bf13cb251caa2a4c529f99cdfdd6537/skills/wewrite-write/references/article-brief.md) 中的目标读者、阅读情境、真正问题、读后行动、核心判断、适用边界、最强反方等字段。
- [aiworkskills topics](https://github.com/aiworkskills/wechat-article-skills/blob/c83831a14a1f708a1a3136ebe75378d2b782f339/skills/aws-wechat-article-topics/SKILL.md) 中“明确主题/粗略方向/无方向/系列文章”四类输入识别，以及一次性给出主题建议卡的做法。

建议迁移：

- 用 brief 的关键字段检查用户需求是否足够明确，帮助 `plan_research_direction` 一次性列全待确认事项。
- clarification payload 中继续给出完整推荐方案，而不是把一个简单主题拆成多轮问答。

不迁移：热点搜索、SEO 选题池和从零寻找选题。本项目以用户文档为依据，这些能力会扩大事实来源边界并与检索服务职责冲突。

### 3.3 `route_documents` -> `extract_materials`

主资产：[WeWrite write](https://github.com/imraywang/wewrite/blob/b998de801bf13cb251caa2a4c529f99cdfdd6537/skills/wewrite-write/SKILL.md)。

建议吸收以下证据纪律：

- 区分事实、推断、意见和用户提供的经历；模型记忆不能被当成已验证来源。
- 数字、日期、引述、研究结论和时效性事实必须能回到本轮提取的文档证据。
- 不受材料支持的主张应删除、缩小表述或明确标成推断，不能让模型补洞。
- 第一人称经历、朋友同事、采访、对话和场景细节只能来自用户文档，不能为了“有故事感”而虚构。

第一版不必照搬 WeWrite 的 `sources.yaml/claims.yaml` 文件系统，也不必新增一套业务表。可以把这些约束编入现有素材提取结构和提示词；若未来需要更强事实追踪，再在素材 JSON 内增加轻量 `claim_type/source_material_ids/support_status`。

### 3.4 `generate_task_spec` -> `review_task_spec`

主资产仍为 WeWrite article brief。建议逐步补齐以下任务书维度：

- `audience`：读者是谁、处于什么情境、真正要解决什么问题。
- `goal`：读完应获得什么判断，以及能采取什么行动。
- `thesis`：核心判断、新增价值、适用边界和最强反方。
- `personal_materials`：用户材料中是否真的包含可用亲历信息。
- `constraints`：篇幅、语气、标题、禁用项、必须覆盖项。

`review_task_spec` 继续使用本项目统一 HITL 卡片。Skill 只提升候选任务书质量，不新增第三方审批状态。

### 3.5 `generate_outline` -> `review_outline`

主资产：[WeWrite framework guide](https://github.com/imraywang/wewrite/blob/b998de801bf13cb251caa2a4c529f99cdfdd6537/skills/wewrite-write/references/frameworks-quick.md)。补充资产：[aiworkskills writing](https://github.com/aiworkskills/wechat-article-skills/blob/c83831a14a1f708a1a3136ebe75378d2b782f339/skills/aws-wechat-article-writing/SKILL.md)。

建议迁移：

- 每一节都要服务于任务书中的读者问题、核心判断或读后行动，避免只有形式完整的“总分总”。
- 大纲中明确每节目的、需要使用的素材和它推动的主张；章节 ID 仍按本项目约定从 Markdown 确定性提取。
- 从 aiworkskills 只补充开头、转折、结尾和节奏的候选模式，不强制所有文章套同一结构。

`review_outline` 仍由用户 approve/revise/regenerate。Skill 不应自动越过审批，也不应把审稿模型的自评分替代用户决定。

### 3.6 `generate_article` -> `review_article`

#### 写作主规则

以 [WeWrite write](https://github.com/imraywang/wewrite/blob/b998de801bf13cb251caa2a4c529f99cdfdd6537/skills/wewrite-write/SKILL.md) 为主：先服从批准的任务书、大纲和素材证据，再处理文风。尤其保留“不虚构个人经历”和“不用模型记忆补事实”的硬约束。

#### 可选文风 preset

[wechat-writing-style](https://github.com/yaoleifly/wechat-writing-style/blob/cb1f138ed12850e01b50bc387a1c8581bc3d1f38/SKILL.md) 适合 AI 工具、跨境科技、金融基础设施等特定账号风格，可抽取为 `conclusion_first_conversational` preset：

- 前三句话给出核心判断。
- 短句、1-3 句短段落、口语化但每段有信息。
- 中英文、中文与数字之间留空格。
- 避免模板化转折、书面套话和过度规整的节奏。

以下内容不能作为全局默认：第一人称亲历、特定口头禅、固定互动结尾、金融免责声明、禁用所有破折号、强制生成 5-10 个标题。它们只应在文章类型、账号风格或用户要求匹配时启用；第一人称内容仍必须有素材证据。

#### 审稿主规则

以 [WeWrite review](https://github.com/imraywang/wewrite/blob/b998de801bf13cb251caa2a4c529f99cdfdd6537/skills/wewrite-review/SKILL.md) 和 [内容质量标准](https://github.com/imraywang/wewrite/blob/b998de801bf13cb251caa2a4c529f99cdfdd6537/docs/content-quality-rubric.md) 为主：

- 从准确、观点、有用、合声、好读五个维度检查。
- 虚构作者经历、核心事实无证据、结论超出证据、没有明确读者交付属于阻断问题。
- 语言“像不像 AI”只用于定位套话、碎句和节奏过齐，不应成为硬性发布分数。

[aiworkskills review](https://github.com/aiworkskills/wechat-article-skills/blob/c83831a14a1f708a1a3136ebe75378d2b782f339/skills/aws-wechat-article-review/SKILL.md) 的敏感词、错别字、禁用词和 AI 味指纹可作为第二层中文检查，但需去重，不能因同一问题在“写作规范”和“AI 味”两处重复扣分。

当前 Graph 没有独立机器审稿节点。第一阶段可先把高价值规则放入生成提示词和人工审批卡的检查提示；以后若数据证明有必要，再在 `review_article` 前增加确定性检查或编辑模型，不应在本轮排版优化中顺带改图。

### 3.7 `generate_images`

#### 通用策略：WeWrite 为主

[WeWrite visual](https://github.com/imraywang/wewrite/blob/b998de801bf13cb251caa2a4c529f99cdfdd6537/skills/wewrite-visual/SKILL.md) 最适合作为通用策略：

- 先提炼具体实体、主情绪、统一色板、构图和质感。
- 正文只在确实能解释概念或缓冲阅读的位置配图，不按固定数量凑图。
- 限制最大图片数和费用；失败只重试明显失败的单张。
- 校验 URL/文件可读、格式、核心实体可辨和整组风格一致。
- 封面与正文图职责分离；当前项目只需要正文图片 URL、标注和插入位置时，不应额外引入封面发布流程。

#### 类型与提示词：aiworkskills 补充

[aiworkskills image type selection](https://github.com/aiworkskills/wechat-article-skills/blob/c83831a14a1f708a1a3136ebe75378d2b782f339/skills/aws-wechat-article-images/references/image-styles/auto-selection.md) 和 [prompt construction](https://github.com/aiworkskills/wechat-article-skills/blob/c83831a14a1f708a1a3136ebe75378d2b782f339/skills/aws-wechat-article-images/references/image-styles/prompt-construction.md) 可补充：

- `concept/process/whiteboard/data-viz/comparison/architecture/mindmap/timeline/checklist/quote-card/scene/atmosphere` 的图像类型表。
- prompt 明确布局、具体内容、视觉关系、语义颜色、风格和比例。
- 若必须有图中文字，prompt 应直接给出短中文原文；更稳妥的默认仍是少字或无字，避免生图模型产生错字。

#### 可选风格：Ian 小黑

[Ian 小黑 Skill](https://github.com/helloianneo/ian-xiaohei-illustrations/blob/91b560849e8f883922cc2fa8a358a668caa94105/ian-xiaohei-illustrations/SKILL.md) 的高价值部分是：

- 先找“认知锚点”，而不是平均配图。
- 一张图只解释一个判断、结构、状态或隐喻。
- 把抽象概念变成物理动作和低科技物件，再让核心角色真正参与动作。
- 16:9、留白、少量颜色、少量短标注，以及清晰的生成后 QA 和单图重试规则。

它是强辨识度 IP，不适合金融报告、政务、严肃企业稿等所有场景。建议注册为用户或任务书显式选择的 `xiaohei-handdrawn` preset，不做自动默认。若复制或改编其提示词、示例或角色设定，须遵守仓库 [LICENSE](https://github.com/helloianneo/ian-xiaohei-illustrations/blob/91b560849e8f883922cc2fa8a358a668caa94105/LICENSE) 和 [NOTICE](https://github.com/helloianneo/ian-xiaohei-illustrations/blob/91b560849e8f883922cc2fa8a358a668caa94105/NOTICE.md) 的署名要求；示例图只用于低频校准，不进入默认 prompt 上下文，也不复刻构图。

### 3.8 `render_html`

这是第一阶段唯一建议实际改造的业务节点。

#### 主资产：WeWrite

[WeWrite converter](https://github.com/imraywang/wewrite/blob/b998de801bf13cb251caa2a4c529f99cdfdd6537/src/wewrite/toolkit/converter.py)、[theme loader](https://github.com/imraywang/wewrite/blob/b998de801bf13cb251caa2a4c529f99cdfdd6537/src/wewrite/toolkit/theme.py)、[HTML validator](https://github.com/imraywang/wewrite/blob/b998de801bf13cb251caa2a4c529f99cdfdd6537/src/wewrite/commands/validate_html.py) 和 [converter tests](https://github.com/imraywang/wewrite/blob/b998de801bf13cb251caa2a4c529f99cdfdd6537/tests/test_converter.py) 提供了最完整的闭环：

- Markdown 转 body-only HTML，主题 CSS 确定性内联。
- H1 作为公众号标题单独提取，正文不重复标题。
- 中英文间距、中文标点移出粗体、代码块、图片响应式处理。
- 原生列表转换为微信编辑器更稳定的 section 结构。
- 普通外链转换为文末脚注。
- 清除 `div/class/id`，注入暗色模式属性，并提供粘贴路径的 `leaf` 加固。
- 对 `<style>/<script>/<link>`、不兼容定位、grid、CSS 变量、外部字体等做 ERROR 级校验。
- 浏览器 preview wrapper 与真正用于微信的 body HTML 分离。

其主题库有 18 套，第一阶段不应全量接入。建议只挑 4-5 套白底、移动端稳定且覆盖主要文章类型的主题做人工和快照测试：

| 建议主题 | 场景 | 第一阶段地位 |
|---|---|---|
| `professional-clean` | 企业、科技、通用 | 默认 |
| `bold-navy` | 商业、金融、行业分析 | 可选 |
| `minimal` | 观点、学术、内容优先 | 可选 |
| `warm-editorial` | 文化、生活方式、人物 | 可选 |
| `newspaper` | 深度报道、评论 | 候选，需先验证底色和字体在微信中的表现 |

WeWrite converter 文件声明它源自另一个 `wechat_article_skills` 实现。若未来复制其代码而非参考设计重写，必须在接入前继续核对来源链并保留所有适用许可证和修改声明。

#### 补充资产：aiworkskills、Baoyu、gzh-design 与 xiaowan

[aiworkskills formatting](https://github.com/aiworkskills/wechat-article-skills/blob/c83831a14a1f708a1a3136ebe75378d2b782f339/skills/aws-wechat-article-formatting/SKILL.md) 可补充“显式用户主题 > 本文主题 > 默认主题”的覆盖顺序，以及小型 YAML 主题契约。它的实现和主题数量更简单，不应与 WeWrite renderer 并行维护。

[Baoyu Markdown conversion entry](https://github.com/JimLiu/baoyu-skills/blob/6b7a2e417500561a5ecdd0b168332f4142584617/skills/baoyu-post-to-wechat/scripts/md-to-wechat.ts) 证明了外链脚注默认开启、图片先变占位符再解析、主题和主色可覆盖等做法。该入口实际依赖 `baoyu-md` 与 Chrome/CDP 相关包，和当前 Python 服务的技术栈不一致，只作为测试用例和行为交叉验证，不引入运行时依赖。

新增调研表明，`gzh-design` 的 6 套主题是供通用 Agent 读取和装配的 HTML 组件库，不是一个独立
Markdown renderer；`xiaowan-wechat-layout-lite` 则明确依赖 `gzh-design`，自身只提供移动端排版、真实
粘贴检查和反馈复盘流程。两者的详细结论、固定提交和许可证边界见第 8 节。

## 4. 重复职责的最终取舍

| 职责 | 主资产 | 只补充什么 | 明确不做什么 |
|---|---|---|---|
| 全流程编排 | 当前 LangGraph | 第三方的阶段边界观念 | 不运行第三方 main skill，不建立第二套 run 状态 |
| 任务书与证据 | WeWrite brief/write | aiworkskills 的中文主题建议卡 | 不引入热点搜索和磁盘 artifact 体系 |
| 大纲与正文 | WeWrite write | aiworkskills 的结构候选；wechat-writing-style 的可选 persona | 不把任何单一“爆款风”设为全局风格 |
| 审稿 | WeWrite review/rubric | aiworkskills 的中文错误、敏感项、AI 味诊断 | 不让 AI 味分数覆盖事实准确性和用户审批 |
| 配图 | WeWrite visual | aiworkskills 的 Type × Style；Ian 的可选手绘 preset | 不固定凑图，不把小黑设为默认，不复制案例构图 |
| 排版 | WeWrite/aiworkskills 的确定性 Python 转换结构 | gzh-design 的内联组件与校验；xiaowan 的手机验收；Baoyu 的脚注与占位行为 | 不维护多套 runtime renderer，不接发布 API/浏览器自动化，不让模型自由生成整篇 HTML |

## 5. 建议的 Skill 注册与运行机制

可以兼容 [Agent Skills specification](https://agentskills.io/specification) 的标准目录结构，但不应把“Agent Skills 目录”直接等同于“可执行 LangGraph 节点”。标准主要解决发现、元数据和渐进加载；节点输入输出、权限和错误处理仍由本项目定义。

建议目录形态：

```text
app/skills/
  registry.yaml
  wechat-layout/
    SKILL.md
    references/
      compatibility.md
    assets/
      themes/*.yaml
    prompts/
      layout-plan-system.md
  wechat-writing-evidence/
    SKILL.md
    prompts/
    references/
  wechat-image-generic/
    SKILL.md
    prompts/
    references/
  xiaohei-handdrawn/
    SKILL.md
    prompts/
    NOTICE.md
```

`SKILL.md` 使用标准 `name/description/license/compatibility/metadata`，项目扩展字段放在 `metadata` 或独立 `registry.yaml`：

```yaml
skills:
  - id: wechat-layout
    version: "1"
    source:
      repository: https://github.com/imraywang/wewrite
      commit: b998de801bf13cb251caa2a4c529f99cdfdd6537
    license: MIT
    nodes: [render_html]
    capabilities: [prompt-fragment, theme, renderer-policy, validator-policy]
    default_enabled: true
    priority: 100
```

实现约束：

- 启动时校验 registry、许可证文件、资源路径和内容哈希；失败则阻止启用该 Skill，而不是在请求时联网修复。
- 节点使用 allowlist 精确加载所需片段。例如 `render_html` 只会拿到排版规则和主题，不会读写作、发布或账号凭证说明。
- `scripts/` 默认为不可执行。真正需要的转换算法进入本项目模块、依赖锁和测试体系，不用 shell 调第三方脚本。
- Prompt 组装优先级固定为：系统安全/事实约束 > 节点结构化输出契约 > 已批准 artifact > 用户本轮要求与 session memory > 启用的风格 Skill。
- 在 artifact 或 debug trace 中记录 `skill_id/version/source_commit/theme_id`，以便复现和 A/B 对比；不把完整第三方 prompt 返回给产品前端。
- 升级 Skill 必须显式改版本并跑节点级质量集，不能跟随 GitHub `main` 自动漂移。
- 第三方许可证与 NOTICE 集中进入项目的 third-party notices。Apache-2.0 资产的修改文件保留变更说明；MIT 资产保留版权和许可文本。

这种机制的价值是统一管理“知识资产”，而不是让模型自由发现和执行任意 Skill。第一阶段只注册 `wechat-layout` 即可，不必为了未来节点一次性建完所有适配器。

## 6. 第一阶段：AI 排版节点接入建议

### 6.1 先解决兼容性，再增加“AI 选择”

当前 renderer 的固定 CSS 对浏览器预览有效，但最终正文依赖 `<style>` 和 `class`。WeWrite 的兼容模型会将这两项判为 ERROR，因为微信编辑器可能过滤 class 或 style tag。这里必须区分两种 HTML：

- `wechat_body_html`：可粘贴/可提交的正文片段，所有必要样式内联，不含页面级 `<style>`、`class/id` 和脚本。
- `preview_html`：供开发面板或前端 iframe 预览的完整 HTML 文档，可以有只服务浏览器预览的 wrapper，但 body 内容必须与交付正文一致。

为避免第一阶段破坏现有前端契约，可以继续让现有 `final_html` 返回完整预览文档，同时在 artifact 中**增量增加** body-only 结果；若暂时不能扩字段，至少应让 `final_html` 的正文节点本身完全内联，并明确前端提取 `<body>`/`<article>` 内容的规则。最终接口形态需要在实现前与前端做一次小型 contract test。

上述兼容性结论来自这些开源实现的真实约束和测试，不应只靠浏览器截图验收；上线前还需要在实际微信公众号编辑器的复制粘贴或目标上传链路中验证一次。

### 6.2 保持 Graph 不变，改造 `render_html` 内部流水线

第一阶段不需要拆新 LangGraph 节点，也不需要增加 HITL。`render_html` 内部建议按以下顺序执行：

```text
加载已批准 Markdown、图片 URL/标注/插入位置
  -> 选择 allowlist 主题
  -> Markdown AST/HTML 结构化转换
  -> 按 heading_path + paragraph_ordinal 插入图片与图注
  -> 中文排版修正、列表/外链/代码块处理
  -> 主题样式内联
  -> HTML 安全清洗
  -> 微信兼容性校验
  -> 成功：保存 body HTML + preview HTML
  -> 失败：使用最小安全内联主题重渲染并再次校验
```

图片仍只使用 Seedream URL、插入位置和标注，不下载、不上传 OSS，也不引入发布逻辑。

### 6.3 AI 只输出严格的 layout plan，不输出 HTML/CSS

“AI 排版”不应让模型直接生成不可验证的整篇 HTML。基础兼容 renderer 和主题注册稳定后，可在 `render_html` 内增加一个可配置、关闭思考的 Ark 调用，使用 strict schema 输出有限布局计划：

```json
{
  "theme_id": "professional-clean",
  "density": "comfortable",
  "accent": "theme-default",
  "section_variants": [
    {
      "heading_path": ["为什么需要调整"],
      "variant": "key-point"
    }
  ],
  "explanation": "文章属于企业技术解读，采用清晰、克制的专业主题。"
}
```

约束：

- `theme_id/accent/variant` 必须是动态 enum allowlist，不能让模型返回任意 CSS、标签或 URL。
- `heading_path` 必须能在批准的 Markdown 中确定性匹配；匹配失败就忽略该装饰项。
- 模型只能分类和选择展示方式，不能改写正文、改变图片 URL、添加事实或生成发布代码。
- `explanation` 可转换成 `agent.activity` 的用户可见说明；原始 strict JSON 只进入 debug trace。
- AI 规划失败、超时或 schema 不合法时，直接用确定性默认主题，不能阻塞已经批准的文章交付。

建议分两步上线：

1. **P0：兼容 renderer + 小型主题库 + validator + fallback**。先保证可交付、可复现。
2. **P1：受限 layout plan**。在 feature flag 下做 A/B，证明能提升主观质量后再默认开启。

### 6.4 第一阶段明确不迁移的能力

- 不接入公众号登录、草稿箱、发布、浏览器自动化或账号配置。
- 不引入 Baoyu 的 Bun/Chrome/CDP 运行时。
- 不让排版节点重新生成正文或图片。
- 不一次性开放 18 套主题，不开放用户上传任意 CSS。
- 不新增排版 HITL；主题修改可先按“完成后业务修改”的既有路径处理。未来若排版反馈量足够大，再考虑让 orchestrator 支持 `html_layout` 作为合法入口。
- 不默认追加第三方实现中的 AIGC 声明、CTA、免责声明或公众号标题。这些属于产品/合规契约，需单独确认，不能由主题暗中改正文。

### 6.5 验收与质量度量

第一阶段至少建立以下固定样例：多级标题、短段与长段、粗体中文标点、中英文混排、引用、无序/有序列表、代码块、外链、表格、包含与不包含图片、恶意 HTML/危险 URL。

自动验收：

- 交付 body 中无 `style/script/link/div/class/id` 等阻断项，无不支持定位、grid、CSS 变量和外部字体。
- unsafe URL、事件属性和未许可标签被清理；图片 URL 仅允许约定协议。
- 图片数、图注、顺序和插入位置与 image artifact 一致。
- 相同 Markdown、图片、theme/version 产生相同 HTML。
- renderer 主路径失败时，最小安全主题能产生通过 validator 的无损正文；兜底也失败则正确报错，不能返回半成品。
- 每套启用主题通过 golden snapshot 和结构断言；主题升级显式更新快照。

人工验收：

- 开发面板桌面/移动预览无溢出、遮挡和不可读文本。
- 实际微信编辑器复制粘贴或目标上传链路中，标题、段落、列表、图片、图注、外链脚注和暗色模式没有明显丢样式。
- 用同一组文章对比当前固定主题与候选主题，从可读性、层级、节奏、内容匹配、微信稳定性五项盲评。
- 不以“装饰更多”作为质量指标；正文内容完整和平台稳定性优先。

## 7. 推荐迁移顺序

1. 保存本调研所列 commit、LICENSE/NOTICE 和来源说明，建立最小 `wechat-layout` registry 条目。
2. 从 WeWrite 提炼 body-only、inline style、sanitize、validator 和 preview 分离的行为测试，不先复制全部实现。
3. 先实现 `professional-clean/minimal` 两套主题做竖切，再按第 10.2 节扩展到首批 6 套；逐套做快照和实际微信验证。
4. 在不改变 Graph 拓扑的前提下替换 `render_html` 内部流水线，保留现有最小安全主题 fallback。
5. 与前端确认 body-only/preview HTML 的增量接口契约，并验证最终 HTML 与开发面板渲染。
6. P0 稳定后再接 strict `layout_plan`，通过 feature flag 和 A/B 数据决定是否默认开启。
7. 排版阶段完成后，再按本文节点顺序依次评估任务书、文章审稿和配图 Skill；每次只改一个质量变量。

## 8. 2026-08-21 新增排版 Skill 调研

本节根据固定源码提交补充调研，避免把“声明适用于微信公众号”误解为“已经提供可直接嵌入本项目的
Python Markdown renderer”。本次源码暂存于开发机临时目录，仅作为调研证据，不作为运行时依赖：

```text
/tmp/wechat-skills-investigation.dRJ7av/gzh-design-skill
  isjiamu/gzh-design-skill @ ba1f4175519b481cb3566616c9e5178705067904
/tmp/wechat-skills-investigation.dRJ7av/xiaowan-wechat-layout-skill
  cyberxiaowan/xiaowan-wechat-layout-skill @ 5a96543a5f47c0f82f74e4ce5041fbe6f6b57c45
```

### 8.1 微信 HTML 兼容性的准确含义

“微信公众号使用邮件 HTML”是一个便于理解但不够准确的说法。它不是完整意义上的邮件标准；更准确的
描述是：公众号正文是经过平台清洗、改写和粘贴处理的 HTML 片段，不能假定浏览器支持的现代网页 CSS
都会保留。

目前可观察到的共同约束包括：

- 交付正文应是 body-only 片段，不应依赖页面级 `<style>`、外部 `<link>` 或脚本。
- 关键样式应内联到元素；`class`/`id` 不能作为样式唯一载体。
- `<div>`、`position: fixed/absolute/sticky`、`float`、`grid`、CSS 变量、外部字体和动画等特性存在
  被过滤或在编辑器中不稳定的风险。
- 公众号编辑器对文字节点的粘贴行为存在特殊性。`gzh-design` 额外要求用 `<span leaf="">` 包裹正文
  文字，以降低粘贴后样式丢失的概率。
- 浏览器预览可以使用完整 `<!doctype html>`、`<head>` 和预览专用样式，但真正交付的
  `wechat_body_html` 必须独立通过兼容性校验。

因此，本项目不应把“现代 HTML”与“公众号 HTML”当成同一产物。当前 `HtmlRenderer` 输出的完整预览文档
适合浏览器，但其中的 `<style>` 和 `class="wechat-article"` 不满足新 Skill 的严格正文校验；迁移时应
区分 `preview_html` 与 `wechat_body_html`。这不是说公众号 HTML 等同于邮件 HTML，而是二者都倾向于
使用平台清洗后仍能保留的内联、简单布局和保守 CSS。

### 8.2 `gzh-design-skill`：组件库加校验器，不是独立 renderer

`gzh-design-skill@ba1f417` 的 `SKILL.md` 将自身定义为“把 Markdown 转为可直接粘贴的 HTML”，但实现形态
是 Agent Skill：

- `references/theme-index.md` 注册 6 个主题：摸鱼绿、红白色系、石墨极简、留白禅意、摸鱼票据风、橄榄手记。
- 6 个主题组件库合计约 4,280 行 Markdown/HTML 示例，另有约 190 行通用代码块、图片和标签组件。
- Agent 需要读取主题组件库、判断文章类型、选择组件配方，然后把原始 Markdown 逐段装配成 HTML。
- `scripts/validate_gzh_html.py` 是确定性校验器，检查 `<style>`、`<div>`、`class/id`、复杂 CSS、文字节点
  是否被 `span leaf` 包裹等问题。
- `scripts/component_lint.py` 校验主题源文件中的反模式；`wrap_preview.py` 只负责生成带复制按钮的预览外壳。
- 仓库没有一个将 Markdown 和图片输入后直接返回最终 HTML 的独立 Python renderer。把 `SKILL.md` 当作
  系统提示词只能得到部分规则，不能替代组件库读取、组件装配、校验和返工循环。

它的核心工作模式是：

```text
支持 Skill 的 Agent
  -> 读取 theme-index 与选定主题组件库
  -> 解析 Markdown 结构和文章类型
  -> 按主题组件配方装配 HTML
  -> 运行 validate_gzh_html.py
  -> 发现 ERROR/WARNING 后返工
  -> 输出 body-only HTML 和预览页
```

因此它是“Agent 驱动的模板/组件装配 + 确定性校验”，不是纯提示词，也不是现成黑盒转换库。

### 8.3 `xiaowan-wechat-layout-lite`：流程增强层，不含排版引擎

`xiaowan-wechat-layout-lite@5a96543` 的 `SKILL.md` 明确声明：它是 `gzh-design` 的公开增强层，使用前
需要另行安装 `gzh-design`，仓库自身没有转换脚本、主题文件或 HTML renderer。它增加的是：

- `BRIEFED -> FROZEN -> ASSETS_READY -> STRUCTURED -> HTML_READY -> PASTE_READY -> LEARNED` 阶段模型。
- 冻结正文、建立段落清单和图片证据表，防止排版时擅自删改正文。
- 首屏、大章节、装饰预算、手机 375--390px 断行检查。
- 真实公众号粘贴和手机预览检查。
- 将用户反馈路由到层级、间距、装饰、文字、强调、图片或平台兼容层，避免每次全量重做。

它对本项目有价值的是流程规则和验收标准，不能被当作 renderer。其 `AGPL-3.0-or-later` 许可证与
`gzh-design` 的 AGPL 许可证都需要在复制代码、修改代码或作为服务分发前做许可证审查；第一阶段优先
参考行为并在项目内独立重写，不能直接复制整套组件库而忽略许可证和 NOTICE。

### 8.4 与已有排版资产的对比

| 资产 | 是否有独立转换代码 | 主题/组件形态 | Agent 参与程度 | 适合当前项目的迁移方式 |
|---|---:|---|---|---|
| `gzh-design` | 没有完整独立 renderer；有校验器和预览脚本 | 6 套数百行 HTML 组件库 | 高，Agent 负责选择和装配 | 提取兼容规则、组件结构和主题变量，项目内重写 renderer |
| `xiaowan-lite` | 无 | 流程、移动端审美、反馈清单 | 高，依赖通用 Agent 执行流程 | 提取冻结正文、图片证据、手机验收和局部反馈规则 |
| `aiworkskills formatting` | 有纯本地 `format.py` | 4 套 YAML 主题：default/grace/modern/simple | 低，Agent 主要选主题并调用脚本 | 最容易作为代码 renderer 参考；需审查依赖和输出契约 |
| `WeWrite` | 有 Python converter/theme/validator | 18 套 YAML 主题 | 低到中 | 作为 Python 结构和主题加载主参考，抽取所需主题 |
| `Baoyu` | 有 TypeScript/Bun 转换与发布脚本 | 4 套发布主题，并处理图片/脚注 | 中到高 | 只参考脚注、图片占位和主题覆盖；不引入 Bun/Chrome/发布流程 |

这也说明“宣称为微信公众号设计”只证明作者针对平台做了规则取舍，不能证明其代码可直接嵌入本项目，
更不能证明多个 Skill 的输入、输出和许可证可以无缝混合。

## 9. 轻量 Skill Agent Engine 可行性评估

### 9.1 Skill 是否依赖 Codex/Claude Code 的全部能力

排版 Skill 通常依赖通用 Agent 的以下基础能力：

1. 读取 `SKILL.md` 和按需读取 references/assets 的渐进式上下文加载。
2. 读取 Markdown、图片 artifact 和主题注册信息。
3. 调用确定性脚本或工具，并读取工具结果。
4. 根据工具报错进行有限返工，而不是无限循环。
5. 输出面向用户的解释文本和结构化最终结果。

它们不必依赖 Codex 的完整代码编辑、复杂多代理、长期记忆或自主浏览器能力。对当前排版场景，有限的
ReAct 循环在理论上足够：

```text
加载已批准输入和 Skill allowlist
  -> 选择/读取主题规则
  -> 调用 deterministic renderer
  -> 调用 validator
  -> validator 有问题：按有限预算修正布局计划或 renderer 参数
  -> 通过：返回 HTML
```

但不能把“简单 ReAct”理解成让模型自由读写文件、自由执行 shell。最低限度仍需要：工具白名单、参数
schema、步骤上限、超时、取消传播、artifact 不可变约束、SSE activity/tool/skill 事件、debug trace、
失败 fallback 和重复调用保护。

### 9.2 对当前项目的建议

第一阶段不建议马上实现通用黑盒 Skill Agent Engine，也不建议把 `gzh-design` 当作运行时黑盒。先实现
一个可复用但受限的 `SkillRuntime`：

```text
SkillRegistry
  -> 根据 node=render_html 和 allowlist 加载 skill 元数据/主题/规则
SkillContext
  -> 只读批准 Markdown、图片 URL/标注/位置和布局配置
SkillTools
  -> render_markdown、validate_html、select_theme、build_preview
SkillRunner
  -> 最多 N 步、每步有 schema/超时/取消、产出 activity 和 trace
```

`SkillRunner` 可以先不让 LLM 参与，只执行确定性步骤；以后文章节点接入写作 Skill 时，仍可复用相同的
Registry、Context、Tool、事件和预算接口，再替换节点自己的循环策略。这样保留了未来 ReAct 化的扩展能力，
同时避免第一版复制一个通用 Agent 的全部复杂性。

### 9.3 何时才需要完整通用 Agent CLI

只有在 Skill 明确要求以下能力时，轻量 engine 才可能不够：

- 自主修改多个项目文件并运行未知命令；
- 多轮浏览器交互、登录态和人工确认；
- 动态发现并安装新的 Skill/依赖；
- 长期跨任务记忆和多代理协作；
- 复杂任务中任意组合几十种工具。

当前两个新增排版 Skill 都不需要这些能力。`gzh-design` 的“Agent 参与”主要是读取组件库、选择组件、调用
校验器并返工；`xiaowan` 的“学习”主要是把人工反馈转成规则，不是模型训练或复杂长期记忆。因此无需引入
完整 Codex/Claude Code CLI 作为生产 engine。

## 10. 第一阶段多主题确定性排版器方案

### 10.1 目标与边界

第一阶段目标是把现有 `render_html` 从单一固定 CSS renderer 升级为多主题、微信正文兼容、可验证、可降级的
确定性 renderer。暂不让 LLM 生成 HTML，也不新增排版 HITL，不接公众号发布 API。

输入仍是：已批准 Markdown、图片 URL/图注/插入位置、可选主题配置。输出建议逐步增加：

```text
wechat_body_html  # body-only，真正交付/粘贴的正文
preview_html      # 开发面板用完整 HTML 外壳
layout_metadata   # theme_id、renderer_version、validator_status、fallback_used
```

为保持第一版前端兼容，可以暂时继续把 `final_html` 作为 preview_html 返回，同时在内部先生成并校验
wechat_body_html；正式扩展 artifact 字段前需要做一次前端 contract test。

### 10.2 主题来源和首批数量

各上游共登记 32 个主题槽位：WeWrite 18 套、aiworkskills 4 套、gzh-design 6 套、Baoyu 4 套；其中
Baoyu 与 aiworkskills 都使用 `default/grace/simple/modern` 名称，且不同仓库还存在视觉职责重叠，因此
不能把 32 当作 32 个可直接上线的独立主题。`xiaowan` 没有主题引擎，只提供一组暖米白/赭金视觉起点。

考虑运行技术栈、维护成本和许可证，建议首批固定 6 套，逐套迁移并做快照：

1. `professional-clean`：企业、科技、政策和通用文章，作为默认主题；来自 WeWrite。
2. `minimal`：观点、学术、深度说明，留白优先；来自 WeWrite/aiworkskills simple 的非冲突规则。
3. `bold-navy`：商业、金融、行业分析；来自 WeWrite。
4. `warm-editorial`：文化、人物、生活方式；来自 WeWrite。
5. `tech-modern`：技术教程、工具清单、产品能力说明；来自 WeWrite。
6. `focus-red`：重要政策、重点解读、行动提醒，使用克制红色强调；来自 WeWrite。

第二批再评估 aiworkskills 的 `grace/modern/default`，以及 WeWrite 的 `newspaper/sspai/bauhaus` 等主题。
`gzh-design` 的 `moyu-green/red-white/graphite-minimal/zen-whitespace/moyu-ticket/olive-journal` 只有在项目
明确接受 AGPL 义务或获得其他授权后才进入代码迁移候选；在此之前仅使用其平台兼容校验思路和人工设计
参考，不复制主题 HTML。`xiaowan` 的暖米白+赭金不是独立 renderer 主题，可以作为 `warm-editorial` 的
验收参考，不再单独复制一套主题。

### 10.3 代码结构建议

```text
app/rendering/
  layout_engine.py       # 统一接口和 fallback 编排
  markdown_ast.py        # Markdown -> 受控块结构
  body_renderer.py       # section/p/span/img + inline style
  theme_registry.py      # YAML/代码主题 allowlist
  themes/
    professional_clean.py
    minimal.py
    bold_navy.py
    warm_editorial.py
    tech_modern.py
    focus_red.py
  wechat_validator.py    # ERROR/WARN 规则
  preview.py              # body -> 完整 preview wrapper
  legacy_renderer.py      # 当前 HtmlRenderer 的安全降级实现
```

统一入口应类似：

```python
result = layout_engine.render(
    markdown=approved_markdown,
    images=approved_images,
    theme_id="professional-clean",
)
```

`ThemeDefinition` 只允许定义颜色、字号、间距、组件模板和可用 variant；不允许主题文件带任意脚本、外部
资源或未知 HTML。主题选择可由配置或未来 AI layout plan 传入，但 renderer 必须再次用 allowlist 校验。

### 10.4 逐步实现顺序

1. 先把当前 `HtmlRenderer` 抽象为 `LayoutEngine` 接口，并保留当前安全主题 fallback。
2. 加入 Markdown AST/受控块模型，确保标题、段落、引用、列表、代码、图片和外链都不会丢失。
3. 先实现 `professional-clean` 和 `minimal` 两套内联主题，输出 body-only，并加入 validator。
4. 迁移 `bold-navy`、`warm-editorial`、`tech-modern`、`focus-red`，每套主题只迁移必要组件，不复制
   整个第三方仓库。
5. 图片按现有 `heading_path + paragraph_ordinal` 插入；只使用 URL、图注和位置，不下载图片。
6. 为相同 Markdown、图片、主题和 renderer 版本建立 golden snapshot；增加恶意 HTML、危险 URL、列表、
   多级标题、长中文标题和中英文混排测试。
7. 新 renderer 或 validator 失败时使用 `legacy_renderer`；AI/主题选择失败时使用
   `professional-clean`；两级 fallback 都失败才报错，不保存半成品。
8. 开发面板增加 theme_id、renderer_version、validator 和 fallback 状态的 debug 展示；非 debug 只展示
   `agent.activity` 的 skill/tool 活动和最终 artifact。
9. P0 稳定后，再增加一个只输出 `theme_id/density/section_variant` 的 strict `layout_plan` LLM；LLM
   不接触 HTML/CSS，不改变正文和图片。

### 10.5 可行性判断

可行性高，原因是：

- 输入输出边界已经明确，Graph 拓扑无需修改。
- 现有 renderer、图片插入逻辑、HTML fallback 和测试都可以复用。
- aiworkskills 已提供可参考的纯本地 Python 多主题实现；WeWrite 提供主题 YAML 和 validator 思路。
- gzh-design 的兼容规则和主题组件可用于设计调研，但 AGPL 资产在许可证结论明确前不进入首批代码。

主要风险不是算法难度，而是迁移工作量与兼容验收：首批 6 套主题约需逐套整理组件、处理 `span leaf`、
校验真实公众号粘贴效果，并完成许可证和 NOTICE 记录。建议先做 2 套主题的竖切验收，再批量迁移其余 4 套；
不要一次复制 18 套 WeWrite 主题，更不能在未确认 AGPL 义务时复制 gzh-design 主题。
