# Fast 与 Robust 复合 Query 全链路验收执行记录

- 执行日期：`2026-07-30`
- 目标模型：`doubao-seed-2-1-pro-260628`
- 测试入口：`POST /rag/v1/retrieve`
- 当前状态：**用例与脚本已完成；完整 fast/robust 对比未执行。**

## 测试设计

测试脚本为 `scripts/run_compound_retrieval_acceptance.py`，固定构造 12 个复合字符串 query，
每个策略各执行一次，共计划 24 次真实检索请求。fast 与 robust 使用相同的 5 篇文档范围、
相同问题、`top_k=12` 和 `max_return_tokens=16384`。

用例包含：

- 来自 `rag-offline-eval` 标注的 focused 事实问题及对应 doc_id；
- “你能看到什么”“你手头有哪些材料”等口语化 scope 问题；
- 页数、章节数、目录和章节结构等 direct 问题；
- “到底在说啥”“究竟是干什么的”、详细总结和整体解释等 broad 问题；
- 同一请求内 3 至 5 个类别交叉的复合问题；
- 同一目标文档跨 focused/direct/broad 的问题；
- 不依赖历史、没有未消解指代的自包含问题。

脚本检查 HTTP 成功率、子问题保留率、分类准确率、group 覆盖率、目标文档命中率、focused
原文锚点命中率、P50/P95 耗时、LLM 调用次数和 robust 专属 trace 是否存在。

## 已执行烟测

更新后的 robust 服务已在 `8220` 重启，并使用单篇《中华人民共和国能源法》执行以下安全
范围烟测：

```text
你这边到底能看到哪些资料？
```

结果：

- 输入类型：`string`；检索服务内部执行了 query 改写；
- 改写结果：`当前能看到哪些资料？`；
- 分类结果：`scope_direct`；
- `classification_trace`：`normalized_queries → scope_decision → final_groups`；
- 返回：1 个 `scope_metadata` chunk；
- coverage：完整，group `g0001` 已覆盖；
- 总耗时：`6260 ms`；
- LLM 调用：3 次，分别为改写、scope 二分类和 scope 工具规划。

该烟测确认最新服务进程、robust 字符串拆分路径、新 debug 契约和真实豆包调用均正常工作。

## 未执行原因

完整检索验收会把本地评测数据库中的文档正文通过检索流程发送给外部豆包模型。当前执行环境
的数据外发保护策略拒绝了该操作，并要求对“把这些具体评测文档发送到该外部平台”取得更明确
授权。测试没有通过其他方式规避该限制，两个临时测试实例已停止。

因此本文不能给出 fast/robust 的真实全链路对比指标，也不能宣称 §15.2 的扩展验收通过。
获得相应授权后可直接运行。脚本会预检两个 `/readyz`；本地 `8320/8420` 未启动时会分别以
`fast` 和 `robust` 配置自动启动临时服务，并在对应策略测试结束后关闭自己启动的进程：

```bash
.venv/bin/python scripts/run_compound_retrieval_acceptance.py \
  --fast-url http://127.0.0.1:8320 \
  --robust-url http://127.0.0.1:8420 \
  --model doubao-seed-2-1-pro-260628 \
  --report docs/robust-compound-retrieval-acceptance.md
```

脚本会用真实结果覆盖本执行记录。
