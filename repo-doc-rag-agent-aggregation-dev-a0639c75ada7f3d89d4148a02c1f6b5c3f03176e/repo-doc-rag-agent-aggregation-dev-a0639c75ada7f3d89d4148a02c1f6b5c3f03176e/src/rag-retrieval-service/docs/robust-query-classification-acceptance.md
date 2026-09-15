# 鲁棒 Query 分类真实豆包 API 验收报告

- 生成时间：`2026-07-30T04:32:50+00:00`
- 模型：`doubao-seed-2-1-pro-260628`
- API host：`ark.cn-beijing.volces.com`
- 每个案例：字符串与列表输入各 `10` 次，合计 `20` 次/策略
- 受控并发：`4` 个分类请求；所有模型调用仍经过项目全局 `LLMGateway`
- API key、完整 prompt 和完整模型响应未写入报告。

## 总体结果

| 策略 | 输入 | 运行成功 | 子问题保留率 | 分类准确率 | 文档分组准确率 | 路由参数可用率 | Schema 失败率 | Fallback 率 | P50/P95 耗时 | 平均/P95 calls | 平均等待轮次 | Prompt/Completion/Total tokens |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fast | str | 100/100 | 100.00% | 99.23% | 100.00% | 100.00% | 0.00% | 0.00% | 2306/4186 ms | 1.00/1 | 1.00 | 177670/11099/188769 |
| fast | list | 100/100 | 100.00% | 100.00% | 100.00% | 100.00% | 0.00% | 0.00% | 2214/4134 ms | 1.00/1 | 1.00 | 177820/11610/189430 |
| robust | str | 100/100 | 100.00% | 100.00% | 100.00% | 100.00% | 0.00% | 0.00% | 2700/7068 ms | 3.80/5 | 2.60 | 230054/8956/239010 |
| robust | list | 100/100 | 100.00% | 100.00% | 100.00% | 100.00% | 0.00% | 0.00% | 1805/3222 ms | 2.80/4 | 1.60 | 171210/6696/177906 |

## 分案例结果

| 策略 | 案例 | 子问题保留率 | 分类准确率 | 失败次数 | P95 耗时 | 平均 calls |
|---|---|---:|---:|---:|---:|---:|
| fast | `scope_visibility` | 100.00% | 100.00% | 0 | 5649 ms | 1.00 |
| fast | `scope_count` | 100.00% | 100.00% | 0 | 1948 ms | 1.00 |
| fast | `scope_information` | 100.00% | 100.00% | 0 | 1494 ms | 1.00 |
| fast | `scope_summary` | 100.00% | 100.00% | 0 | 2004 ms | 1.00 |
| fast | `direct_page_count` | 100.00% | 100.00% | 0 | 3269 ms | 1.00 |
| fast | `focused_fact` | 100.00% | 100.00% | 0 | 2629 ms | 1.00 |
| fast | `broad_summary` | 100.00% | 100.00% | 0 | 3405 ms | 1.00 |
| fast | `cross_document_fact` | 100.00% | 100.00% | 0 | 4154 ms | 1.00 |
| fast | `mixed_three_categories` | 100.00% | 100.00% | 0 | 4213 ms | 1.00 |
| fast | `ambiguous_document_content` | 100.00% | 95.00% | 0 | 2597 ms | 1.00 |
| robust | `scope_visibility` | 100.00% | 100.00% | 0 | 1944 ms | 1.50 |
| robust | `scope_count` | 100.00% | 100.00% | 0 | 2095 ms | 1.50 |
| robust | `scope_information` | 100.00% | 100.00% | 0 | 1735 ms | 1.50 |
| robust | `scope_summary` | 100.00% | 100.00% | 0 | 2355 ms | 1.50 |
| robust | `direct_page_count` | 100.00% | 100.00% | 0 | 5328 ms | 4.50 |
| robust | `focused_fact` | 100.00% | 100.00% | 0 | 6869 ms | 4.50 |
| robust | `broad_summary` | 100.00% | 100.00% | 0 | 4103 ms | 4.50 |
| robust | `cross_document_fact` | 100.00% | 100.00% | 0 | 7068 ms | 4.50 |
| robust | `mixed_three_categories` | 100.00% | 100.00% | 0 | 4014 ms | 4.50 |
| robust | `ambiguous_document_content` | 100.00% | 100.00% | 0 | 6882 ms | 4.50 |

## 失败与误分类样本

仅列出前 30 个样本；错误文本已限制为异常类型或本地校验消息。

- `fast/str/ambiguous_document_content` 第 2 次：categories=['routed_direct'], questions=['《中华人民共和国能源法》这份文件大致讲了什么？'], retained=1/1, category_correct=0/1, error=-

## 验收结论

- Robust 严格门槛：**通过**。门槛要求运行、子问题保留、分类和 strict schema 均无失败。
- Fast 总体子问题保留率：100.00%；Robust：100.00%。
- Fast 总体分类准确率：99.62%；Robust：100.00%。
- 延迟是本次受控并发条件下的端到端分类节点耗时，包含网关排队和模型服务波动，不包含数据库、文档路由或 chunk 检索。
