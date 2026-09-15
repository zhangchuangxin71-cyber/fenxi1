# 鲁棒 Query 分类方案与实现计划

本文定义检索服务新版鲁棒分类器的完整设计和实现边界。本文只改造
`classify_query` 这一既有 LangGraph 节点的内部策略，不改变 keyword 预筛、多文档路由、
`core_access`、预算控制、broad retrieval 或 merge 的图结构与行为。

## 0. 实现状态

本文方案已于 2026-07-30 完成实现。默认使用 `robust`，并保留 `fast` 作为低延迟兼容策略。
自动化测试、动态 strict schema 和真实豆包对照验收均已执行；400 次 fast/robust 分类运行的
方法、精度、延迟、调用次数和 token 数据见
[真实豆包 API 验收报告](robust-query-classification-acceptance.md)。可重复执行的验收脚本为
`scripts/run_query_classification_acceptance.py`。

## 1. 目标与非目标

### 1.1 目标

新版分类器首先保证分类正确和子问题不丢失，其次才考虑调用次数、成本和延迟。它必须稳定处理：

- “你能看到哪些文档”“你能查到几篇文档”“你能查到哪些信息”等请求范围问题；
- 指定文档的页数、章节数、指定页、指定章节、目录、章节结构和数据库已有摘要；
- 指定少量文档中的事实、数值、日期、比例、流程和结论；
- 对指定少量文档做详细总结、深入解释或整体比较；
- 同一个请求中混合出现上述多类子问题；
- 跨文档比较问题，例如“A 与 B 的营业额谁更高”；
- 模糊但确实在询问文档内容的问题。此类问题应保守进入 `routed_broad`，让下游获得较多原文。

### 1.2 非目标

- 分类器不回答用户问题，不读取正文，不挑选 chunk。
- 分类器不生成任何业务 ID。所有 query ref 和 group ref 均由代码生成。
- 本次不把内部小分类器注册成新的 LangGraph node。
- 本次不修改分类节点之后的 LangGraph 节点、边或并发方式。
- 本次不删除现有速度优先分类器。

## 2. 已确认的核心决策

1. 保留现有单次 LLM 分类方案，命名为速度优先策略，例如
   `FastLLMClassificationStrategy`，不使用 `legacy` 命名。
2. 新增鲁棒策略，例如 `RobustLLMClassificationStrategy`。两种策略都实现同一个分类接口，
   由进程级配置选择，推荐配置名为：

   ```dotenv
   RAG_QUERY_CLASSIFICATION_STRATEGY=robust
   ```

   允许值只包含 `fast` 和 `robust`。线上鲁棒性优先时使用 `robust`；需要低延迟或进行对照实验时
   使用 `fast`。配置在进程启动时解析，不允许请求级切换。
3. `/rag/v1/retrieve` 的 `query` 支持 `str | list[str]`：
   - `str` 表示调用者没有完成子问题拆分和必要改写；
   - `list[str]` 表示调用者已依据完整对话历史完成拆分、指代消解和必要改写。
4. fast 模式收到 `str` 时保持现有行为；收到 `list[str]` 时由代码使用中文分号连接成一个字符串，
   再进入现有单次分类逻辑。这样旧分类器的处理过程不变。
5. robust 模式收到 `str` 时，先用一次专职 LLM 调用完成拆分与必要改写；收到 `list[str]` 时不再
   让检索服务改写，直接规范化并进入分类。
6. robust 模式先运行 `scope_classifier`。被判为 scope 的问题不再进入其他分类器和文档分组。
7. 对全部非 scope 问题，同时并行运行：
   - `direct_classifier`；
   - `focused_classifier`；
   - 目标文档统一分组与路由参数生成器。
8. 二分类结果由代码按以下固定优先级合并：

   ```python
   if is_scope:
       category = "scope_direct"
   elif is_direct:
       category = "routed_direct"
   elif is_focused:
       category = "routed_focused"
   else:
       category = "routed_broad"
   ```

9. 目标文档分组时先忽略 category，将所有非 scope 问题按目标文档统一分组并生成一次路由参数；
   分类结果与文档分组结果在代码中汇合后，再拆回 `routed_focused`、`routed_direct` 和
   `routed_broad`。
10. `scope_direct` 第一版不做目标文档分组和路由参数生成，每个 scope query 直接生成一个独立
    `QueryGroup`。
11. robust 策略整体仍然只是 LangGraph 中原有的 `classify_query` 节点。内部并发属于普通 Python
    协程并发，所有调用继续经过全局 `LLMGateway` 排队、限并发、统计和熔断。

## 3. API 输入契约

### 3.1 `query` 字段

目标 Pydantic 契约等价于：

```python
query: str | list[str] = Field(
    description=(
        "字符串表示尚未拆分的原始检索问题，检索服务会完成必要的拆分与改写；"
        "字符串列表表示调用者已利用对话历史完成指代消解、拆分与改写，每项必须是可独立检索的问题。"
    )
)
```

校验规则：

- `str` 去除首尾空白后不能为空，长度继续受当前 16000 字符上限约束。
- `list[str]` 不能为空；元素必须全是字符串，去除首尾空白后不能为空。
- 列表按原顺序去重，但不得静默丢弃有内容差异的问题。
- 列表全部问题的字符总量使用与字符串相同的 16000 上限。超过上限直接返回
  `QUERY_TOO_LONG_OR_INVALID`，不为极端输入增加二维分片。
- 不接受混合类型列表，也不把非字符串值隐式转换成字符串。

`list[str]` 代表的是调用方承诺，而不是检索服务能验证的事实。检索服务仍会检查空值、重复、数量
和总长度，但不会再次调用 LLM 改写列表项。调用方必须在能看见对话历史的位置完成指代消解；例如
“那这篇文档的报销流程是什么”必须改写成包含确切文档身份的独立问题。

### 3.2 内部规范化结果

不论输入是哪一种形式，进入二分类器前统一为：

```json
{
  "queries": [
    {"query_ref": "q_001", "question": "你能看到哪些文档？"},
    {"query_ref": "q_002", "question": "A 公司 2023 年营业收入是多少？"}
  ]
}
```

`query_ref` 在拆分、清理、去重和校验全部完成后按稳定顺序由代码生成。LLM 只能引用输入中已有的
ref，不能创建、修改或重新编号 ref。query ref 只服务于分类节点内部绑定，最终下游仍使用代码生成的
`group_ref`。

## 4. 策略接口与切换

两种策略应实现同一个协议。协议允许 query 为联合类型，但对外只返回现有 `ClassificationRun`，
从而不要求后续图节点理解新分类器的内部状态：

```python
class ClassificationStrategy(Protocol):
    async def classify(
        self,
        *,
        request_id: str,
        query: str | list[str],
        scope_document_count: int,
    ) -> ClassificationRun: ...
```

建议由小型工厂按配置创建策略：

```python
def build_classification_strategy(settings, gateway) -> ClassificationStrategy:
    if settings.rag_query_classification_strategy == "fast":
        return FastLLMClassificationStrategy(...)
    return RobustLLMClassificationStrategy(...)
```

不要在 `graph/builder.py` 中写 `if fast/robust`，也不要建立两套 graph。图只依赖统一协议，配置切换
只发生在容器装配阶段。以后新增第三种策略时，只新增实现和工厂分支，不修改图结构。

## 5. Robust 策略完整流程

```text
query: str | list[str]
        |
        v
normalize input
        |
        +-- str ------> rewrite_and_decompose (LLM) --+
        |                                             |
        +-- list -------------------------------------+
                                                      v
                                      validate + code generates query_ref
                                                      |
                                                      v
                                        scope_classifier (LLM)
                                           /                 \
                                      true refs          false refs
                                         |                   |
                              one scope group/query           +-----------------------+
                                                             |          |            |
                                                             v          v            v
                                                          direct     focused      group_and_route
                                                        classifier  classifier     parameters
                                                             \          |            /
                                                              +---------+-----------+
                                                                        |
                                                   code decision tree + cluster split
                                                                        |
                                                            materialize QueryGroup list
                                                                        |
                                                       existing keyword_prefilter node
```

如果输入是 `list[str]`，关键路径是两个串行等待轮次：scope 一轮，三个并发任务一轮。最多产生 4 个
LLM call。如果输入是 `str`，前面再增加一次拆分改写，因此是三个串行等待轮次、最多 5 个 LLM call。
“三个等待轮次”不能表述为“总共三次 LLM 调用”。

如果所有问题都被 scope classifier 选中，流程立即完成，不调用 direct、focused 或目标文档分组器。
因此 scope-only 的 `list[str]` 只需 1 个 call，scope-only 的 `str` 只需 2 个 call。

## 6. 字符串拆分与必要改写

### 6.1 职责

`rewrite_and_decompose` 只在 robust 模式且请求传入 `str` 时运行。它负责：

- 将多个明确子问题完整拆开，不能丢掉元信息问题；
- 把每一项改写成脱离其他子问题也能理解、能独立用于检索的问题；
- 将跨文档事实比较拆成每篇目标文档各自的事实查询；
- 保留用户的限定词、年份、单位、数量、指定页/章等约束；
- 在当前字符串本身包含足够信息时消除局部指代；
- 不回答问题，不分类，不生成路由参数，不生成 ref 或依赖关系。

检索服务看不到对话历史，因此无法可靠消解只存在于历史中的“这篇”“上一份”。如果传入的字符串
缺少指代对象，拆分器不得猜测。调用方应优先传入已经消解的 `list[str]`；无法消解时应在调用检索
服务前询问用户。

### 6.2 结构化输出

```json
{
  "queries": [
    "你能看到哪些文档？",
    "《中华人民共和国能源法》中规定的能源规划类型有哪些？",
    "《中华人民共和国能源法》的章节结构是什么？"
  ]
}
```

这里故意没有 query ref。代码先验证问题与原始字符串的保真度，再生成 ref，避免模型同时承担拆分
和引用维护。

### 6.3 提示词基本结构

```text
# 角色与任务
你是检索问题拆分与改写器；只拆分和改写，不分类、不检索、不回答。

# 拆分与改写规则
- 每项必须是从原 query 拆分并改写出的独立问题。
- 原 query 中每个明确问题都必须保留，特别是集合元信息问题。
- 跨文档比较拆成各目标文档的独立事实查询。
- 保留年份、数量、范围、页码、章节和比较对象。
- 不新增原问题没有的目标和条件。

# 禁止事项
- 不输出答案、类别、路由参数、ID、depends_on、reason 或 confidence。

# 示例
...

# 输出约束
严格遵守结构化输出，只返回 queries。
```

## 7. 二分类器设计

### 7.1 统一输出契约

豆包真实探测已证明可接受运行时动态生成的 `properties + required`。三个二分类器统一使用：

```json
{
  "decisions": {
    "q_001": true,
    "q_002": false,
    "q_003": true
  }
}
```

每次请求根据本批 query refs 动态构造 strict JSON Schema：

```json
{
  "type": "json_schema",
  "json_schema": {
    "name": "scope_decisions",
    "strict": true,
    "schema": {
      "type": "object",
      "properties": {
        "decisions": {
          "type": "object",
          "properties": {
            "q_001": {"type": "boolean"},
            "q_002": {"type": "boolean"}
          },
          "required": ["q_001", "q_002"],
          "additionalProperties": false
        }
      },
      "required": ["decisions"],
      "additionalProperties": false
    }
  }
}
```

选择该方案而不是 `{"true": ["q_001"]}` 的原因是：

- 每个 ref 与 boolean 局部绑定，不依赖与输入数组的位置对齐；
- 动态 `required` 强制每个问题都有显式判断；
- `additionalProperties=false` 禁止模型产生未知 ref；
- 代码可以严格验证键集合完全相等，无须从缺失项猜测模型是判为 false 还是遗漏了输出。

代码验证必须使用 exact-key 语义：根对象只能有 `decisions`；decision key 集合必须与输入 ref 集合
完全相等；值必须是 JSON boolean，不能接受 `0/1`、字符串或 Pydantic 的宽松转换。

### 7.2 所有二分类器的提示词结构

每个提示词都采用明确分区，不能只替换一句分类说明：

```text
# 角色与任务
# True 条件
# False 条件
# 边界与优先规则
# 输入说明
# 示例
# 禁止事项
# 输出约束
```

共同约束：只做当前一个二分类；逐项独立判断；不得遗漏或新增 ref；不输出理由、置信度、答案、
新问题或自然语言说明；严格使用动态结构化输出。

### 7.3 `scope_classifier`

输入：全部 queries。

True：问题关注当前请求范围内全部可见文档构成的集合，而不是少量具体文档，包括：

- 能看到/查到/访问多少篇文档；
- 能看到/查到哪些文档或哪些集合级信息；
- 列举当前范围文档；
- 利用数据库已有文档摘要对整个集合做简要概览、主题归纳；
- “这些文档主要讲了什么”“这批论文的研究方向有哪些”。

False：

- 命名或指向少量具体文档的问题，即使问的是页数或目录；
- 需要在全部文档中逐篇搜索正文事实的问题，例如“哪些公司营业收入超过一亿元”；
- 对少量具体文档进行详细总结、比较、解释或事实查找。

边界：判断的是“目标范围是否为请求集合本身”，不能只看是否出现“文档”“总结”“信息”等词。
scope 的优先级最高；一旦为 true，该 query 不进入后续分类和目标文档分组。

### 7.4 `direct_classifier`

输入：仅 non-scope queries，与 focused 和分组器并行运行。

True：问题针对少量具体文档，并且只需直接读取数据库已经保存的资源：页数、章节数、指定页、指定
章节、目录、章节结构、文档摘要。

False：

- 需要按语义在未知位置搜索事实、数值、日期、比例、流程或结论；
- 需要大范围原文的详细总结、深入解释、整体比较；
- 模糊地询问文档内容，无法仅靠某个数据库字段直接回答。

必须明确写入的边界：数据库摘要只支持简单概览。详细总结、深入解释和整体比较必须判为 false，
不能因为数据库存在摘要字段就判为 direct。

### 7.5 `focused_classifier`

输入：仅 non-scope queries，与 direct 和分组器并行运行。

True：答案通常由少量、位置未知但可通过语义找到的原文片段充分支持，例如事实、数值、日期、
比例、人物、明确流程、定义、处罚条款或局部结论。

False：

- 指定页、指定章节、目录、章节结构、页数等直接访问问题；
- 详细总结、深入解释、整体比较等需要大范围原文的问题；
- 确实在询问文档内容但目标粒度模糊，无法判断少量局部片段足够的问题。

direct 和 focused 并发执行，可能产生冲突。代码决策树中 `is_direct` 优先于 `is_focused`，因此明确
直接访问的问题不会误入 focused。对于两个分类器都判 false 的非 scope query，固定进入
`routed_broad`；这是已确认的保守 fallback。

## 8. 目标文档统一分组与路由参数

### 8.1 输入和职责

分组器接收全部 non-scope queries，但不接收、不判断 category。它只完成两件事：

1. 将询问同一目标文档或同一组目标文档的问题放进同一个 document cluster；
2. 为每个 cluster 生成一套共享的 `target_docs_description` 和 `target_docs_keywords`。

例如：

```text
q_001: A 公司营业额是多少？
q_002: A 公司年报第 12 页是什么？
q_003: 《XXX 法》中对 XXX 行为的处罚是什么？
```

分组器输出：

```json
{
  "document_groups": [
    {
      "query_refs": ["q_001", "q_002"],
      "target_docs_description": "A 公司的年度报告或财务报告",
      "target_docs_keywords": ["A 公司", "年度报告", "年报", "财务报告"]
    },
    {
      "query_refs": ["q_003"],
      "target_docs_description": "规定 XXX 行为及其处罚的《XXX 法》",
      "target_docs_keywords": ["XXX 法"]
    }
  ]
}
```

分组器不输出 `group_id`。`query_refs` 的 item schema 使用输入 ref 的 enum；代码要求所有 non-scope
refs 恰好出现一次，不允许遗漏、重复或未知 ref。

### 8.2 路由参数规则

- `target_docs_description` 只描述目标文档身份、名称、别名、主题或文档类型；
- `target_docs_keywords` 只保留可能出现在文档名或数据库摘要中的身份锚点；
- 不把待查答案、数值、页码、章节位置、执行动作或泛化问题词作为关键词；
- 同一目标 cluster 的路由参数只生成一次。

### 8.3 按 category 拆回现有 `QueryGroup`

当 direct/focused 结果和 document clusters 都返回后，代码执行固定决策树得到每个 query 的
category。随后对每个 document cluster 按 category 分桶：

```text
document cluster A: [q_001 focused, q_002 direct]
    -> routed_focused group: [q_001] + cluster A 路由参数
    -> routed_direct group:  [q_002] + cluster A 路由参数
```

同一个 cluster 中属于同一 category 的 queries 保持在一个 `QueryGroup`；不同 category 必须拆成
不同 `QueryGroup`。拆出的多个组复制同一套路由参数，再由代码按稳定顺序生成 `g0001` 等
`group_ref`。

现有 `multi_document_route` 会重新收集所有非 scope `QueryGroup` 的路由参数放入同一批路由任务，
因此分类阶段按 category 拆开不会阻止后续路由器同时处理这些共享目标。这里的“共享”是语义和值
相同，不要求多个 Pydantic 对象持有同一个可变列表引用。

## 9. Scope group 与当前执行方式

第一版 robust 分类器为每个 scope query 直接生成独立 `QueryGroup`，不生成文档路由参数。这样一个
混合 query 中的“列举文档”和“简要概览这些文档”可分别由 scope tool planner 选择
`get_docs_metainfo` 与 `get_docs_description`，不会因为强行合组而只能调用一个工具。

当前 `core_access` 的真实行为是：

- 用普通 `for` 循环按顺序执行 scope groups；
- 每个 scope group 的 planner 必须且只能选择一个工具；
- scope groups 之间当前没有 `asyncio.gather`。

这一行为可接受，本次不修改。

## 10. 现有 routed 分支的并发事实

静态检查 `app/graph/builder.py` 与 broad retrieval 实现后的结论如下：

| 类别 | 当前是否 fan-out 并行 | 真实执行方式 |
|---|---:|---|
| `routed_focused` | 是 | 在 `core_access` 中按 `group × accept document` 建任务并 `asyncio.gather` |
| `routed_direct` | 否 | 在 `core_access` 中按 group 顺序 `await direct.run` |
| `routed_broad` | 否 | 预算检查后的单个纯规则 LangGraph node，内部顺序构造各 group/doc 的页面计划 |

此外，`core_access` 先顺序完成 scope，再顺序完成 direct，之后才并发运行 focused；它们三者之间也
不是平行 fan-out。所有 focused LLM call 即使由 `asyncio.gather` 创建，仍然受进程级全局
`LLMGateway` 的公平排队和并发上限约束。

这张表只是记录当前事实。鲁棒分类器实施不得借机调整这些下游行为。

## 11. 错误处理与保守降级

本节是为了让后续实现可以闭环而给出的实现级默认策略，不改变前文已经确认的分类语义与图结构。
如果实现阶段要改变这些错误语义，应先单独评审，不能隐藏在代码细节中。

鲁棒模式不能在任一内部调用失败后无提示地整条切回 fast 模式，否则线上观察到的实际策略会与配置
不符，也会重新引入复合任务丢问题的风险。第一版采用以下明确边界：

- `str` 拆分改写失败或输出无法通过保真校验：分类节点失败，返回可观测的 503，不猜测拆分结果。
  调用者可改用已拆分的 `list[str]` 重试。
- scope classifier 失败或动态键不完整：分类节点失败，返回 503。scope 是最高优先级，不能把缺失
  判定默认成 false，否则会再次丢失集合元信息问题。
- direct classifier 失败：该批 non-scope queries 的 `is_direct` 保守记为 false，并产生降级 warning。
- focused classifier 失败：该批 non-scope queries 的 `is_focused` 保守记为 false，并产生降级 warning。
- direct/focused 同时失败时，非 scope queries 进入 `routed_broad`，符合“模糊问题多返回原文”的
  既定原则。
- 目标文档分组失败或不能形成 exact partition：使用确定性降级，每个 query 单独形成 document
  cluster；`target_docs_description` 使用问题原文，关键词使用现有确定性 tokenizer 提取。产生 warning，
  不静默合并目标不同的问题。
- 任何未被输出契约列出的 query ref、重复 ref、缺失 ref、非 boolean 值都视为无效响应，不能由
  Pydantic 宽松转换“修复”。

这些 warning 和内部调用仍接入现有 debug trace；`RAG_DEBUG_ENABLED=false` 时不得为记录完整 prompt
或 response 产生额外响应体开销。

## 12. 豆包动态 Strict Schema 探测结论

### 12.1 探测实现

脚本：`scripts/probe_dynamic_classification_schema.py`

脚本使用检索项目当前 `.env`，通过项目现有 `OpenAICompatibleProvider` 请求豆包，不直接绕过项目
适配层。API key 永远只显示 `<redacted>`。运行命令：

```bash
cd /workspace/repo-doc-rag-agent-aggregation/src/rag-retrieval-service
.venv/bin/python scripts/probe_dynamic_classification_schema.py
```

探测使用两套运行时动态键：

- scope：`q_001`、`q_007`、`q_042`；
- direct：`q_105`、`q_219`。

两套 schema 都把各自 ref 动态写入 `properties` 和 `required`，并禁止额外属性。脚本随后在本地再次
验证 exact-key 和真正 boolean 类型。

### 12.2 2026-07-30 真实结果

测试模型：`doubao-seed-2-1-pro-260628`  
接口：火山方舟 OpenAI-compatible `/api/v3`

```text
[PASS] dynamic_scope_keys: 3 个动态键全部返回，结构和语义均正确，total_tokens=490
[PASS] dynamic_direct_keys: 2 个不同动态键全部返回，结构和语义均正确，total_tokens=470
[PASS] Doubao accepted both runtime-generated strict schemas.
```

第一次 direct 语义探测曾把“详细总结”判为 direct。该次返回的动态 schema 结构仍然完全合法；原因是
测试提示词没有明确“数据库摘要只能支持简单概览”。补充 direct/broad 边界后再次运行，语义判定通过。
这说明动态 schema 能力可用，也说明三个正式二分类器的提示词必须保留本文定义的边界段落。

因此正式二分类输出采用动态 decision object 方案，不采用稀疏 true-ref 列表，也不采用与输入等长的
boolean 数组。

## 13. 代码组织要求

建议保持分类目录内聚，不把新实现继续堆入已经较长的 `strategies.py`：

```text
app/workflows/classification/
├── __init__.py
├── models.py                  # 现有最终 QueryGroup + 新内部数据模型
├── contracts.py               # 动态 schema、exact-key 校验
├── prompts.py                 # 拆分、三个二分类器、统一分组提示词
├── fast_strategy.py           # 从现有代码最小迁移/重命名
├── robust_strategy.py         # robust 内部编排，不是 LangGraph node
├── rule_strategy.py           # 现有规则 fallback，避免复制
└── factory.py                 # 进程级策略选择
```

如果为了降低改动风险，第一步保留 `strategies.py` 中的 fast/rule 实现也可以；但 robust 的多个 prompt、
schema 和内部模型不能全部堆进同一文件。迁移旧代码时必须保持现有 fast 行为的回归测试，不做无关重构。

内部建议使用不可变数据对象表达：

```python
@dataclass(frozen=True, slots=True)
class ReferencedQuery:
    query_ref: str
    question: str

@dataclass(frozen=True, slots=True)
class QueryFacets:
    query_ref: str
    is_scope: bool
    is_direct: bool = False
    is_focused: bool = False

@dataclass(frozen=True, slots=True)
class DocumentCluster:
    query_refs: tuple[str, ...]
    target_docs_description: str
    target_docs_keywords: tuple[str, ...]
```

不要把二分类器的临时结果加入全局 `RetrievalGraphState`；robust strategy 内部组装成现有
`ClassificationRun` 后一次性返回。debug trace 可以记录内部 phase，但不能要求图理解内部状态。

## 14. 分阶段实现计划

### 阶段 1：输入联合类型与规范化

涉及文件：

- `app/api/schemas.py`
- API schema 单元测试和 OpenAPI 契约测试

步骤：

1. 先增加失败测试，覆盖 `str`、合法 `list[str]`、空列表、空元素、混合类型、重复项和总长度超限。
2. 将 `RetrieveRequest.query` 改为明确的 `str | list[str]` 并补全 Field description。
3. 提供一个唯一的规范化 helper，避免 service、trace 和 classifier 各自拼接。
4. 确认 debug input 能安全展示两种 query 类型，其他请求字段契约不变。

### 阶段 2：动态结构化输出契约

涉及文件：

- `app/workflows/classification/contracts.py`
- `tests/unit/test_classification_contracts.py`
- 已完成的 `scripts/probe_dynamic_classification_schema.py`

步骤：

1. 将探测脚本中已验证的纯 schema builder 和 exact-key validator 提炼到业务模块；脚本改为复用业务
   实现，不能长期保留两份相似逻辑。
2. 用测试固定 `required`、`additionalProperties=false`、动态键和 strict boolean 行为。
3. 测试未知、遗漏、重复和类型错误均明确失败。

### 阶段 3：Fast 策略封装与配置工厂

涉及文件：

- `app/workflows/classification/strategies.py` 或拆分后的 `fast_strategy.py`
- `app/workflows/classification/factory.py`
- `app/config/settings.py`
- `app/container.py`
- `.env.example` 及聚合项目环境变量文档

步骤：

1. 用 golden tests 固定现有 `str` 分类 prompt、schema、修正规则和 fallback 行为。
2. 将现有 LLM 分类器改名为速度优先语义，不出现 `legacy`。
3. fast 收到 list 时只由代码按稳定顺序用 `；` 拼接，然后走原逻辑。
4. 增加仅有 `fast|robust` 两个值的配置校验和策略工厂。
5. `graph/builder.py` 继续只调用统一 `classifier.classify`，不得出现策略分支。

### 阶段 4：拆分改写器

涉及文件：

- `app/workflows/classification/prompts.py`
- `app/workflows/classification/robust_strategy.py`
- 拆分改写单元测试

步骤：

1. 先用 fake gateway 测试：多个子问题全部保留、scope 问题不丢、跨文档比较拆开、约束不丢、
   LLM 不生成 ID。
2. 实现 strict `{"queries": [...]}` 输出和保真校验。
3. 只为 str 调用；list 路径断言不会触发改写 LLM。
4. 校验后由代码生成稳定 query refs。

### 阶段 5：Scope-first 与三个并发任务

涉及文件：

- `app/workflows/classification/robust_strategy.py`
- `app/workflows/classification/contracts.py`
- `app/workflows/classification/prompts.py`
- robust 编排与并发测试

步骤：

1. 实现 scope classifier，并测试被选中的 refs 不会传给任何后续任务。
2. 对 non-scope queries 用一个 `asyncio.gather` 同时启动 direct、focused、统一分组三个协程。
3. 所有协程调用同一个全局 `LLMGateway`，不得自己创建 provider、semaphore 或线程池。
4. 用事件屏障测试三个任务确实重叠运行，而不是仅在代码中依次 await。
5. 按固定决策树合并 facets，不能使用置信度或额外 LLM 仲裁。

### 阶段 6：统一分组、拆回类别并物化

步骤：

1. 用测试覆盖同一文档跨 focused/direct、同一文档同类别、多文档不同 cluster、缺失 ref、重复 ref。
2. 分组 LLM 不接收 category，不输出 group ID。
3. exact partition 校验通过后，按 cluster 和 category 拆分。
4. 复制共享路由参数，由代码生成稳定 group refs 和 ordinal。
5. scope 每 query 一个 group，无路由参数。
6. 输出必须继续是现有 `ClassificationRun` 和 `QueryGroup`，后续节点无需修改。

### 阶段 7：调用方与文档

知识库问答能够读取对话历史，应优先升级为向检索服务传 `list[str]`，并保证：指代消解、跨文档事实
问题拆分、多问题全部保留。报告服务可以暂时继续传 `str`，由 robust 策略承担拆分。

同步更新：

- 检索 API 文档中 `query` 的联合类型和调用方责任；
- 知识库问答的 query 改写结构化输出；
- 环境变量文档和 changelog；
- debug 面板兼容原始 query 为数组的展示。

## 15. 严格验收边界

### 15.1 自动化测试

必须通过以下测试后才能声明实现完成：

- API 接受合法 str/list，拒绝空值、混合类型和超限输入；
- fast + str 的请求、prompt、LLM call 数和最终 groups 与修改前一致；
- fast + list 仅发生代码拼接，并仍只有一次分类 LLM call；
- robust + list 不调用拆分器；robust + str 只调用一次拆分器；
- scope classifier 始终先完成，scope refs 不进入后三个任务；
- non-scope 的 direct、focused、分组三个任务并发启动；
- 每个动态二分类结果必须覆盖全部输入 refs，且没有额外 ref；
- 固定决策树的所有 8 种 boolean 组合结果正确；
- 两个 classifier 都为 false 的 non-scope query 进入 broad；
- 文档 cluster 跨 category 后能正确拆回不同 `QueryGroup`，路由参数一致；
- 每个 query 恰好进入一个最终 group，不丢失、不重复；
- scope 每 query 一个 group，且无路由参数；
- LangGraph node 和 edge 集合与改造前一致；不得新增二分类 LangGraph node；
- keyword、router、direct、focused、broad、merge 的既有回归测试全部通过；
- debug 关闭时不会因为 robust 内部 trace 保存完整 prompt/response。

建议命令：

```bash
cd /workspace/repo-doc-rag-agent-aggregation/src/rag-retrieval-service
.venv/bin/ruff check app tests scripts
.venv/bin/pytest tests/unit -q
.venv/bin/pytest tests/integration -q
```

### 15.2 真实豆包 API 验收

使用真实线上同款豆包模型，至少覆盖并重复以下问题：

- “你能看到哪些文档？”
- “你能查到几篇文档？”
- “你能查到哪些信息？”
- “这些文档主要讲了什么？”
- “中国铁物 2023 年半年度报告文件有多少页？”
- “详细总结某文档。”
- “A 公司与 B 公司营业额谁更高？”
- “你能看到哪些文档？《中华人民共和国能源法》中规定的能源规划类型有哪些？这个文件的章节结构是什么？”
- 一个模糊的指定文档内容问题，确认落入 broad。

每个关键边界至少重复 20 次，报告：

- 子问题保存率；
- 四分类准确率；
- 文档 cluster 准确率；
- 路由参数可用率；
- schema 校验失败率；
- p50/p95 分类耗时；
- 平均和 p95 LLM call 数；
- prompt/completion/total tokens。

对同一测试集同时运行 fast 和 robust。robust 的上线门槛不是“平均准确率略高”，而是已知 scope、direct、
focused、broad 边界错误显著下降，复合问题不再丢子问题。性能报告必须区分：

- str 与 list 两种输入；
- scope-only 与包含 non-scope 的请求；
- 串行等待轮次和总 LLM call 数。

### 15.3 最小修改审查

实现完成后做一次显式 diff 审查：

- `build_retrieval_graph` 的 node/edge 不因本功能变化；
- 除 query 联合类型的必要适配外，不修改分类下游业务逻辑；
- 不复制 LLM provider、gateway、熔断、并发控制或 trace 基础设施；
- prompt、schema、编排、模型和工厂职责分离，没有一个超长文件容纳全部实现；
- 没有 LLM 生成的 query ID、group ID 或 depends_on；
- 新增策略可以只通过工厂和环境变量接入，未来第三种策略不需要修改 LangGraph。

满足以上边界后，才能开始以 `robust` 作为线上鲁棒性优先配置，并保留 `fast` 作为低延迟选择和
基线对照。
