# 检索服务 499 文档 × 30 并发容量测试报告

测试日期：2026-07-27（UTC）

## 一、主管结论

本次被测场景通过，但目前不能据此无条件承诺“任意 1000 篇文档 × 30 并发”都稳定。

- 对“499 篇请求范围、keyword 预筛后只保留少量相关文档”的精确事实查询，30 个并发请求全部
  HTTP 200、全部返回非空结果、30/30 命中目标文档和标注页；P95 为 9.34 秒，检索进程
  峰值 RSS 为 229.34 MB，没有内存 PSI 压力。
- 读取 1000 篇文档名和摘要本身不是主要瓶颈。499 篇的文档名和摘要原始数据合计约
  302 KB，按当前数据平均值线性外推，1000 篇约 0.61 MB/请求。即使 30 个请求同时持有，
  原始字段也只有约 18.2 MB；Python/Pydantic、JSON 和路由对象会放大该值，但仍不应导致
  16 GB 机器仅因 scope metadata 崩溃。
- 当前更危险的是预筛后的候选规模：候选多时会增加 document-routing LPT 批次、LLM prompt、
  全局 LLM 排队，以及 accept 文档的 focused-search 并行任务。此次 manifest 用例具有明确文档
  身份，不能代表“预筛保留数百至上千篇”的最坏情况。
- `APP_MAX_CONCURRENCY=30` 当前没有接入 API 请求级 semaphore。30 个请求会全部进入 LangGraph，
  只有 LLM call 受全局 16 并发限制。实测 PostgreSQL 连接从 2 条升到 31 条（其中 1 条为采样器），
  检索池的 30 条连接全部建立并在请求结束后保持空闲。
- 测试期间还观察到同一 `q_000001` 小范围正确性门禁一次失败、重试恢复，说明真实模型路径仍有
  非确定性。最终容量数字改用门禁稳定通过的 `q_000003`，但这次失败必须作为可靠性风险保留。

因此，当前版本可支持已测的“499 篇、30 并发、精确查询且预筛高度选择性”场景；若甲方验收口径
明确要求任意 1000 篇输入都能承载，仍需补齐请求级准入保护，并完成 1000 篇选择性与高候选扇出
两类测试后才能正式承诺。

## 二、静态链路与风险节点

### 2.1 文档范围加载和 keyword 预筛

`validate_and_load_scope` 通过一次 SQL 读取请求范围内每篇 ready 文档的：

- `doc_id`、`doc_name`、`doc_type`；
- `doc_description`；
- `page_count`、`node_count`；
- 临时文档和 session 元信息。

SQL 使用 `doc_id = ANY(array)` 限定显式 scope，没有读取 `doc_pages` 或 `doc_nodes` 正文。随后
`keyword_prefilter` 同时对文档名和摘要做规则评分：文档名短语/覆盖度权重为 0.45/0.30，摘要
短语/覆盖度权重为 0.15/0.10。计算复杂度近似为：

```text
query groups × documents × routing keywords
```

当前实现会在每个 group 中重新归一化文档名和摘要，因此多个子问题 group 会线性放大 CPU 和临时
字符串分配。不过 1000 篇、少量 group 的规模仍较小，本次 30 并发下检索进程只占约 0.13 个 CPU
core，证明主要等待时间不在 keyword 字符串匹配。

数据库中 499 篇实际元信息大小：

| 项目 | 实测值 |
|---|---:|
| 文档名 UTF-8 字节数 | 34,503 B |
| description UTF-8 字节数 | 267,682 B |
| description 平均值 | 536.44 B/篇 |
| description 最大值 | 1,170 B |
| 文档名 + description | 302,185 B |

### 2.2 第二个瓶颈：候选文档扇出和 LLM 队列

keyword 预筛只是入口。风险更高的后续链路是：

1. 所有预筛候选被序列化为 document-routing view，并按 token 使用 LPT 分组。
2. 每个 LPT 批次都进入全局 LLM scheduler；不会因并发上限而丢弃，但会增加排队时间和内存驻留。
3. 每个 routed-focused group 的所有 `accept_docs` 会一次性构造 coroutine，并通过
   `asyncio.gather` 并行执行每文档 tree search。
4. 每篇 accept 文档又可能读取完整 node tree；最终 PageInspector 会读取候选 pages，并再次执行
   LPT + LLM page inspection。
5. 全局 LLM provider 并发被限制为 16、单请求最多 6 个 in-flight，但请求 state、候选文档、
   page/node 数据和等待中的 coroutine 不受 `APP_MAX_CONCURRENCY` 控制。

因此，1000 篇 scope 并不等于一定检索 1000 篇正文。预筛只保留 1 至数篇时成本可控；阈值为 0、
whole-scope marker 生效、路由模型大量 accept，或用户问题本身要求跨大量文档时，才会触发真正的
容量风险。

### 2.3 数据库和连接池

- `DB_POOL_MAX=30`，阻塞 psycopg executor 也是 30 workers。
- PostgreSQL `max_connections=100`，`shared_buffers=128MB`。
- 30 并发阶段数据库连接峰值为 31：检索服务 30 + 压测采样器 1。
- 请求完成后仍为 31，当前自研连接池没有 idle shrink/TTL。
- 精确到 30 并发时没有失败；若同进程还承接健康检查、文档元信息接口，或者并发瞬时超过 30，
  新 DB 操作只能等待最多 10 秒，随后可能返回 `DB_POOL_EXHAUSTED`。

这不是本次内存崩溃点，但属于上线容量边界，且一个检索实例会长期占用数据库最多 30 个连接。

## 三、测试环境与方法

### 3.1 环境

| 项目 | 值 |
|---|---:|
| CPU | 28 vCPU |
| 内存 | 16,601,767,936 B（约 15.46 GiB） |
| Swap | 4 GiB，测试期间未使用 |
| PostgreSQL | 14，`127.0.0.1:5433` |
| 文档数 | 499 |
| pages | 33,672 |
| nodes | 70,192 |
| 检索服务 | 单 Uvicorn worker，端口 8220 |
| `RAG_MAX_DOC_IDS` | 1000（仅本地 `.env`） |
| `RAG_DOCUMENT_PREFILTER_THRESHOLD` | 0.05 |
| `DB_POOL_MAX` | 30 |
| `RAG_LLM_MAX_CONCURRENCY` | 16 |
| `RAG_LLM_PER_REQUEST_MAX_IN_FLIGHT` | 6 |
| `RAG_LLM_MAX_QUEUED_CALLS` | 1024 |

### 3.2 固定测试问题

从 `rag-offline-eval/rag-eval-dataset/manifests/query_labels.jsonl` 选择：

```text
query_id: q_000003
问题：北京的建筑垃圾资源化利用国家循环经济标准化试点的承担单位是哪家？
标注页：第 1 页
```

先使用目标文档 + 9 篇噪声文档执行小范围正确性门禁；命中目标文档和标注页后，单请求及 30 并发
阶段均传入数据库中的全部 499 个 `doc_ids`。请求参数固定为 semantic、`top_k=5`、
`max_return_tokens=8192`。主压力阶段关闭 response debug，避免把调试响应体开销混入生产型指标。

压测脚本位于 `scripts/load_scope_benchmark.py`，原始结果位于
`docs/capacity-benchmark-499-documents-2026-07-27.json`。P95 使用 nearest-rank 定义，避免少量样本
插值造成偏低；正确性分别统计 HTTP 成功、非空结果、目标文档命中和标注页命中，避免 200 空结果
误报为成功。

复现命令：

```bash
uv run python scripts/load_scope_benchmark.py \
  --postgres-dsn "$POSTGRES_DSN" \
  --manifest /workspace/rag-offline-eval/rag-eval-dataset/manifests/query_labels.jsonl \
  --query-id q_000003 \
  --base-url http://127.0.0.1:8220 \
  --service-port 8220 \
  --concurrency 30 \
  --max-docs 1000 \
  --output docs/capacity-benchmark-499-documents-2026-07-27.json
```

## 四、结果

### 4.1 请求正确性与耗时

| 阶段 | Scope | 并发 | HTTP 成功 | 非空 | 目标文档命中 | 标注页命中 | 平均 | P95 | 最大 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 小范围门禁 | 10 | 1 | 1/1 | 1/1 | 1/1 | 1/1 | 6.36s | 6.36s | 6.36s |
| 全 scope 单请求 | 499 | 1 | 1/1 | 1/1 | 1/1 | 1/1 | 3.89s | 3.89s | 3.89s |
| 全 scope 压测 | 499 | 30 | 30/30 | 30/30 | 30/30 | 30/30 | 6.47s | 9.34s | 9.42s |

30 并发阶段墙钟时间 9.54 秒，吞吐 3.14 req/s，共发生 60 次 LLM call；没有 warning、HTTP 错误、
空 chunk、错误文档或错误页命中。

### 4.2 CPU、内存和数据库

| 指标 | 单请求 499 篇 | 30 并发 499 篇 |
|---|---:|---:|
| 检索进程 CPU（100%=单核） | 0.79% | 12.95% |
| 主机 CPU | 0.38% | 0.90% |
| 检索 RSS 起始 | 166.57 MB | 168.35 MB |
| 检索 RSS 峰值 | 168.21 MB | 229.34 MB |
| 检索 RSS 峰值增长 | 1.64 MB | 60.99 MB |
| 主机可用内存最大下降 | 7.11 MB | 152.97 MB |
| memory PSI some avg10 | 0 | 0 |
| DB 连接峰值（含 1 个采样连接） | 2 | 31 |
| PostgreSQL 临时文件/字节 | 0 / 0 | 0 / 0 |

CPU 很低说明该用例主要是 LLM 网络 I/O 等待，而不是 metadata 预筛计算。主机可用内存下降大于
检索进程 RSS 增长，主要还包括 PostgreSQL backend、socket buffer、HTTP/LLM client 和系统 cache。

## 五、1000 篇 × 30 并发外推

以下是启发式外推，不是实测承诺：

- metadata 原始字节从 499 增到 1000 约翻倍，每个请求增加约 0.30 MB，30 请求增加约 9.1 MB
  原始字段；考虑 Python 对象、归一化字符串、JSON 和 Pydantic 对象，实际增量会是数倍。
- 若 keyword 预筛仍然只保留少量文档，参考 499 篇阶段每请求约 2 MB RSS 增量，1000 篇下检索
  进程峰值大概率仍在数百 MB，而不是数 GB；在本次 16 GB 主机上有明显余量。
- 若预筛保留数百或上千文档，成本不再按 metadata 大小简单线性变化：document-routing prompt
  会被拆成多个 LPT 批次，30 个请求的 LLM call 数和队列等待可能显著增加；大量 accept 还会触发
  node/page 读取和 focused-search coroutine 扇出。本次数据不能为该路径提供上界。
- 生产机器的 CPU/内存、PostgreSQL 是否同机、数据库冷缓存、网络 RTT 和模型配额都会改变结果。

## 六、上线前建议

按优先级建议：

1. **先补请求级准入。** 将 `APP_MAX_CONCURRENCY` 真正接入 API middleware/semaphore；超过容量的
   请求应排队或快速返回明确的 429/503，不能让无界 LangGraph state 同时驻留。
2. **明确实例与数据库连接预算。** `DB_POOL_MAX` 不应自动等于业务并发上限。为健康检查和其它接口
   预留连接，并为连接池增加 idle 回收，避免每实例永久占用 30 条连接。
3. **限制文档级 fan-out，而不是截断召回。** focused search 应使用每请求文档任务 semaphore/worker
   queue；任务可排队，但不要一次性创建数百个持有 node/page 数据的 coroutine。
4. **补两类 1000 篇验收。** 一类使用像本次一样的高选择性 query；另一类应让预筛保留大量候选，
   专门测 LPT 批次、LLM queue、deadline、RSS 和 fallback。二者不能相互替代。
5. **生产环境显式设置 `RAG_MAX_DOC_IDS=1000`。** 当前代码和示例默认仍是 100；若部署环境没有
   覆盖，1000 ID 请求会在业务处理前被拒绝。
6. **记录正确性稳定率。** 同一用例至少重复多轮；本次 `q_000001` 的一次门禁失败说明单轮成功
   不能代替稳定性评估。

在不改变检索召回逻辑的前提下，前三项主要解决过载时“崩进程/耗尽连接”的问题；它们不会把
1000 篇硬截断回固定数量，也不会重新引入旧项目的候选 chunk 截断缺陷。
