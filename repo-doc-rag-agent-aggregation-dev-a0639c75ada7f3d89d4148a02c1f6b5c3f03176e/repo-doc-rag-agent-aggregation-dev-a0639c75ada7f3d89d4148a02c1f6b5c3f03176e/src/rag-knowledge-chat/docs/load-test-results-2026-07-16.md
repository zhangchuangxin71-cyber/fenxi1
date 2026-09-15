# 30 并发压测结果（2026-07-16）

## 目标

验证单容器 `rag-knowledge-chat` 在生产模板配置下能够接受 30 个同时进行的
`POST /v1/chat/completions` SSE 请求，并区分应用本地限流与 Ark 上游限制。

## 配置

- `REQUEST_RPM_LIMIT=600`
- `ARK_RPM_LIMIT=1800`
- `ARK_MAX_CONCURRENCY=30`
- 请求数：30
- 客户端并发：30
- 场景：短通识回答，不调用检索服务
- 回答上限：48 tokens
- thinking：关闭

测试没有读取、打印或写入 Ark API Key。

## 应用内可控测试

使用真实 FastAPI 路由、SSE 编排、认证与限流，只替换 Ark/检索为可控假实现。

结果：

- 30/30 请求成功
- 应用 429：0
- 上游错误：0
- 最大在途 HTTP 请求：30
- 最大在途 Ark 调用：30

自动化用例：`tests/integration/test_load_capacity.py`。

## 单 Ark Key 真实测试

命令：

```bash
uv run python -m scripts.load_chat \
  --base-url http://127.0.0.1:8131 \
  --requests 30 \
  --concurrency 30 \
  --max-tokens 48 \
  --timeout-seconds 120 \
  --query "请只回答你好"
```

结果：

| 指标 | 结果 |
|---|---:|
| 成功请求 | 30/30 |
| HTTP 状态 | 30 个 200 |
| 应用本地限流 | 0 |
| Ark/上游错误 | 0 |
| 收到 `[DONE]` | 30/30 |
| 最大在途请求 | 30 |
| 墙钟时间 | 6.42 s |
| 吞吐 | 4.67 req/s |
| 总延迟平均 | 4.30 s |
| 总延迟 P95 | 5.28 s |
| 总延迟最大值 | 6.37 s |
| 首状态事件 P95 | 0.37 s |
| 首正文 P95 | 5.28 s |

## 结论与边界

当前应用 semaphore、RPM 配置和本次使用的单个 Ark Key 能承接一次 30 并发短回答，
没有观察到应用限流或上游配额错误。

该结果不是持续 soak test，也没有覆盖长上下文、长回答和 hybrid 检索。上线后仍需从
容器指标观察 CPU/内存、Ark 429、请求 deadline 和检索服务延迟。若压测结果出现
`rate_limit_exceeded`，说明应用模板限制过低；若只有 `ark_upstream_error`，
应先核对 Ark Key 配额，不能继续盲目提高并发。
