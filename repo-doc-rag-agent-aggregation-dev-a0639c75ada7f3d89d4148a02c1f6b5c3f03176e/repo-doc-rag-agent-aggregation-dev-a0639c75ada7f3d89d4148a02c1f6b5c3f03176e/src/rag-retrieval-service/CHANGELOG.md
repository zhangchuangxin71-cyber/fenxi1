# Changelog

## 2026-07-31

- scope 元信息列举数量达到当前全部文档时，hint 只说明当前文档数和已经全部列举，不再把
  工具默认列举数量表述成用户请求但不存在的额外文档。
- `/rag/v1/documents/meta` 支持通过 `temp_doc_ids` 批量读取临时文档元信息；正式文档优先
  处理重复 ID，正式和临时文档均只按 `user_id + kb_id` 隔离，不再校验上传 session。
- 检索 scope、page、node 和 metadata 查询移除 `session_id` 过滤；该字段仅作为兼容的上游
  会话记录保留。
- `max_return_tokens` 的静态 schema 上限改为由运行时 `RAG_MAX_RETURN_TOKENS` 统一校验；
  doubao-2.0-lite 环境模板默认使用 `180000`，更换模型时无需修改接口代码。

## 2026-07-30

- merge 对 page、scope 和 direct 等所有候选统一按完全相同的 content 严格去重，并合并
  group、questions、document IDs 和去重后的 hint；同一 page 定位出现冲突正文仍直接报错。
- page 类型检索结果新增顶层 `page_number`，下游无需从不稳定的扩展元信息或 path 中解析页码。
- 单文档请求的非 scope group 直接接受范围内唯一文档，跳过 keyword 预筛和多文档路由，
  避免“当前唯一文档”等相对指代因缺少文档名关键词被预筛误杀。
- `/rag/v1/retrieve.query` 新增 `string[]` 输入；数组表示调用方已利用对话历史完成指代消解、
  子问题拆分和必要改写，字符串调用保持兼容。
- 新增默认 `robust` 分类策略：先运行 scope 二分类，再对 non-scope 并发运行 direct、focused
  二分类和统一目标文档分组，代码按固定优先级物化原四类 group。
- 二分类使用豆包已实测兼容的运行时动态 strict JSON Schema，强制每个代码 query ref 都有
  boolean 决策；目标文档分组要求 refs 形成 exact partition，所有 group/query ID 仍由代码生成。
- 保留 `fast` 单次分类策略，通过 `RAG_QUERY_CLASSIFICATION_STRATEGY=fast|robust` 在进程启动时切换。
- robust 的 direct/focused 失败保守进入 broad，目标文档分组失败按每 query 独立规则降级；
  拆分或 scope 分类失败保持关键失败，不静默切回 fast。
- 新增可重复运行的 fast/robust 真实豆包验收脚本和报告；400 次分类运行中 robust 的
  子问题保留、分类、文档分组和路由参数指标均为 100%，strict schema 与 fallback 均无失败。
- debug 开启时，robust 模式新增 `classification_trace`，按阶段记录内部改写、二分类、目标
  文档分组和最终 group；fast 模式及关闭 debug 的请求不额外收集该数据。
- 新增 12 个口语化复合 query 的全检索接口对比脚本，覆盖 scope/direct/focused/broad 混合、
  目标文档和 focused 原文锚点检查；脚本会自动启动缺失的本地 fast/robust 临时服务，逐案例
  输出具体错误，并只回收自己启动的进程。

## 2026-07-24

- 从 legacy 流程重构为 LangGraph page-first 检索：node 仅内部导航，公开正文 chunk 统一
  返回 page 原文，消除 node/page 竞争和固定候选 chunk 截断。
- 引入 `routed_focused`、`routed_broad`、`routed_direct`、`scope_direct` 四类 group，
  支持一个复合 query 拆分后并行进入不同处理节点。
- Focused Search 改为每文档独立 tree search，并通过 `scan_node_tree` 和 page inspection
  定位最终 page；多文档路由和 page inspection 使用 token 预算与 LPT 分组。
- 多文档路由增加文档名 + description keyword 硬预筛；阈值为 0 时可关闭预筛做评测。
- 多文档路由和 page inspection 改用稀疏结构化选择，只输出 `accept/possible`，遗漏项由
  代码确定为 `reject`；未知或重复引用仍触发批次规则 fallback。
- `top_k` 改为全局软目标，新增 `max_return_tokens` 硬预算、coverage 和显式 warning；
  极端预算不足时允许返回连续原文截断并标记 `content_truncated=true`。
- 增加进程内全局 LLM 并发池、单请求并发上限、队列、timeout、熔断和规则 fallback；
  所有模型调用显式关闭 thinking。
- 增加无 LangSmith 的详细 debug trace，记录分类、路由、tree navigation、工具调用、
  LPT 批次、provider/queue 耗时与降级原因；服务端与请求双开关任一关闭时不采集。
- Scope Access 元信息工具支持模型指定列举篇数，默认 4，并对请求 scope 做上限保护。
- 元信息 chunk 不再暴露内部 doc ID；所有类型的最终 chunk 统一渲染人类可读 hint。
- `session_id` 保留为兼容字段，不作为临时文档访问作用域。
- 健康接口保持 legacy 契约：`/healthz` 检查进程，`/readyz` 检查数据库。
- `semantic` 与 `hybrid` 使用新图；全程无 LLM 的 `keyword` 模式保留扩展点，当前未实现。
