# 素材调研与联网搜索 V1 设计

> 状态：已确认的 V1 实现基线
>
> 实现目录：`src/wechat-article-agent`
>
> 相关基线：`docs/implement.md`
>
> 本文只描述 V1 的素材调研、Ark 内置联网搜索、素材合并和冲突处理。它不改变任务书、大纲、正文、图片和 HTML 节点的总体职责；下游节点暂时只通过提示词认识新增的素材类型。

## 1. 目标与明确边界

原来的“文档研读”升级为“素材调研/素材搜集”。它仍然先通过 HITL 确认 `about + target`，然后并行处理用户文档和联网素材，最后把两类素材合并后交给任务书节点。

V1 的目标是：

1. 用户可以确认“想搜集什么主题的素材”（`target`）以及“希望从哪些方面搜集”（`about`）。
2. 在环境变量允许且前置判断认为素材不足时，使用 Ark Responses 内置 `web_search` 补充事实、创作范例和网络流行文化素材。
3. 文档素材和网络素材在一次调研阶段并行执行，网络搜索不等待文档研读完成，也不在后续文章生成阶段再次补搜。
4. 保持旧版 `material_library` 的总体形状，新增网络素材所需的最少字段。
5. 在素材进入任务书前完成确定性的整理、必要的逐 chunk 压缩、事实冲突识别和一次性冲突 HITL。
6. 保留每个 chunk 的可读出处，冲突卡片能够告诉用户“哪一份材料的哪一处与哪一处不一致”。

V1 明确不做以下事情：

- 不调用独立的 Google、Bing、Tavily 或豆包搜索 API；只调用 Ark Responses 的内置 `web_search`。
- 不做图片搜索，不抓取搜索结果网页全文，不抓取网页中的图片，也不做多模态图片标注。
- 不把 `web_info_overview` 的结果当作素材；它的 `annotations` 完全忽略。
- 不把网络搜索结果用于当前图片生成节点；`resource` 只保留扩展槽，V1 的临时结果不被任务书、大纲、文章或生图消费。
- 不在文档研读之外再次联网搜索。
- 不保存独立的 `conflict_resolutions` artifact；冲突处理结果直接写入对应 chunk 的 `content`。
- 不压缩 `raw` 素材。预算不足时直接按优先级丢弃低优先级素材。
- 不新增 session、run、event 或 material 专用业务表。素材仍然作为当前 revision 的 JSONB 快照写入 `article_artifacts`。

## 2. Ark Web Search 的使用方式

### 2.1 调用形式

Ark Responses 请求在 `tools` 中声明内置工具：

```json
{
  "model": "doubao-seed-2-1-pro-260628",
  "input": [
    {"role": "system", "content": "你是网络素材搜集器……"},
    {"role": "user", "content": "请搜索与本次文章需求有关的事实资料……"}
  ],
  "tools": [{"type": "web_search", "max_keyword": 2}],
  "stream": true
}
```

Ark 会在响应中产生 `web_search_call` output item，并在其 `action.query` 中给出实际执行的 query。系统必须保存这个实际 query，而不是只保存调用前规划的文字。流式事件通常包括：

```text
response.output_item.added                  # web_search_call 开始
response.web_search_call.in_progress
response.web_search_call.searching
response.web_search_call.completed
response.output_item.done                   # action.query 出现在这里
```

同一次调用还会返回模型对搜索结果的总结文本和 annotations。V1 对不同节点的处理不同：

- `web_info_overview`：只取模型总结文本作为网络背景说明；忽略全部 annotations，不创建素材。
- `web_material_worker` 的四类搜索：取模型总结作为该类素材的 summary，并将 annotations 转为原始 chunks；此时 annotations 的 URL、标题、站点名称和摘要是当前能获得的有限出处信息。V1 不声称这些摘要等于网页全文。

### 2.2 搜索能力边界

Ark 内置工具是通用网页搜索，不等于专门的图片搜索或某个自媒体平台的完整 API。它可能召回公开网页、新闻、博客以及部分平台的公开页面，但不保证能够检索登录墙、反爬页面或每个平台的完整用户讨论。V1 只把搜索结果作为有限网络证据和创作参考，不能把“搜索不到”解释为“网上不存在”。

`resource` 类型保留在模型和素材 schema 中，便于未来接入专门图片搜索或资源 API；V1 即使收录了 resource 临时结果，也不会让下游内容生成节点消费。

## 3. 总体工作流

```text
intent_router
    |
    v
load_document_meta
    |
    v
material_sufficiency_judge（联网开关关闭时跳过 LLM，直接 false）
    |
    v
web_info_overview（need_web_search=true；结果只作背景）
    |
    v
research_direction HITL
    |  用户确认 about + target
    v
expired_material_filter
    |
    +-------------------------------+
    |                               |
    v                               v
document_material_worker       web_material_worker
                               （一次 Ark Responses 调用，
                                同时规划并搜索四类素材）
    |                               |
    +---------------+---------------+
                    |
                    v
           material_normalize
                    |
                    v
    material_merge_and_conflict_check
          |                    |
       无冲突                 有冲突
          |                    v
          |              conflict HITL
          |                    |
          +--------------------+
                    |
                    v
        emit_material_sources artifact
                    |
                    v
                task_spec
```

`document_material_worker` 和 `web_material_worker` 必须并行。网络搜索只执行这一轮，不等待文档研读结果后再“补搜”，也不在正文阶段再次搜索。`web_info_overview` 位于 HITL 前，因为它用于帮助模型理解“牛来”一类陌生或时效性主题并提出正确的研读方向；它不是正式素材。

`material_merge_and_conflict_check` 是一个阶段名称，其内部顺序固定为：

```text
document materials + web materials + retained old materials
    -> material_normalize
    -> deterministic exact dedup（同 ref/URL 或规范化内容哈希相同）
    -> context budget preflight
    -> material_compacter（仅超预算时）
    -> 按优先级裁剪（压缩后仍超预算时）
    -> conflict_checker
    -> 无冲突：写最终素材库
    -> 有冲突：HITL -> conflict_handler -> 写最终素材库
```

V1 不增加语义去重 LLM。只有 URL/ref 完全相同或规范化内容哈希相同才由代码确定性去重；“主题相近但内容不完全相同”的素材全部保留，交给下游使用。

## 4. 素材模型

### 4.1 兼容原则

素材仍然存放在 `ArticleArtifact.material_library` 中，外层仍然是素材数组，素材仍然拥有 `summary`、`active`、`metadata` 和 `orig_chunks`。旧代码需要的字段名尽量不变：

- `source_doc_id` 改为 `source`，因为它不再只表示文档。
- `orig_chunks[].path` 保留字段名，但语义改为“面向人和 LLM 的可读出处”。不再写 `document:<doc_id>` 这种无语义字符串。
- 文档素材的 `source` 是 `doc_id`；网络素材的 `source` 固定为 `web_search`。
- 文档 chunk 的 `type` 仍可为 `full_text` 或 `page`；网络 chunk 的 `type` 固定为 `web_search`。
- `metadata.extraction_mode` 允许 `raw`、`retrieve`、`web_search`。旧数据中的 `retrieve_fallback` 读取时视为 `retrieve`，写入新数据时不再使用该值。

### 4.2 V1 schema

```json
{
  "material_id": "mat_01",
  "material_kind": "user_document",
  "source": "doc-1",
  "summary": "该材料说明某公司 2025 年营业额、同比增长率及统计口径。",
  "active": true,
  "metadata": {
    "extraction_mode": "raw",
    "queries": [],
    "coverage_complete": true,
    "warnings": []
  },
  "orig_chunks": [
    {
      "chunk_id": "chunk_001",
      "content": "……",
      "type": "page",
      "path": "《公司年度报告》：第 12 页",
      "page_number": 12,
      "ref": "doc-1",
      "title": "公司年度报告"
    }
  ]
}
```

`material_kind` 取值：

| 值 | 含义 | V1 下游用途 |
| --- | --- | --- |
| `user_document` | 用户提供并授权使用的文档素材 | 最高优先级证据 |
| `factual` | 网络检索得到的事实、数据、官方资料 | 事实补充和交叉核对 |
| `resource` | 网络资源或未来图片搜索结果 | V1 仅暂存扩展槽，不供下游生成 |
| `creative_reference` | 他人文章、写法和结构的参考 | 只作创作参考，不作为事实证据 |
| `popular_culture` | 热梗、流行表达和受众语气参考 | 只作创作参考，不构成事实证据 |

网络素材示例：

```json
{
  "material_id": "mat_web_factual_01",
  "material_kind": "factual",
  "source": "web_search",
  "summary": "多个公开来源对该公司 2025 年营业额的报道及统计口径。",
  "active": true,
  "metadata": {
    "extraction_mode": "web_search",
    "queries": ["公司 2025 年营业额 官方 年报"],
    "coverage_complete": true,
    "warnings": []
  },
  "orig_chunks": [
    {
      "chunk_id": "chunk_web_001",
      "content": "搜索结果摘要……",
      "type": "web_search",
      "path": "公司官网",
      "page_number": null,
      "ref": "https://example.com/report",
      "title": "公司 2025 年度报告"
    }
  ]
}
```

网络 `metadata.queries` 必须保存 Ark 实际返回的 `web_search_call.action.query`。`coverage_complete` 对网络素材只表示“计划中的搜索类别和主题是否完成”，不表示互联网信息已经完整，也不表示事实已经被证明正确。

### 4.3 path 与出处

`path` 是展示字段，不是权限字段。生成规则：

- 用户文档：`{doc_name}：第 {page_number} 页`；全文 chunk 没有页码时使用 `{doc_name}：全文`。
- 网络搜索：优先使用 annotation 的 `site_name`，没有时使用站点域名或标题；不得写成没有语义的 URL 拼接字符串。

`ref` 保存机器可用的引用：用户文档为 `doc_id`，网络素材为原文 URL。V1 不保证能访问 URL，也不进行 URL 抓取。

## 5. 节点与 LLM 契约

以下契约是实现基线。所有结构化输出使用 strict JSON Schema，字段必须有中文 description；动态 ID 字典使用 Ark 支持的动态 schema，并由代码二次校验。

提示词改造遵守以下规则：

1. `research_direction`、`research_direction_validation`、`material_query_plan`、任务书、大纲和文章等已有节点，必须在当前已经优化过的系统提示词上做局部增补，保留其现有角色、输入说明、质量要求、禁止事项和已有示例，不得整段推翻重写。
2. 新增 LLM 节点的系统提示词仿照 `app/prompts/system.py` 现有 Markdown 分区风格，至少包含 `# 角色与任务`、与任务匹配的输入/规则说明、`# 输出契约`、`# 示例` 和 `# 禁止事项`。示例应覆盖一个典型场景或易错场景，不能只重复 schema。
3. 同一规则可以在系统提示词和由 Pydantic description 生成的输入说明中适度重复，但不能加入与该节点职责无关的工作流知识。

本阶段会出现的 LLM 调用如下，除此之外不再增加隐藏的规划器或二次补搜：

| LLM phase | 触发条件 | 次数上限 | 输出 |
| --- | --- | ---: | --- |
| `material_sufficiency` | 联网开关开启 | 1 | 是否需要联网 |
| `web_info_overview` | `need_web_search=true` | 1 | Markdown 网络背景，不入素材库 |
| `research_direction` | 进入素材调研 | 1 | A/B/C 三个 about/target 选项 |
| `research_direction_validation` | 用户提交自定义方向且未到轮次上限 | 每次自定义输入 1 次 | 是否明确及替代选项 |
| `material_filter` | 存在上一 revision 的素材 | 1 | 动态 material ID 保留字典 |
| `material_query_plan` | 单篇文档需要 retrieve | 每个待处理文档 1 次 | 文档检索 query groups |
| `web_material` | `need_web_search=true` | 整轮 1 次 | 四类网络素材总结和 URL |
| `material_compaction` | 素材超安全预算且非 raw | 每个待压缩 material 1 次 | 一一对应的 chunk 压缩字典 |
| `conflict_checker` | 至少两条可比较事实 chunk | 1 | 冲突组 |
| `conflict_handler` | 用户处理冲突 HITL | 1 | 各冲突 chunk 的最终说明或空字符串 |

### 5.1 `material_sufficiency_judge`

这是联网发现的前置判断，不判断最终素材库是否足够，也不决定文章能否交付。它只根据用户本轮提供文档的 overview 判断是否值得调用网络搜索，不读取上一 revision 的素材库。

当 `WEB_SEARCH_ENABLED=false` 时不调用 LLM，代码固定输出：

```json
{"user_facing_message":"当前联网搜索关闭或不可用，我将仅使用用户提供的文档素材。","need_web_search":false}
```

启用时的 LLM 输入：

```json
{
  "user_request": "用户原始请求",
  "document_overview": [{"doc_id": "doc-1", "doc_name": "年度报告", "doc_description": "……"}]
}
```

联网开关开启但本轮没有任何文档时，可以由代码直接设置 `need_web_search=true`，不必浪费一次 LLM 调用；此时说明“本轮没有可用参考文档，将尝试通过联网搜索补充素材”。

系统提示词：

```text
# 角色与任务
判断现有用户文档是否足以支持本次文章需求，决定是否值得补充联网素材。

# 判断标准
重点检查用户点名的主题、时效性事实、关键数据和创作背景是否明显缺失。这里只做联网发现前置判断，不评价最终文章能否生成。

# 输出契约
严格返回 need_web_search 和面向用户的简短说明。

# 示例
用户要求介绍某个近期网络事件，但文档只包含机构简介，应返回 need_web_search=true，并简要说明缺少时效性背景；用户要求总结文档内已经完整列出的制度条款时，可返回 need_web_search=false。

# 禁止事项
不要生成搜索 query，不要判断最终素材完整性，不要编写文章。
```

输出契约：

```json
{
  "user_facing_message": "用户文档中缺少与营业额相关的资料，我将补充联网素材。",
  "need_web_search": true
}
```

### 5.2 `web_info_overview`

仅在 `need_web_search=true` 时执行一次。它用用户请求做宽泛背景搜索，帮助后续研读方向 LLM 理解陌生词、热梗或主题背景。

系统提示词：

```text
# 角色与任务
你是素材调研阶段的网络背景调查器，只负责形成背景概览。

# 输入说明
只包含用户请求。

# 搜索要求
先理解用户主题，再搜索能帮助系统解释主题的公开信息。不要为了凑数量扩展到无关主题。

# 多主题与歧义处理
如果搜索后发现有两个或多个主题都合理符合用户请求，而仅凭当前输入无法判断用户具体指哪一个，不要擅自选择、合并或省略。必须在输出 Markdown 中分别设置清晰的小标题，详细介绍每个候选主题的含义、背景、区别和为什么可能符合用户请求。后续 research_direction 节点会据此生成选项让用户确认。

# 输出要求
用简洁 Markdown 总结网络背景、可能的歧义和需要后续重点搜集的方向。

# 示例
用户只说“写一篇关于牛来的文章”，搜索结果同时出现同名电影、网络热梗或其他同名对象时，应分别介绍各候选主题及其区别，而不是自行认定其中一个就是用户本意；如果搜索结果明确只对应一个主题，则直接总结该主题，不制造额外歧义。

# 禁止事项
不要把 annotations、URL 或未经核实的细节伪装成正式证据；不要输出 JSON 素材列表。
```

LLM 输入：

```json
{
  "user_request": "请生成一篇关于牛来的微信公众号文章"
}
```

多主题输出示例：

```markdown
## 候选主题一：同名网络流行文化话题

介绍该话题的出现背景、近期传播语境、主要受众和它为什么可能符合用户请求。

## 候选主题二：同名影视作品

介绍该作品的基本背景、与前一候选主题的区别和它为什么也可能符合用户请求。

## 待确认方向

当前请求不足以确定用户指的是哪个主题，后续应让用户在两个方向中确认，而不是将两者混写。
```

输出是一个 Markdown 字符串。存在多个无法确定的候选主题时，该 Markdown 必须完整保留各候选主题，供 `research_direction` 生成相应选项并询问用户。`annotations` 全部忽略，不写入 `material_library`，也不作为 conflict checker 的输入。

### 5.3 `research_direction` HITL

职责仍是一次性给出三个素材搜集方向，让用户确认 `about + target`。语义必须向前端解释为：

- `target`：用户想要搜集什么主题的素材。
- `about`：用户想要从哪些方面搜集该主题素材。

LLM 输入：

```json
{
  "user_request": "用户原始请求",
  "documents": [{"doc_id": "doc-1", "doc_name": "年度报告", "doc_description": "……"}],
  "previous_direction_memory": "仍然有效的历史调研偏好",
  "web_info_overview": "联网背景信息；没有搜索时为空字符串"
}
```

系统提示词：

```text
# 角色与任务
根据用户需求、文档概览和网络背景提出三个明确、彼此有差异的素材搜集方向。

# 字段语义
target 表示搜集什么主题的素材；about 表示从哪些方面搜集该主题素材。

# 选项要求
每个选项必须可以直接执行，不能用“进一步确认”等空泛描述；三个选项应覆盖不同侧重点。

# 输出契约
返回 coverage_mode、用户说明和恰好 A/B/C 三个选项。

# 示例
当网络背景同时介绍了同名电影和同名网络热梗时，应把两者分别形成可选择的 target，而不是把两个主题揉成一个选项；第三个选项可从用户文档提供的其他合理方向生成。
```

输出契约：

```json
{
  "coverage_mode": "best_effort",
  "user_facing_message": "我建议从以下三个素材搜集方向中选择一个。",
  "options": [
    {"id": "A", "about": "经营数据与发展背景", "target": "营业额、增长率和关键时间节点"},
    {"id": "B", "about": "行业影响与读者关注点", "target": "行业变化、现实影响和典型案例"},
    {"id": "C", "about": "通俗化表达所需的背景", "target": "概念解释、常见误解和可用于文章的例子"}
  ]
}
```

用户可以选择 A/B/C 或提交自定义 `about + target`。自定义输入继续受 `CLARIFICATION_MAX_ROUNDS` 约束；选择模型选项直接开始调研。达到最大轮次时采用用户最后输入强制开始，不报错。该 HITL 不让用户决定联网开关，联网是否可用由环境变量控制。

前端每个选项的主文案统一为：

```text
本次将搜集与「{target}」主题有关的素材，重点关注「{about}」方面，其余无关内容将忽略。
```

自定义输入使用已有的方向校验 LLM。其输入为用户请求、文档概览、自定义 `about/target`、当前轮次和最大轮次；系统提示词只判断两个字段是否已经明确、可执行，不扩展到受众、语气和文章目标。输出契约保持：

```json
{
  "sufficient": false,
  "user_facing_message": "当前范围仍较宽，请从以下更具体的方向中选择。",
  "normalized_about": "用户输入的忠实规范化结果",
  "normalized_target": "用户输入的忠实规范化结果",
  "options": [
    {"id": "A", "about": "……", "target": "……"},
    {"id": "B", "about": "……", "target": "……"},
    {"id": "C", "about": "……", "target": "……"}
  ]
}
```

`sufficient=true` 时 `options=[]`；达到轮次上限时不再调用该 LLM，代码直接采用最后一次输入。

### 5.4 `expired_material_filter`

这是以代码为主、包含一次轻量 LLM 素材级筛选的节点。

1. 调用检索服务 `route`，得到 `relevant_doc_ids`。仍然使用 `relevant_doc_ids - remain_doc_ids` 判断需要重新处理的文档；只要保留了某篇文档的一条旧素材，就认为该文档已经覆盖当前 `target/about`，不引入 `fully_covered_doc_ids`。
2. 代码删除来源不在 `relevant_doc_ids` 的旧用户文档素材。网络素材不参与文档级 route。
3. 有旧素材时，把所有素材的 `material_id + material_kind + summary` 交给 LLM，动态 schema 输出要保留的 ID；没有旧素材时跳过该调用。
4. 代码按输出保留/删除素材，并保留 `relevant_doc_ids` 供文档 worker 计算差集。

素材级筛选 LLM 输入：

```json
{
  "research_direction": {"about": "……", "target": "……"},
  "materials": [
    {"material_id": "mat-1", "material_kind": "user_document", "summary": "……"}
  ]
}
```

系统提示词：

```text
# 角色与任务
判断旧素材是否仍与当前素材搜集方向直接相关。

# 输入说明
只根据 research_direction 和每项素材的种类、摘要判断，不猜测未提供的正文。

# 输出契约
对输入中的每个 material_id 返回 true/false；true 表示保留。

# 示例
当前 target 已从公司历史改为最新经营数据时，仅介绍创立历史且不再相关的旧素材返回 false，仍包含当前经营数据或统计口径的素材返回 true。

# 禁止事项
不要新增 ID，不要改写素材，不要因为来源是网络就自动删除。
```

动态输出示例：

```json
{"mat-1": true, "mat-web-1": false}
```

代码必须忽略未知 ID，并在空结果或结构不合法时保守保留已有素材，避免一次错误的筛选删除全部历史证据。

### 5.5 `document_material_worker`

这是原文档研读流程的主体，职责不再包含过期素材删除：

- 对 `relevant_doc_ids - remain_doc_ids` 的文档并行提取。
- 短文档优先调用 `raw`，生成 `extraction_mode=raw` 的素材；`raw` 素材以后永远不压缩。
- 长文档或 raw 不可用时，调用已有 query planner，再调用 `retrieve`。raw 失败可按既有降级策略使用 retrieve。
- 单个文档失败可以产生 warning 并继续；`all_required` 覆盖模式下仍按现有契约失败。
- 本轮没有任何可用文档素材时不报错，交给后续联网结果或 `generation_basis=general_knowledge` 继续生成。

现有 query planner 的输入仍包含纯净的文档概览、`about`、`target` 和查询组数；输出仍为分组 queries。其系统提示词继续要求按主题拆出可由检索服务回答的具体问题。fallback query 是代码固定的“请详细总结这篇文档中与当前需求相关的事实、数据和结构”，不新增联网逻辑。

query planner 的 LLM 输入和输出契约：

```json
{
  "document": {"doc_id": "doc-1", "doc_name": "年度报告", "doc_description": "……"},
  "research_direction": {"about": "经营数据", "target": "营业额和增长率"},
  "group_count": 3
}
```

```json
{
  "query_groups": [
    {"queries": ["2025 年营业额是多少，统计口径是什么？", "同比增长率是多少？"]}
  ]
}
```

提示词只要求为当前单篇文档规划可被检索服务回答的问题，不讨论联网搜索，也不生成文章内容。

### 5.6 `web_material_worker` 与四类搜索

该 worker 仅在 `need_web_search=true` 时执行。整个 worker **只做一次 Ark Responses 调用**：模型在同一次调用中自行生成多个实际 query、调用内置 `web_search`，然后将结果归入 factual、resource、creative_reference、popular_culture 四类。这里没有四个独立的 LLM worker，也没有“搜索后再补搜”的第二轮。resource 保留扩展槽，可暂时收录本次调用里的结果，但不供下游内容生成使用。

这一次调用的 prompt 由代码拼接，不设置独立的 query planner：

```text
# 角色与任务
你是微信公众号素材调研器，负责搜索并总结指定类别的网络素材。

# 用户需求
用户请求：{user_request}
搜集主题：{target}
搜集方面：{about}
网络背景：{web_info_overview}

# 类别规定
将结果区分为 factual、resource、creative_reference、popular_culture；严肃主题不强求 popular_culture，当前不做专门图片搜索。

# 搜索要求
可改写为多个具体 query；优先相关、近期、可核对的公开来源；区分事实证据和创作参考。

# 输出要求
总结本类别真正有用的内容，并保留可访问的出处；不得编造搜索结果。

# 示例
严肃的法规解读主题应重点返回 factual 和 creative_reference，popular_culture 可以为空；近期热梗主题应补充 popular_culture 的实际用法，但其中的表达不能写入 factual。
```

单次调用中的类别规定：

- `factual`：搜索事实数据、官方资料、时间、制度、统计和可核对的事件；尽量优先官方、权威媒体和原始报告。
- `resource`：V1 不执行专门图片搜索；保留接口和素材类型，未来接入资源 API 时再启用。临时结果不被下游消费。
- `creative_reference`：搜索他人文章、公开写法和结构，只提取表达方式、叙事组织和可借鉴角度，不把其事实直接当成证据。
- `popular_culture`：只在主题明显涉及热梗、流行文化或时效性网络表达时启用；搜索近期公开讨论，说明语气和使用场景，不把梗的解释当成事实证明。

搜索 LLM 的 strict 输出契约为四个可为空的类别：

```json
{
  "factual": {"summary": "事实总结", "urls": ["https://example.com/a"]},
  "resource": {"summary": "", "urls": []},
  "creative_reference": {"summary": "写法总结", "urls": ["https://example.com/b"]},
  "popular_culture": {"summary": "近期用法总结", "urls": ["https://example.com/c"]}
}
```

同次响应返回总结 JSON、若干 `web_search_call.action.query` 和 annotations。代码按以下规则归一化：

1. `summary` 使用 Ark 模型的最终总结文本。
2. 输出 URL 必须存在于同次调用的 annotations URL 集合；无法对应的 URL 删除。
3. URL 按 `factual > creative_reference > resource > popular_culture` 跨类别去重，同一 URL 的 annotation
   正文只进入最高优先级类别，避免下游重复接收同一搜索摘要。creative_reference 或 popular_culture
   如果因为跨类别去重而没有剩余 URL chunk，但该类别原本引用过本次合法 annotation URL，则保留该类别
   material，并用该类别 `summary` 构造无重复 URL 的 summary-only chunk；没有合法 URL 的模型总结仍丢弃。
4. 每个 URL 对应一个 `orig_chunks`，chunk 的 `content` 使用当前 annotations 摘要，`path` 使用 site_name/域名/标题，`ref` 使用 URL，`type=web_search`。
5. `metadata.queries` 写本次响应中 Ark 实际执行的全部 `web_search_call.action.query`；如果 provider 无法指出某个 query 对应哪个类别，各网络 material 保存同一份实际 query 列表，不自行猜测映射。
6. `coverage_complete` 只表示本次计划的搜索主题是否完成。

web worker 的失败降级：一次调用中某类别为空时只跳过该类别；整次调用失败时不做第二轮搜索，记录 warning 后使用文档素材或通识生成，不使整轮文章失败。用户可见文本应说明网络素材不可用，但不能暴露内部堆栈。

### 5.7 `material_normalize`

这是纯代码归一化步骤，不调用 LLM：

- 将旧字段 `source_doc_id` 迁移成 `source`；旧 `document:<doc_id>` path 转成可读文档名和页码。
- 补齐 `material_kind`：旧文档素材为 `user_document`。
- 将旧 `retrieve_fallback` 归一化为 `retrieve`。
- 确保每个 material 和 chunk 有稳定唯一 ID；重复 ID 重新生成。
- 按既定类别优先级对相同 `ref`/URL 做跨类别确定性去重，对规范化 content 哈希完全相同的 chunk 去重；
  creative_reference 和 popular_culture 被去空时按上一节规则保留 summary-only chunk，不做基于相似度的
  语义去重。
- 去除 warnings 中不适合输入 LLM 的大段诊断文本，只保留短 code/message 摘要。
- 不改写 `raw` 的 content，不删除其原文。

### 5.8 `material_compacter`

素材整理先按类型确定内容表示，再判断是否需要调用压缩 LLM。最大模型窗口通过 `ARK_CONTEXT_WINDOW`
配置，默认 256K；它不是直接可用的安全阈值，代码必须预留系统提示词、输出、工具事件和安全余量。

压缩规则：

1. `creative_reference` 和 `popular_culture` 不调用压缩 LLM，直接用该 material 的 `summary` 作为下游
   内容；未被跨类别去重的 `orig_chunks` 仍保留 URL、title、path 等来源元数据。若其 URL 全部被更高
   优先级类别占用，则保留一个无重复 URL 的 summary-only chunk。
2. `resource` 不压缩，也不供下游生成节点消费，V1 只落库保存。
3. `metadata.extraction_mode=raw` 的素材永远跳过压缩。
4. 只有纯净输入仍超过安全预算时，才对非 raw 的 `user_document` 和 `factual` 素材并行调用压缩 LLM。
5. 一个参与 LLM 压缩的 `orig_chunk` 必须对应一个压缩结果，一一对应、原地覆盖 `content`；不能把多个
   chunk 合并成一个，也不能改变 chunk 数量、`chunk_id`、`path`、`ref` 和出处信息。
6. 压缩提示词只处理两类重点：factual 保留事实、数值、时间和口径，user_document 保留与
   `about/target` 相关的证据。
7. 压缩失败时保留原 chunk；不能因为压缩失败删除用户文档或事实证据。
8. 整理后仍然超预算时不继续压缩 raw，也不无限循环；按
   `user_document > factual > creative_reference > resource > popular_culture` 保留并丢弃低优先级素材，
   直到输入可用。被丢弃的素材写入 warning，不改变原始外部文档。

为保证一一对应，一个素材下的所有 chunk 使用动态 schema 一次返回：

```json
{
  "chunk_001": "保留该 chunk 中与当前素材类型和研读方向有关的内容。",
  "chunk_002": "第二个 chunk 的压缩内容。"
}
```

LLM 输入包含 `material_kind`、material summary、`about/target` 以及 `[{chunk_id,path,content}]`。动态 schema 要求输出 key 与输入 chunk ID 完全一致。代码校验数量和 ID 集合；缺少、增加或空内容时，该素材整次压缩结果作废并保留全部原 chunks，防止半覆盖。

压缩器系统提示词：

```text
# 角色与任务
在不改变出处和 chunk 对应关系的前提下，压缩每个 chunk 中与当前素材搜集方向有关的内容。

# 保留规则
factual 保留事实数据、数值、时间、统计口径和限定条件；user_document 保留与当前方向相关的原始证据。
creative_reference、popular_culture 和 resource 不调用本节点。

# 输出契约
动态 schema 中每个输入 chunk_id 必须恰好返回一次，value 是该 chunk 的压缩内容。

# 示例
输入 chunk 包含“2025 年营业额 12 亿元、统计口径不含海外业务”等信息时，压缩结果必须同时保留数值、年份和口径，不能只写成“公司营业额有所增长”。

# 禁止事项
不要合并 chunk，不要补充外部知识，不要修改 ID、path 或 ref，不要压缩 raw 素材。
```

### 5.9 `conflict_checker`

冲突检查只在素材预算整理完成后执行。它接收所有仍然 active 的纯净 chunks，至少包含 `chunk_id`、`content`、`path`、`ref`、`title` 和 `material_kind`。没有至少两条可比较的事实性内容时可直接输出空列表。

系统提示词：

```text
# 角色与任务
比较素材中的事实、数值、时间、定义和流程，找出对文章结论有影响的矛盾，并按冲突组列出涉及的 chunk。

# 判断原则
区分真正的事实矛盾、统计口径差异和仅仅表述不同。创作范例与流行文化的写法差异不是事实冲突。

# 输出要求
每组返回涉及的 chunk_id 和一段面向用户的冲突说明，说明中引用可读 path。

# 示例
两个来源分别写“营业额 12 亿元”和“营业额 15 亿元”且统计年度和口径相同时，应返回一个冲突组；如果一个是全年数据、另一个是上半年数据，应说明口径不同，不要直接判定为事实冲突。

# 禁止事项
不要改写原文，不要输出输入中不存在的 chunk_id，不要把表述差异误判为事实冲突。
```

LLM 输入：

```json
{
  "research_direction": {"about": "……", "target": "……"},
  "chunks": [
    {"chunk_id": "chunk_001", "material_kind": "user_document", "path": "年度报告：第 12 页", "ref": "doc-1", "content": "……"},
    {"chunk_id": "chunk_002", "material_kind": "factual", "path": "某媒体", "ref": "https://example.com", "content": "……"}
  ]
}
```

输出契约只描述冲突组，不在这一阶段判断哪些 chunk 不可信，也不生成最终前缀：

```json
{
  "conflicts": [
    {
      "conflict_chunk_ids": ["chunk_001", "chunk_002"],
      "user_facing_message": "《年度报告》第 12 页与某媒体关于营业额的数据不一致。"
    }
  ]
}
```

如果没有冲突，`conflicts=[]`，不发起 HITL。代码校验每组至少两个已知 chunk ID、组内去重和说明长度；非法组删除，全部非法时按“无可确认冲突”降级并写 debug warning，不阻塞文章生成。

### 5.10 冲突 HITL 与 `conflict_handler`

有非空 `conflicts` 时，先把冲突 chunks 的出处、摘要和说明通过 `agent.artifact` 发送给前端，再发 `agent.interrupt`，表单类型为 `agent_artifact_review`。三个选项是：

1. `document_priority`：始终以我提供的文档为准。
2. `automatic_authority`：按来源权威性和数据可靠性自动判断。
3. `custom_feedback`：用户自由输入处理意见。

用户提交的 decision、feedback 和冲突 chunks 交给 `conflict_handler`。只输入冲突组，不把无关素材重新塞给模型。

`conflict_handler` 只调用一次 LLM。它不输出要删除的 ID，而是对模型认为不可信的 chunk 生成最终冲突说明；可信 chunk 输出空字符串。动态 schema 示例：

```json
{
  "chunk_001": "该数据来源于某自媒体平台，已验证可能有误，正确应为：……",
  "chunk_002": ""
}
```

对于 `document_priority`，代码可直接要求 LLM 以 `material_kind=user_document` 为判断基准；对于 `automatic_authority`，模型根据 `path/ref/material_kind` 判断来源权威性；自定义反馈则是最高优先级的处理依据。

handler 的 LLM 输入：

```json
{
  "decision": "automatic_authority",
  "feedback": "",
  "conflicts": [
    {
      "user_facing_message": "两个来源的营业额不一致",
      "chunks": [
        {"chunk_id": "chunk_001", "material_kind": "user_document", "path": "年度报告：第 12 页", "content": "……"},
        {"chunk_id": "chunk_002", "material_kind": "factual", "path": "某媒体", "content": "……"}
      ]
    }
  ]
}
```

系统提示词：

```text
# 角色与任务
根据用户选择和素材出处判断冲突组中哪些 chunk 不可信，并为这些 chunk 生成简短、具体的冲突说明。

# 决策规则
document_priority 以用户文档为准；automatic_authority 比较官方性、原始性、时间和统计口径；custom_feedback 忠实遵守用户意见。

# 输出契约
动态 schema 中每个冲突 chunk_id 都返回字符串；不可信者返回可直接拼到原 content 开头的说明，可信者返回空字符串。

# 示例
用户选择 document_priority，用户年度报告与自媒体数字冲突时，年度报告 chunk 返回空字符串，自媒体 chunk 返回“该数据与用户提供的年度报告不一致，应以年度报告口径为准”等具体说明。

# 禁止事项
不要删除或改写原文，不要处理无关 chunk，不要生成不存在的 ID。
```

代码校验：

- 只接受当前冲突组中的 ID，未知 ID 忽略。
- 输入冲突组的每个 chunk 必须有输出；缺失时该 chunk 视为可信，不删除任何内容。
- 原有 chunk 全部保留。对 message 非空的 chunk，将 message 作为前缀拼接到该 chunk `content` 最开头，例如：

  ```text
  [冲突提示] 该数据来自某自媒体平台，已验证可能存在错误，正确口径应为：……

  原始素材内容……
  ```

- 不新增 `conflict_resolutions` 数据库字段；处理后的 content 随最终 `material_library` 写回当前 revision。HITL 的原始说明只存在 checkpoint interrupt payload 和实时 SSE 中。

## 6. 持久化、checkpoint 与事件

### 6.1 写入时机

沿用 `article_artifacts` 单表和 revision 快照：

1. `material_sufficiency_judge` 与 `web_info_overview` 的临时结果只保存在小型 Graph State/checkpoint 中；overview 不入素材库。
2. `research_direction` 确认后写入当前 revision 的 `research_direction`。
3. `expired_material_filter` 完成后写入 `relevant_doc_ids` 和筛选后的历史素材，避免后续 worker 失败时丢失有效旧素材。
4. 两个 worker 完成并归一化后，将候选 `material_library` 写入当前 revision，状态仍为 `running`。
5. `conflict_checker` 无冲突时，把最终素材库写入并进入任务书；有冲突时，候选素材库先写入，revision 状态置为 `waiting_for_input`，这样断线后可以根据 checkpoint 和 artifact 重新展示冲突卡。
6. `conflict_handler` 恢复后按上述规则修改 chunks，再以同一 revision 覆盖 `material_library`，然后进入任务书。

不写入独立网络素材表、不保存完整 web_search 原始 SSE、不保存单独 conflict resolution 历史版本。revision 仍由新的业务需求变更创建；同一 revision 内的冲突处理和审批可以覆盖未批准的候选素材。

### 6.2 Graph State 只存控制信息

checkpoint 只保留当前节点、游标、`artifact_id/revision`、研究方向、是否需要网络搜索、overview、小型 warning 摘要和 pending interrupt。大段原文、网络 summary、chunk 内容和 HTML 从 `article_artifacts` 按 artifact ID 读取，不复制进 checkpoint。

### 6.3 下游消费

任务书和大纲接收 `web_info_overview` 作为“网络背景”，但提示词明确它不是可引用证据。任务书、大纲和文章读取最终 material library 时：

- `user_document` 与 `factual` 可作为事实依据，但网络 factual 仍需尊重冲突提示。
- `creative_reference` 只指导写法和结构。
- `popular_culture` 只指导语气、梗的使用方式和受众表达。
- `resource` V1 过滤掉，不进入这三个节点的输入。

V1 不修改大纲/文章结构化输出契约，只调整其输入清洗和系统提示词。

### 6.4 对外事件

- 节点开始/完成继续使用 `agent.activity`。
- `web_info_overview` 发一个 tool 类型 `agent.activity`，label 为“正在了解网络背景”；单次 web material 调用发一个 tool 类型 `agent.activity`，label 为“正在搜索网络素材”。Ark 在同一响应中实际执行的多个 query 作为该活动的实时/调试明细，不在无法可靠映射时伪造成多个类别工具调用。resource V1 无独立调用时不伪造工具事件。
- 素材来源列表、素材候选、冲突来源和冲突说明使用 `agent.artifact`；冲突审批使用 `agent.interrupt`。
- debug 开启且服务允许时，在 trace 的“LLM 调用”和“工具/Skill 调用”中保存各次调用的输入、原始输出、实际 query、annotations 摘要、校验/去重结果和错误；debug 关闭时不构造、发送或持久化这些重型字段。

#### 面向用户的素材来源 artifact

素材调研全部完成、冲突处理也已经完成后，`emit_material_sources` 从最终 active `material_library` 纯代码派生一个 `agent.artifact`。派生过程先读取 `material_kind` 判断素材来源类型，再从该素材的 `orig_chunks` 读取展示值。它只告诉用户本轮参考了哪些来源，不包含 summary、chunk content、页码、查询词、warning 或其他内部字段。

构造规则：

1. `material_kind=user_document`：只读取该素材 `orig_chunks[0].title`，将文档名加入列表；即使素材来自特定页面，也不展示页码，不读取 `path`。
2. `material_kind` 为 `factual`、`resource`、`creative_reference` 或 `popular_culture`：不按类别分组，遍历所有 `orig_chunks`，将每个非空 `ref` URL 加入同一个列表。
3. 按最终 material 和 chunk 的稳定顺序去重；同一文档名或 URL 只出现一次。空 title、非 URL 的网络 ref 和非 active material 忽略，并只在 debug trace 记录 warning。
4. 即使没有任何来源也发送该 artifact，`content=[]`，前端据此显示“本轮未使用外部参考素材”。

这里以具体构造规则为准：用户文档取 `orig_chunks[0].title`，网络素材取 `orig_chunks[].ref` 中的 URL。`orig_chunks[].path` 仍用于 debug trace 和冲突出处展示，但来源列表不直接使用 `path`，否则用户文档会泄露页码，网络素材也只能得到站点名称而不能得到可点击 URL。

事件示例：

```json
{
  "type": "agent.artifact",
  "artifact_id": "art_...",
  "revision": 2,
  "stage": "material_sources",
  "status": "completed",
  "content": [
    "公司年度报告",
    "https://example.com/official-report",
    "https://example.com/reference-article"
  ]
}
```

该事件的业务内容严格只有一个字符串列表，不再包装成 `{\"sources\": [...]}`。前端可根据字符串是否为 `http://` 或 `https://` 决定渲染为外链或普通文档名。

这份列表不作为新的数据库 artifact 字段持久化，而是由已经持久化的最终 `material_library` 确定性派生；需要重新展示当前结果时可以重新构造。

现有开发面板展示的“原始素材模型” artifact 保持不变：它继续包含完整素材模型，只在服务端 debug 开启且请求允许 debug 时发送给开发者，不向正式用户返回。开发面板在保留该 debug 卡片的同时，新增与正式前端一致的“参考素材来源”卡片；debug 关闭时只能看到来源列表卡，不能看到完整素材内容。

冲突卡片沿用协议已有字段，不新增事件名。建议的最小 payload：

```json
{
  "type": "agent.artifact",
  "stage": "material_conflicts",
  "status": "pending_review",
  "content": {
    "conflicts": [
      {
        "conflict_chunk_ids": ["chunk_001", "chunk_002"],
        "message": "《年度报告》第 12 页与某媒体的营业额数据不一致。",
        "sources": [
          {"chunk_id": "chunk_001", "path": "年度报告：第 12 页"},
          {"chunk_id": "chunk_002", "path": "某媒体"}
        ]
      }
    ]
  }
}
```

随后发送的 `agent.interrupt` 复用当前 `artifact_id/revision/response_id/interrupt_id`，form 中提供 `document_priority`、`automatic_authority` 和 `custom_feedback`。前端提交时仍通过 `POST /v1/responses` 的 `context.hitl` 恢复，不新增 resume 接口。

## 7. 降级与错误矩阵

| 环节 | 可恢复/可接受错误 | V1 行为 |
| --- | --- | --- |
| `meta` 读取文档失败 | 网络超时、上游 5xx | 按现有有限重试；仍失败则返回清晰错误，不能猜测文档名称 |
| `raw` 不可用 | 202 等待、短暂 503 | 按现有轮询/重试；耗尽后对该文档降级到 `retrieve` |
| `retrieve` 部分覆盖 | `coverage.complete=false`、部分 chunks | 保留已得到素材并记录 warning；普通文章继续，`all_required` 失败 |
| 文档 route 没有命中 | `relevant_doc_ids=[]` | 不报错；记录“没有匹配文档”，继续联网或通识生成 |
| 文档 worker 全部失败 | 技术错误 | 若联网得到素材则继续；否则 `generation_basis=general_knowledge` |
| 网络开关关闭 | 配置为 false | 不调用 sufficiency LLM，也不调用 Ark web_search |
| sufficiency LLM 失败 | 超时、结构化修复耗尽 | 保守设置 `need_web_search=false`，使用文档/通识生成，并写 warning |
| overview 失败 | Ark 搜索失败 | overview 为空，仍可进入研究方向；不创建网络素材 |
| 网络素材调用失败 | 超时、429、5xx | 不发起第二轮搜索；整个网络分支降级，文档/通识流程继续 |
| annotations 不完整 | URL 无法校验、摘要为空 | 删除该 URL/chunk；必要时该类别为空 |
| 压缩 LLM 失败 | 超时、schema 错误 | 对应素材的原 chunks 全部保留；预算仍超时按优先级丢弃低优先级素材 |
| conflict checker 失败 | 结构化输出修复耗尽 | 不发起冲突 HITL，保留素材并写 warning；不得删除证据 |
| conflict handler 结果非法 | ID 未知、字段缺失 | 忽略未知 ID；缺失 ID 视为无冲突说明，保留原 chunk |
| 数据库写入短暂失败 | 连接断开、deadlock | 使用现有数据库有限重试；耗尽后 run 失败，checkpoint 不伪装为完成 |

网络调用错误不得将环境变量中的 API key、完整 provider 响应或内部堆栈发给用户。可重试的 Ark 超时、429、5xx 使用现有网关的有限重试和熔断；耗尽后把对应网络分支标为 degraded，不无限尝试。

## 8. 配置项

V1 只新增联网开关；模型窗口沿用已有配置。超时、重试、并发、限流和熔断全部复用当前 Ark 网关配置，不再为 web search 建一套重复参数。

| 配置 | 默认建议 | 用途 |
| --- | ---: | --- |
| `WEB_SEARCH_ENABLED` | `false`（本地按需开启） | 全局联网开关 |
| `ARK_CONTEXT_WINDOW` | `262144` | 模型最大上下文能力；安全预算由代码另行计算 |

安全余量是代码常量/计算规则，不直接把 256K 全部交给素材，也不再增加一个可随意配置而破坏安全边界的 ratio。Ark `web_search.max_keyword` V1 使用代码中的保守固定值，后续确有调优需要再提升为配置。

生产根 `.env.example` 和子项目 `.env.example` 都要同步，不能从旧遗留配置照搬 Redis 等无关项。

## 9. 实现计划

### 阶段 0：契约和探测基线

1. 固定 `MaterialKind`、chunk 字段和兼容旧 schema 的归一化函数。
2. 检查现有 Ark web_search 探测脚本，补充对 `web_search_call.action.query`、summary、annotations 和异常响应的解析测试。
3. 固定新增配置及本地/生产默认值。

### 阶段 1：输入/输出 schema 与提示词

1. 在 `app/llm/schemas.py` 增加 sufficiency、material filter、web result、chunk compaction、conflict checker、conflict handler 的 strict schema，所有字段写中文 description。
2. 在 `app/llm/inputs.py` 增加纯净输入模型，过滤 warnings、重复 metadata 和无关完整响应。
3. 在 `app/prompts/system.py` 对已有提示词做增量修改，不替换已经优化过的主体；新增节点提示词必须包含 `# 示例`，并明确 overview 不是素材、各 material kind 的消费边界、raw 不压缩和 strict 输出要求。

### 阶段 2：素材模型和持久化兼容

1. 更新 `ArticleArtifact.material_library` 的兼容模型，确保旧 revision 可读取。
2. 实现 `normalize_material_library`，迁移 `source_doc_id`、path、旧 extraction mode，补齐 kind/title/ref。
3. 不新增素材表；验证同 revision 候选写入、冲突等待和恢复覆盖的事务边界。

### 阶段 3：图编排

1. 增加 `material_sufficiency_judge` 和条件 `web_info_overview`，并把 overview 输入研究方向。
2. 将现有 `route_documents` 拆为 `expired_material_filter`；保留 `relevant_doc_ids - remain_doc_ids` 规则。
3. 将 `extract_materials` 拆为 `document_material_worker`，并增加并行 `web_material_worker`。
4. 增加 normalize、compacter、conflict checker、conflict HITL、handler。HITL 恢复必须使用标准 LangGraph `Command(resume=...)`，不得直接修改 checkpoint。
5. 用 LangGraph 并行分支和 join 保证两个 worker 都结束后才合并；局部错误转 warning，不吞掉另一分支结果。

### 阶段 4：Ark 工具和事件

1. 在 Ark Responses 网关中增加 web_search tools payload，解析 web_search_call 生命周期和实际 action.query。
2. 整个 web material worker 只调用一次 Ark Responses；resource 保留字段但不进入下游。
3. 复用现有 tool activity、debug trace 和 Responses-like normalizer；debug 关闭时避免保存大 prompt、annotations 和原始响应。
4. 实现 `emit_material_sources` 纯代码节点，从最终 material library 派生字符串来源列表并发送 `agent.artifact`；不新增数据库字段。

### 阶段 5：开发面板与文档

1. 文档研读文案改为“素材搜集主题”和“素材关注方面”。
2. 增加网络搜索工具卡，显示 running/completed/degraded，并与节点 activity 视觉区分。
3. debug trace 展示 sufficiency、overview、各搜索、压缩器和冲突节点的 LLM/tool 调用；已有调用不能被新结果覆盖。
4. 冲突 artifact 以来源对照方式展示，interrupt 显示三个决策和自由反馈框。
5. 新增正式用户可见的“参考素材来源”卡片，渲染文档名和可点击 URL；原始素材模型卡继续只在 debug 开启时展示，两者不能相互覆盖。
6. 更新 API、前端集成、环境变量和 implement 文档中的相关章节。

### 阶段 6：下游提示词和回归

1. 不改任务书、大纲、文章结构化契约；只调整输入清洗和提示词，明确各素材种类用途。
2. 仍根据最终可消费素材是否为空设置 `generation_basis`；只有 resource 时仍视为无内容素材，使用 general knowledge。
3. 运行单元、图 contract、SSE、开发面板和真实 Ark 冒烟测试。

## 10. 验收边界与测试清单

### 10.1 Schema 和归一化

- 旧版文档素材可读，`source_doc_id` 正确迁移为 `source`。
- `path` 对文档显示文档名和页码，对网络显示站点语义，不再出现 `document:doc-id` 噪音。
- 每个 material 和 chunk 有唯一 ID；`raw` content 在任何步骤前后字节级不变。
- 压缩前后 chunk 数量、ID、path、ref 完全相同；不存在多 chunk 合并。

### 10.2 工作流

- 只有文档、只有联网、文档和联网同时存在、两者都没有四种情况都能完成到任务书。
- `WEB_SEARCH_ENABLED=false` 时没有任何 Ark web_search 请求和工具 activity。
- sufficiency 只判断是否需要联网，不以最终素材数量作结论。
- overview summary 可被研究方向、任务书和大纲看到，但 annotations 不进入素材库。
- overview 遇到多个都符合请求且无法判定的主题时，Markdown 分别完整介绍，research direction 能据此生成不同选项供用户确认。
- 文档 worker 与 web worker 真正并行；网络慢不会让文档 worker 等待后才开始。
- resource 不会进入任务书、大纲、文章和图片输入。

### 10.3 网络搜索

- 每次素材调研最多一次 web material Ark Responses 调用，不因类别为空再次搜索。
- `metadata.queries` 等于 Ark 实际 `action.query`。
- URL 校验、跨类别去重和空类别删除有单元测试。
- 类别为空允许继续；整次调用失败可降级到文档或通识生成。

### 10.4 压缩与冲突

- 素材未超预算时不发生压缩调用。
- 超预算时 raw 不压缩，且按约定顺序丢弃低优先级素材。
- 无冲突不产生 interrupt；有冲突先发 artifact，再发 `agent_artifact_review` interrupt。
- 三种冲突审批均可通过 Command 恢复；handler 只接收冲突 chunks。
- 冲突说明拼接到对应 chunk content 开头；数据库没有额外 conflict resolution 字段或表。

### 10.5 持久化、事件和 debug

- 候选和最终素材写入同一 artifact revision；断线后可重放同一冲突卡。
- debug 关闭时不构造/返回完整工具参数和原始搜索结果；debug 开启时 trace 能看到全部新增 LLM/tool 调用。
- 网络工具发送通用 `agent.activity`，不新增 `wechat.*` 协议事件。
- 素材调研完成后总是发送一个 `stage=material_sources` 的 `agent.artifact`，其 content 只能是去重后的字符串列表。
- 用户文档来源只显示 `orig_chunks[0].title` 且不显示页码；全部网络 material kind 跨类别汇总合法 `orig_chunks[].ref` URL。
- debug 关闭时来源列表仍可见，但完整 material library artifact 不得出现；debug 开启时两张卡可以同时正确展示。
- 新增节点均有有限超时、重试和 degraded/fail 状态，没有无限循环。

### 10.6 真实验收样例

1. 严肃主题 + 用户文档：出现 factual 搜索，不误用 popular culture。
2. 热梗主题 + 无相关文档：overview 能解释主题，popular culture 能返回近期表达，最终完成文章。
3. 文档与网络对营业额给出不同数字：冲突卡展示双方 path，三种审批均能完成。
4. 文档和事实网络素材都很长：只对非 raw 文档和 factual 逐 chunk 压缩；creative_reference 与
   popular_culture 使用 summary，resource 不压缩；仍超预算时按顺序删除低优先级素材。
5. 网络超时或 429：对应分支 degraded，整轮不崩溃。
6. 冲突 interrupt 时断开 SSE 后输入“继续”：重放同一卡；cancel 后输入修改意见：新 revision 由 orchestrator 路由。
7. debug true/false 分别检查事件、trace payload 和性能边界。
8. overview 搜到两个同名候选主题：输出分别介绍两个主题，HITL 选项能够让用户明确选择。
9. material library 同时包含多页用户文档、重复文档素材和多类重复 URL：来源 artifact 只返回一次文档名和一次 URL，不泄露页码或 chunk content。

## 11. 容易遗漏的修改点

- `app/graph/state.py`：只新增 research/web/conflict 的小型控制字段，不写 material chunks。
- `app/llm/schemas.py`、`app/llm/inputs.py`：所有新字段有中文 description，动态 schema 输出还要做代码校验。
- `app/prompts/system.py`：overview 非素材、kind 用途、raw 不压缩、事实/创作边界必须同时写在相关节点提示词中。
- `app/prompts/system.py`：已有提示词只做增量补充；所有新增 LLM 节点提示词必须有清晰的 `# 示例` 分区。
- `app/llm/ark_responses.py`：除文本 delta 外，还要解析 web_search_call added/searching/completed/done，并取实际 query 和 annotations。
- `app/events/normalizer.py`、`responses.py`：继续使用跨 Agent 通用事件，不引入微信专用事件名。
- `app/debug/trace.py`：新增调用不能覆盖旧 trace；大小上限和 debug 双开关继续生效。
- `app/artifacts/repository.py`：候选、等待冲突和恢复结果均更新同一 revision，不能误建新 revision。
- `.env.example`、根环境变量文档和部署脚本：同步联网开关、预算、超时和并发配置。
- `dev/static/app.js`、`index.html`、`styles.css`：同步 about/target 文案、网络工具卡、冲突卡、来源列表卡和 trace；保留 debug 原始素材卡，并保持跨 run 隔离、折叠状态与滚动行为。
- `docs/api.md`、`frontend-integration.md`：补充冲突 HITL 表单、联网工具事件、素材来源 artifact 的字符串数组契约和 debug 裁剪规则。

后续实现严格以本文为基线分阶段推进，避免旧文档研读路径和新素材路径同时形成两套不一致的素材语义。
