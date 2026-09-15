# RAG 检索服务 API

本文面向直接调用 `rag-retrieval-service` 的内部服务。检索服务根据 query 规划检索流程，
返回数据库中的 page 原文或工具生成的元信息 chunk；它不直接回答用户问题，也不会改写、
总结或润色检索到的正文。

## 基本约定

- 生产默认地址：`http://rag-retrieval-service:8120`
- 主检索接口：`POST /rag/v1/retrieve`
- 文档元信息：`POST /rag/v1/documents/meta`
- 独立多文档路由：`POST /rag/v1/documents/route`
- 文档原始解析结果：`POST /rag/v1/documents/raw`
- MinerU raw 修复状态：`GET /rag/v1/documents/raw/status`
- 存活检查：`GET /healthz`
- 就绪检查：`GET /readyz`
- 请求和响应类型：`application/json`
- 检索接口是同步 HTTP；缺失 MinerU raw 时使用 HTTP 202 + 状态轮询，不支持 SSE。

## 安全边界

检索服务本身不校验 API Key，必须部署在可信内网或认证网关之后。`user_id`、`kb_id` 和
`session_id` 仍保留在请求契约中，用于兼容上游、日志和 RPM 限流，但不参与数据库文档
可见性判断。数据库读取只按请求中的 `doc_ids` 与 `temp_doc_ids` 查找文档；两类 ID 实际
会合并为一个 doc_id 集合。

不要把该服务直接暴露到不可信公网。调用方必须在自己的权限层完成 doc_id 隔离；检索服务
不会根据 `user_id` 或 `kb_id` 替调用方做权限判断。

## 主检索接口

### `POST /rag/v1/retrieve`

最小示例：

```bash
curl http://127.0.0.1:8120/rag/v1/retrieve \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-1",
    "kb_id": "kb-1",
    "query": "这两份年报的营业收入分别是多少？",
    "doc_ids": ["doc-a", "doc-b"]
  }'
```

### 请求字段

| 字段 | 类型 | 必填 | 默认值 | 含义 |
|---|---|---:|---|---|
| `user_id` | string | 是 | 无 | 兼容字段和进程内 RPM 限流身份，1–256 字符；不参与 SQL 文档过滤。 |
| `kb_id` | string | 是 | 无 | 兼容字段，1–256 字符；不参与 SQL 文档过滤。 |
| `query` | string 或 string[] | 是 | 无 | 字符串表示尚未拆分的完整问题；字符串数组表示调用者已利用对话历史完成指代消解、拆分和必要改写，每项必须可独立检索。总长度不超过 16000 字符。 |
| `session_id` | string/null | 否 | `null` | 上游会话记录字段，最长 512 字符；为兼容保留，不参与文档可见性判断。 |
| `doc_ids` | string[] | 否 | `[]` | 正式文档范围。重复 ID 会去重。生产默认最多 100 个总文档 ID。 |
| `temp_doc_ids` | string[] | 否 | `[]` | 临时文档范围。与 `doc_ids` 重复的 ID 按正式文档处理。 |
| `top_k` | integer | 否 | `5` | 全局软目标，接口范围 1–100。不会强制截断 direct 结果或 broad 完整性。 |
| `max_return_tokens` | integer/null | 否 | `8192` | 最终 chunks 的硬 token 预算。下限为 128，上限由 `RAG_MAX_RETURN_TOKENS` 决定；当前 doubao-2.0-lite 部署默认上限为 180000。 |
| `search_mode` | string | 否 | `hybrid` | `hybrid`、`semantic` 或 `keyword`，当前语义见下文。 |
| `options` | object | 否 | 见下表 | 文档元信息、debug 和 coverage 开关。 |

ID 列表中的空白项会被移除，重复项按首次出现顺序去重。若请求中的文档不存在或状态未
就绪，返回 404，不会静默检索其他文档。一个 doc_id 即使存在多条 document binding，
服务也只选择一条确定性的 binding 元数据，不会重复返回同一文档。

能够读取完整对话历史的调用方应优先传 `string[]`。例如“A 与 B 的营业额谁高”应传为
`["A 的营业额是多少？", "B 的营业额是多少？"]`；“你能看到哪些文档”与其他问题混合时
也必须作为独立数组项保留。检索服务看不到对话历史，无法可靠猜测“这篇文档”指向谁。
调用方只传字符串时，`robust` 分类策略会额外调用一次模型完成拆分与必要改写；`fast`
策略则保持原单次分类行为。

请求范围只有一篇文档时，所有非 `scope_direct` group 由代码直接接受该唯一文档，并绕过
keyword 预筛和多文档路由；后续仍按原 category 执行 focused、direct 或 broad 检索。

### `options` 字段

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `include_document_meta` | boolean | `true` | 是否在每个文档 chunk 中附加文档类型、摘要、页数等元信息。 |
| `include_debug` | boolean | `false` | 请求详细 trace；服务端还必须启用 `RAG_DEBUG_ENABLED=true`。 |
| `ensure_document_coverage` | boolean | `false` | merge 时优先尝试保留目标文档覆盖；仍受硬 token 预算约束。 |

### `search_mode`

| 值 | 当前行为 |
|---|---|
| `semantic` | 使用当前 LangGraph agentic 检索流程。 |
| `hybrid` | 当前也进入 agentic 语义流程，并保留未来组合规则检索的契约。响应中的 `usage.actual_mode` 为实际执行模式。 |
| `keyword` | 已预留但尚未实现，当前返回 HTTP 501 `MODE_NOT_IMPLEMENTED`，全流程不会调用 LLM 的版本将在后续实现。 |

不要因为 `hybrid` 名称假设当前一定执行一条独立的纯 SQL keyword 分支；应以
`usage.actual_mode` 为准。

### 完整请求示例

```json
{
  "user_id": "eval-user",
  "kb_id": "eval-rag-kb",
  "query": [
    "你能看到哪些文档？",
    "《中华人民共和国能源法》规定的能源规划类型有哪些？",
    "《中华人民共和国能源法》的章节结构是什么？"
  ],
  "session_id": null,
  "doc_ids": [
    "document-id-1",
    "document-id-2"
  ],
  "temp_doc_ids": [],
  "top_k": 8,
  "max_return_tokens": 16384,
  "search_mode": "hybrid",
  "options": {
    "include_document_meta": true,
    "include_debug": false,
    "ensure_document_coverage": false
  }
}
```

## 检索响应

成功请求返回 HTTP 200：

```json
{
  "chunks": [
    {
      "chunk_id": "document-id-1:page:12",
      "document_id": "document-id-1",
      "document_ids": ["document-id-1"],
      "document_name": "中华人民共和国能源法.pdf",
      "page_number": 12,
      "path": "page:12",
      "content": "能源规划包括……",
      "hint": "本 chunk 用于回答：能源规划类型有哪些？",
      "score": 42.0,
      "source_type": "page",
      "document_meta": {
        "doc_type": "pdf",
        "doc_description": "中华人民共和国能源法相关文件",
        "page_count": 36,
        "node_count": 14,
        "is_temporary": false
      },
      "chunk_meta": {}
    }
  ],
  "warnings": [],
  "coverage": {
    "complete": true,
    "truncated": false,
    "truncated_by": null,
    "covered_group_refs": ["g0001"],
    "uncovered_group_refs": [],
    "degraded_group_refs": []
  },
  "usage": {
    "candidate_document_count": 2,
    "inspected_node_count": 8,
    "inspected_page_count": 12,
    "returned_count": 1,
    "returned_tokens": 680,
    "tokenizer": "cl100k_base",
    "latency_ms": 4200,
    "actual_mode": "semantic",
    "llm_request_count": 4,
    "prompt_tokens": 3500,
    "completion_tokens": 260,
    "total_tokens": 3760
  },
  "debug": null
}
```

### `chunks`

| 字段 | 含义 |
|---|---|
| `chunk_id` | 当前响应内稳定的 chunk 标识。 |
| `document_id` | 单文档 chunk 的文档 ID；请求范围级元信息 chunk 可以为 `null`。 |
| `document_ids` | chunk 涉及的文档 ID 列表；集合级结果可能使用该字段。 |
| `document_name` | 文档名；集合级结果可以为 `null`。 |
| `page_number` | page chunk 的一基页码；非 page 或集合级结果为 `null`。 |
| `path` | 内容位置，例如 `page:12`、章节路径或 `request:scope`。 |
| `content` | 交给下游模型消费的原文或工具生成元信息，不做回答式润色。 |
| `hint` | 面向下游模型的可读提示，说明该 chunk 对应的问题、文档范围和截断情况。 |
| `score` | 规则或检索相关性分数；不适用时为 `null`。不要跨请求比较绝对值。 |
| `source_type` | `page`、`scope_metadata`、`document_overview` 或 `title_tree`。 |
| `document_meta` | 可选文档元信息；由 `include_document_meta` 控制。 |
| `chunk_meta` | 截断状态等扩展信息。调用方应容忍新增键。 |

正文检索最终只返回 page 原文，不返回内部 node chunk。node 的标题、summary 和页码范围只
用于检索服务内部导航。元信息、数据库已有文档摘要和标题树是工具生成 chunk，因此
`source_type` 可能不是 `page`。

若单个原子 chunk 超过硬预算，服务可以返回连续原文片段，并设置：

```json
{
  "chunk_meta": {
    "content_truncated": true,
    "original_content_tokens": 12000,
    "returned_content_tokens": 6000
  }
}
```

此时响应必须同时包含 warning。调用方应把 `hint` 和 `warnings` 一并交给下游模型或展示
给开发人员，不能把截断结果误认为完整全文。

### `warnings`

warning 表示请求返回了可用结果，但发生了降级、预算截断或覆盖不足：

```json
{
  "code": "BROAD_TRUNCATED_BY_BUDGET",
  "message": "broad evidence was truncated by the return token budget",
  "affected_group_refs": ["g0002"],
  "affected_document_ids": ["document-id-2"],
  "retryable": false
}
```

常见类别：

| warning code | 含义 |
|---|---|
| `CLASSIFICATION_RULE_FALLBACK` | `fast` 分类 LLM 失败，已使用规则分类。 |
| `DIRECT_CLASSIFIER_BROAD_FALLBACK` | robust direct 二分类失败，受影响问题保守进入 broad。 |
| `FOCUSED_CLASSIFIER_BROAD_FALLBACK` | robust focused 二分类失败，受影响问题保守进入 broad。 |
| `TARGET_DOCUMENT_GROUPING_RULE_FALLBACK` | robust 目标文档分组失败，已按每个问题独立生成规则路由参数。 |
| `DOCUMENT_ROUTING_BATCH_RULE_FALLBACK` | 某个文档路由批次失败，已使用规则结果。 |
| `TREE_NAVIGATION_RULE_FALLBACK` | 标题树导航失败，已降级到规则/page 检索。 |
| `KEYWORD_PREFILTER_EMPTY` | keyword 硬预筛没有为某组保留文档，可能造成召回缺失。 |
| `GROUP_EVIDENCE_UNAVAILABLE` | 某个问题组没有最终证据。 |
| `BROAD_TRUNCATED_BY_BUDGET` | broad 原文因 token 预算未完整返回。 |
| `ATOMIC_CHUNK_CONTENT_TRUNCATED` | 单个不可拆分 chunk 返回了连续截断片段。 |
| `DOCUMENT_COVERAGE_INCOMPLETE` | 请求的文档覆盖没有完全满足。 |
| `REQUEST_SOFT_DEADLINE_REACHED` | 到达软 deadline，返回已完成的部分结果。 |
| `REQUEST_HARD_DEADLINE_REACHED` | 到达硬 deadline，但仍有完整证据可返回。 |

调用方不能只检查 HTTP 200 和 `chunks`；至少还应记录 `coverage.complete` 和所有 warning。

### `coverage`

| 字段 | 含义 |
|---|---|
| `complete` | 当前检索计划要求的 group 是否都得到证据。 |
| `truncated` | 是否因预算、deadline 或失败发生截断。 |
| `truncated_by` | 截断原因，例如 token budget、deadline 或 failure。 |
| `covered_group_refs` | 已覆盖的内部问题组。 |
| `uncovered_group_refs` | 没有证据的问题组。 |
| `degraded_group_refs` | 使用 fallback 或部分失败的问题组。 |

`group_ref` 由服务代码生成，只用于关联同一响应中的 warning、coverage 和 debug。调用方
不需要生成、保存或在后续请求中回传它。

### `usage`

`usage` 用于延迟、成本和召回诊断：候选文档数、访问 node/page 数、返回 chunk/token 数、
实际模式、LLM 调用次数及 prompt/completion token 都在此汇总。它不是计费承诺；tokenizer
名称由服务返回。

### `debug`

只有以下两个条件同时成立才会返回详细 debug：

1. 服务端 `RAG_DEBUG_ENABLED=true`。
2. 请求 `options.include_debug=true`。

debug 可包含分类结果、文档路由、候选 chunk、节点耗时、LLM 结构化请求/响应和工具事件，
响应可能很大。robust 分类模式还会在 `classification_trace` 中按阶段记录规范化子问题、scope
判定、direct/focused 判定、目标文档分组和最终 group；fast 模式保持该列表为空。生产请求应
保持关闭。服务不使用 LangSmith；trace 由进程内收集器生成。

## 独立多文档路由接口

### `POST /rag/v1/documents/route`

仅根据数据库已有的文档名、类型和 `doc_description`，判断请求范围内的文档是否符合一组或
多组目标条件。该接口不执行 query 分类、不读取 page/node/原文，也不回答用户问题。它适合已经
自行构造路由条件、只需要复用检索服务多文档路由能力的内部 agent。

请求示例：

```json
{
  "user_id": "wechat-article-agent-dev-user",
  "kb_id": "wechat-article-agent-dev-kb",
  "session_id": null,
  "doc_ids": ["doc-1", "doc-2"],
  "temp_doc_ids": [],
  "criteria": [
    {
      "queries": [
        "哪些文档适合用于编写新能源汽车产业趋势文章？"
      ],
      "target_docs_description": "与新能源汽车产业、市场趋势和政策影响相关的文档",
      "target_docs_keywords": [
        "新能源汽车",
        "汽车产业",
        "市场趋势",
        "产业政策"
      ]
    }
  ],
  "keyword_prefilter": true
}
```

### 请求字段

| 字段 | 类型 | 必填 | 默认值 | 含义 |
|---|---|---:|---|---|
| `user_id` | string | 是 | 无 | 兼容字段和进程内 RPM 限流身份；不参与 SQL 文档过滤。 |
| `kb_id` | string | 是 | 无 | 兼容字段；不参与 SQL 文档过滤。 |
| `session_id` | string/null | 否 | `null` | 兼容的上游会话字段；不参与文档可见性判断。 |
| `doc_ids` | string[] | 否 | `[]` | 正式文档 ID。与 `temp_doc_ids` 合计至少一项，默认总上限为 100。 |
| `temp_doc_ids` | string[] | 否 | `[]` | 临时文档 ID。与 `doc_ids` 重复时按正式文档处理。 |
| `criteria` | object[] | 是 | 无 | 路由条件组，至少一组；响应中的 `groups[index]` 与这里保持相同顺序。 |
| `keyword_prefilter` | boolean | 否 | `false` | 是否先执行既有关键词硬预筛；关闭时所有请求范围内文档直接进入 LLM 路由。 |

每个 `criteria` 元素包含：

| 字段 | 类型 | 必填 | 含义 |
|---|---|---:|---|
| `queries` | string[] | 是 | 当前组需要路由文档的独立问题，至少一项。 |
| `target_docs_description` | string | 是 | 目标文档的整体语义描述。 |
| `target_docs_keywords` | string[] | 否 | 目标文档关键词；默认空数组。开启预筛时用于既有评分，空数组时预筛逻辑回退使用 `target_docs_description`；该字段始终会作为 LLM 路由上下文。 |

空白 ID 和文本项会被清理，重复项按首次出现顺序去重。任意请求文档不存在或状态不是
`ready` 时，接口整体返回 404，不会静默缩小文档范围。调用方负责保证所传 doc_id 可访问。

### 执行语义

- `keyword_prefilter=false`：不计算关键词分数，将全部文档以 `keyword_score=0` 交给既有
  `DocumentRoutingService`，由 LLM 完成筛选。
- `keyword_prefilter=true`：原样复用检索图的 `KeywordPrefilterService`，包括既有评分权重、
  阈值、身份锚点/scope IDF 二次预筛和最大候选数检查；接口不会另设一套评分策略。
- 某组经过预筛后没有候选时，该组直接返回全部文档为 `reject`，不为该组额外调用 LLM；若
  所有组都没有候选，`llm_request_count` 为 0。
- 所有 criteria 的固定提示内容先执行单窗口预算检查。文档摘要过长时沿用固定 token 前缀并
  返回 `DOCUMENT_DESCRIPTION_TRUNCATED_FOR_ROUTING` warning。
- 候选文档按照 token 成本通过 LPT 分组，各批次经共享 LLM Gateway 并行执行。模型只输出
  `accept/possible`；未输出的候选以及预筛排除的文档由代码补为 `reject`。
- 单个 LPT 批次失败或返回非法引用时，沿用既有确定性规则处理该批次，其他批次继续执行；
  HTTP 仍为 200，并返回 `DOCUMENT_ROUTING_BATCH_RULE_FALLBACK`、`degraded=true`。

`keyword_prefilter` 是该独立接口的请求级开关。`POST /retrieve` 的 LangGraph 流程仍始终进入
图内关键词预筛节点；`RAG_DOCUMENT_PREFILTER_THRESHOLD` 和
`RAG_DOCUMENT_PREFILTER_MAX_CANDIDATES` 是开启预筛时使用的服务级参数。

### 成功响应

```json
{
  "request_id": "route_7ea4f1c2-6d72-4e54-b7e8-8fa8e56dd8cb",
  "groups": [
    {
      "index": 0,
      "accept_doc_ids": ["doc-1"],
      "possible_doc_ids": ["doc-2"],
      "reject_doc_ids": [],
      "decision_source": "llm",
      "degraded": false,
      "warnings": []
    }
  ],
  "usage": {
    "candidate_document_count": 2,
    "llm_request_count": 1,
    "prompt_tokens": 1342,
    "completion_tokens": 41,
    "latency_ms": 863
  }
}
```

`decision_source` 的取值为：

| 值 | 含义 |
|---|---|
| `llm` | 当前组所有保留结果均来自 LLM；没有保留文档时也使用该值。 |
| `rule_fallback` | 当前组所有保留结果均来自失败批次的既有规则降级。 |
| `mixed` | 当前组同时包含 LLM 与规则降级产生的保留结果。 |

`candidate_document_count` 是本次成功装载的请求范围文档总数，不是每组经过关键词预筛后的
候选数。`llm_request_count` 是 LPT 分批后实际进入共享 Gateway 的调用数。

### 路由接口错误

| HTTP | error code | retryable | 含义 |
|---:|---|---:|---|
| 404 | `DOCUMENT_NOT_FOUND` | false | 一个或多个请求文档不存在或尚未 ready；details 包含 `missing_document_ids`。 |
| 422 | `TOO_MANY_DOCUMENT_IDS` | false | `doc_ids + temp_doc_ids` 超过 `RAG_MAX_DOC_IDS`。 |
| 422 | `DOCUMENT_PREFILTER_OVERFLOW` | false | 开启预筛后，二次预筛仍为至少一组保留了过多候选；details 包含 `affected_group_refs`。 |
| 422 | `QUERY_TOO_LONG_OR_INVALID` | false | criteria 固定内容或单篇文档元信息无法放入路由窗口。 |
| 429 | `RATE_LIMITED` | true | 用户请求 RPM 超过防滥用限制。 |
| 503 | `DB_QUERY_FAILED` | true | 读取文档轻量元信息失败。 |
| 503 | `SERVICE_OVERLOADED`、`ADMISSION_TIMEOUT` | true | 重型请求准入池已满或等待超时。 |

## 文档元信息接口

### `POST /rag/v1/documents/meta`

按请求中的 doc_id 批量读取正式和临时文档元信息，不执行 agentic 检索。`user_id`、`kb_id`、
`session_id` 为兼容字段，不参与读取过滤。`doc_ids` 与 `temp_doc_ids` 会合并处理；同一个
doc_id 如果存在多条 binding，只选择一条元数据。

```json
{
  "user_id": "user-1",
  "kb_id": "kb-1",
  "session_id": "upload-session-1",
  "doc_ids": ["doc-1", "doc-2"],
  "temp_doc_ids": ["temp-doc-1"],
  "include_missing": true
}
```

字段：

| 字段 | 必填 | 默认值 | 含义 |
|---|---:|---|---|
| `user_id` | 是 | 无 | 兼容字段，也用于调用侧 RPM 统计；不参与文档读取过滤。 |
| `kb_id` | 是 | 无 | 兼容字段；不参与文档读取过滤。 |
| `session_id` | 否 | `null` | 兼容的上游会话记录字段，不参与读取过滤。 |
| `doc_ids` | 否 | `[]` | 文档 ID 列表。与 `temp_doc_ids` 合并查询。 |
| `temp_doc_ids` | 否 | `[]` | 兼容字段；其中的 ID 与 `doc_ids` 一样按 doc_id 查询。 |
| `include_missing` | 否 | `true` | 是否在响应中返回未找到的 ID。 |

`doc_ids` 与 `temp_doc_ids` 至少一个非空，两者合计不能超过服务文档数量上限。同一 ID 同时
出现时只查询一次；两类文档可以在一次请求中同时返回。不存在的 ID 按未找到处理。

响应：

```json
{
  "documents": [
    {
      "doc_id": "doc-1",
      "doc_name": "制度.pdf",
      "doc_type": "pdf",
      "doc_description": "差旅与报销制度",
      "status": "ready",
      "page_count": 28,
      "node_count": 12,
      "created_at": "2026-07-01T08:00:00Z",
      "updated_at": "2026-07-01T08:10:00Z",
      "kb_id": "kb-1",
      "user_id": "user-1",
      "is_temporary": false,
      "bound_at": null
    }
  ],
  "missing_doc_ids": ["doc-2"]
}
```

## 文档原始解析结果与异步修复

### `POST /rag/v1/documents/raw`

读取一篇正式文档保存的 page、重建标题结构和 MinerU 原始结果。该接口不执行检索；当旧文档缺少
`raw.raw_mineru` 时，会请求入库服务在后台补齐该字段。修复只复用原文档 ID 并更新
`documents.raw.raw_mineru`，不会重新生成摘要、page、node、绑定关系或新文档。

```json
{
  "user_id": "user-1",
  "kb_id": "kb-1",
  "doc_id": "doc-1"
}
```

#### 已有 MinerU raw：HTTP 200

响应保持原契约，主要字段如下：

| 字段 | 含义 |
|---|---|
| `schema_version` | 当前固定为 `1.0`。 |
| `doc_id/doc_name/doc_type/status` | 文档标识和状态。 |
| `doc_description` | 入库阶段保存的文档摘要。 |
| `page_count/line_count/node_count` | 解析结果统计。 |
| `pages` | `{page, content}` 列表，页码从 1 开始。 |
| `structure` | 递归标题节点，包含 node ID、标题、文本、summary 和页码范围。 |
| `raw_mineru` | MinerU 的 Markdown、content list 和 middle JSON。 |

#### 缺少 MinerU raw：HTTP 202

接口原子认领或复用后台修复任务并立即返回，不等待 MinerU 完成：

```json
{
  "schema_version": "1.0",
  "doc_id": "doc-1",
  "repair_id": "8da692f4-acde-4ca1-9d89-f3f421949688",
  "task_id": "c47f8879-7db5-42d3-b28d-f8664b554bf7",
  "status": "queued",
  "status_url": "/rag/v1/documents/raw/status?user_id=user-1&kb_id=kb-1&doc_id=doc-1",
  "retry_after": 2
}
```

调用方应等待 `retry_after` 秒后轮询 `status_url`。同一文档的并发请求只会复用一个有效租约，
不会并行重复解析。修复完成后，再次调用本接口即可取得 HTTP 200 的完整 raw。

对同一旧文档再次调用标准入库接口时，如果 MinerU raw 缺失，也会复用相同的 repair claim 和
失败标记；不会新建 doc ID，也不会重新生成 description、page 或 node。首次正常 MinerU 入库
成功不创建 repair 标记；只有 MinerU 失败并成功回退 native parser 时才记录失败及冷却状态。

若旧文档没有 `file_oss_key`，接口返回 HTTP 422 `DOCUMENT_OSS_KEY_MISSING`。该错误不可重试；
调用方应使用入库服务的删除接口删除旧文档，再使用标准入库接口重新入库，使新记录带有 OSS key。

### `GET /rag/v1/documents/raw/status`

只查询修复状态，不创建任务。三个 query 参数 `user_id`、`kb_id`、`doc_id` 均为必填：

```bash
curl 'http://127.0.0.1:8120/rag/v1/documents/raw/status?user_id=user-1&kb_id=kb-1&doc_id=doc-1'
```

响应状态：

| `status` | 含义 | 调用建议 |
|---|---|---|
| `not_started` | 当前没有 raw，也没有修复记录。 | 调用 `POST /documents/raw` 触发修复。 |
| `queued` | 已进入入库服务队列。 | 按 `retry_after` 继续轮询。 |
| `processing` | MinerU 正在解析。 | 继续轮询，不要重复入库。 |
| `completed` | raw 已持久化。 | 调用 `POST /documents/raw` 获取完整结果。 |
| `failed` | 本次修复失败。 | 检查 `retryable/error_code/message/next_retry_at`。 |

示例：

```json
{
  "schema_version": "1.0",
  "doc_id": "doc-1",
  "status": "failed",
  "repair_id": "8da692f4-acde-4ca1-9d89-f3f421949688",
  "task_id": "c47f8879-7db5-42d3-b28d-f8664b554bf7",
  "retryable": true,
  "error_code": "RAW_MINERU_UPSTREAM_UNAVAILABLE",
  "message": "MinerU service is temporarily unavailable",
  "updated_at": "2026-07-27T12:00:00Z",
  "next_retry_at": "2026-07-27T12:05:00Z"
}
```

瞬时的网络、MinerU 5xx 或超时错误会记录为可重试失败；冷却期结束后，后续
`POST /documents/raw` 可以重新认领。来源缺失、下载内容与原 doc ID 不一致或 MinerU 返回不可用结果等
终态错误会标记为不可重试，防止前端轮询或重复请求造成无限解析。

### Raw 修复错误

| HTTP | error code | retryable | 含义与处理 |
|---:|---|---:|---|
| 404 | `DOCUMENT_NOT_FOUND` | false | 指定 doc_id 不存在。 |
| 422 | `DOCUMENT_OSS_KEY_MISSING` | false | 删除旧文档后通过标准入库重新入库。 |
| 422 | `RAW_MINERU_SOURCE_MISMATCH`、`RAW_MINERU_INVALID_RESULT` | false | 终态失败，不会自动重复解析；需人工检查源文件。 |
| 503 | `INGESTION_SERVICE_UNAVAILABLE` | true | 入库服务不可用，退避后重试。 |
| 503 | `RAW_MINERU_SOURCE_UNAVAILABLE` | true | OSS 源文件暂时不可用，等待 `next_retry_at` 后重试。 |
| 503 | `INGESTION_TIMEOUT`、`INGESTION_RUNTIME_ERROR` | true | MinerU/入库瞬时失败，等待 `next_retry_at` 后重试。 |

检索服务通过 `INGESTION_SERVICE_BASE_URL` 调用入库服务内部修复接口，调用超时由
`INGESTION_REPAIR_TIMEOUT_SECONDS` 控制。该内部接口不应暴露给浏览器或公网调用方。

## 健康检查

### `GET /healthz`

只检查进程是否存活，不访问数据库：

```json
{
  "ok": true,
  "service": "rag-retrieval-service",
  "version": "0.1.0",
  "env": "production"
}
```

### `GET /readyz`

检查 PostgreSQL 是否可用。就绪时：

```json
{
  "ok": true,
  "service": "rag-retrieval-service",
  "database": "ok"
}
```

数据库不可用时返回 HTTP 503 `DATABASE_UNAVAILABLE`。部署和负载均衡应使用 `/readyz`，
进程存活探测可以使用 `/healthz`。

## 错误响应

错误统一为：

```json
{
  "error": {
    "code": "DOCUMENT_NOT_FOUND",
    "message": "one or more requested documents were not found or are not ready",
    "retryable": false,
    "details": {
      "missing_document_ids": ["doc-unknown"]
    }
  }
}
```

| HTTP 状态 | 常见错误 | 调用建议 |
|---:|---|---|
| 404 | `DOCUMENT_NOT_FOUND`、`EMPTY_DOCUMENT_SCOPE`、`DIRECT_RESOURCE_NOT_FOUND` | 检查文档 ID和文档状态。 |
| 422 | `REQUEST_VALIDATION_ERROR`、`INVALID_TOP_K`、`INVALID_RETURN_TOKEN_BUDGET`、`QUERY_TOO_LONG_OR_INVALID` | 修正请求，不应原样重试。 |
| 429 | `RATE_LIMITED` | 按 `Retry-After` 退避。限流按 `user_id` 统计。 |
| 501 | `MODE_NOT_IMPLEMENTED` | 当前不要使用 `search_mode=keyword`。 |
| 503 | `DATABASE_UNAVAILABLE`、`RETRIEVAL_DEPENDENCY_UNAVAILABLE` | 可退避重试，并检查数据库、LLM 熔断和服务日志。 |
| 504 | `REQUEST_TIMEOUT` | 请求在产生完整证据前超时，可缩小文档范围或提高调用方超时后重试。 |

部分 LLM 批次失败、规则 fallback 或已有结果时，服务优先返回 HTTP 200 加 warning，而不是
让整个请求失败。只有在无法产生任何完整检索结果时才返回 5xx。

## 调用方检查清单

- 只从可信、已认证的上游调用，并由上游先完成 doc_id 权限校验。
- 正式与临时文档 ID 都只按 doc_id 查询；`session_id` 不影响可见性。
- 显式限定 `doc_ids/temp_doc_ids`，不要依赖服务自动扫描整个知识库。
- 读取 `chunks` 的同时检查 `warnings` 和 `coverage.complete`。
- 将 `content` 原样交给下游模型，并把 `hint` 用作证据用途和截断提示。
- 不依赖内部 node，不假设所有 chunk 都有单个 `document_id`。
- 记录 `usage.latency_ms`、`usage.actual_mode` 和 debug 中的 `request_id` 以便排障。
- 生产关闭 debug，并把 HTTP timeout 设置得高于检索服务允许的实际执行时间。
