# rag-knowledge-chat 烟测报告

日期：2026-07-15；增量复测：2026-07-16

## 范围

本次烟测覆盖：

- fake LLM 的四种回答路径
- 真实 Ark strict 路由与流式生成
- 真实 `rag-retrieval-service` 检索
- 本地 FastAPI Bearer 鉴权和完整 SSE 协议
- debug 双开关
- 开发面板静态页面、配置接口和服务端 key 加载

所有报告内容均已脱敏，没有记录 Ark API Key 或 RAG API Key。

## 自动化测试

- `uv run pytest -q`：59 项通过
- `uv run ruff check .`：通过
- `uv run ruff format --check .`：47 个 Python 文件格式正确
- `uv run python -m compileall -q app dev scripts`：通过
- 覆盖请求校验、鉴权、限流、strict schema、Ark adapter、检索客户端、引用、编排、
  SSE、debug gating、deadline、开发数据生成和 dev proxy

## Fake LLM 烟测

命令：

```bash
uv run python -m scripts.smoke_fake
```

结果：

| Case | Answer basis | Ark calls | Retrieval calls | References | Result |
|---|---|---:|---:|---:|---|
| direct | `general_no_retrieval` | 2 | 0 | 0 | PASS |
| grounded | `knowledge_base` | 2 | 1 | 1 | PASS |
| no_result | `general_no_result` | 2 | 1 | 0 | PASS |
| retrieval_error (401) | `general_retrieval_error` | 2 | 1 | 0 | PASS |

四条路径都满足单请求最多两次 LLM 调用，检索为空和检索 401 都能继续通识回答。

## 真实 Ark 烟测

命令：

```bash
uv run python -m scripts.smoke_real
```

结果：

| Case | Route | Retrieval | Basis | References | Latency | Result |
|---|---|---|---|---:|---:|---|
| identity | `identity` | false | `general_no_retrieval` | 0 | 9422 ms | PASS |
| grounded synthetic evidence | `knowledge_base` | true | `knowledge_base` | 1 | 7335 ms | PASS |

身份回答正确包含“广州日报粤传媒和光明实验室联合研发”的身份来源。知识库问题生成了独立 query，
最终回答使用合法 `[1]` 引用。

## 真实检索与 Ark 端到端烟测

命令：

```bash
uv run python -m scripts.smoke_live_pipeline
```

数据：

- Query ID：`q_000001`
- Gold doc ID：`76022cd1-39e0-5f96-b6d8-657c79c21c0f`
- Scope：`user_id=eval-user`、`kb_id=eval-rag-kb`

### 首次失败

- Route：`general_knowledge`
- Retrieval：未调用
- Basis：`general_no_retrieval`
- Latency：20745 ms

失败原因是路由提示词把“模型自己知道的事实”归类为无需检索。该行为不符合知识库问答入口的召回
目标，也会让离线评估 query 绕过知识库。

修复内容：

- 身份、打招呼、感谢、闲聊、纯创作仍不检索
- 事实性、解释性、总结、比较、制度和报告问题默认优先检索
- 即使模型知道通识答案，也应先在调用者给定范围内查找证据
- 新增 `test_route_prompt_defaults_factual_questions_to_retrieval` 回归测试

该失败发生在 chat 路由，检索服务没有收到请求，因此没有写入
`rag-retrieval-service/docs`。

### 修复后结果

- Route：`knowledge_base`
- Retrieval：`success`
- Returned chunks：2
- Returned document：gold doc
- Basis：`knowledge_base`
- References：2
- 最终重跑 latency：10938 ms
- Result：PASS

## HTTP SSE 烟测

正式服务：

```bash
DEBUG_ENABLED=true uv run uvicorn app.main:app --host 127.0.0.1 --port 8130
uv run python -m scripts.smoke_http
```

结果：

- HTTP status：200
- Bearer 鉴权：PASS
- `X-Request-Id`：存在
- Completion ID：全流唯一
- Status stages：`route -> retrieval -> generation`
- Basis：`knowledge_base`
- Chunks：2
- References：2
- `data: [DONE]`：收到
- Debug：服务端关闭时不返回；服务端和请求双开时返回
- 最终 debug 双开重跑 latency：14727 ms
- Result：PASS

## OpenAI Python SDK 兼容烟测

命令：

```bash
SMOKE_CHAT_URL=http://127.0.0.1:8130 uv run python -m scripts.smoke_openai_sdk
```

结果：

- SDK 能迭代标准 `ChatCompletionChunk`
- 正文来自 `choices[0].delta.content`
- 顶层 `rag` 可从 `chunk.model_extra` 读取
- Status stages：`route -> generation`
- Basis：`general_no_retrieval`
- 指定助手身份：正确
- Latency：8847 ms
- Result：PASS

## 开发面板烟测

- `GET /`：200
- 三个工作区：request、conversation、trace 均存在
- `GET /api/config`：200
- API key 原文：未暴露
- 初次发现 dev server 直接读取 `os.getenv` 时不会加载 `.env`
- 修复为复用 `Settings()` 后：`api_key_configured=true`
- `tests/integration/test_dev_server.py`：6 项通过
- 用户与助手消息分别显示 `U` / `AI` 头像
- 回答下方按 `references + chunks` 显示可展开的文档、页码/节点和原文预览
- `stage=thinking` 会触发“正在思考”状态动效，正文开始后停止

由于无人值守环境不进行人工浏览器交互，本次使用 HTTP 页面检查、mock SSE proxy 测试和静态前端
逻辑测试替代人工点击验收。页面已启动，可在本地浏览器继续人工观察。

## 2026-07-16 Thinking 与引用增量烟测

本轮将 Ark provider stream 改为 typed event：路由固定关闭 thinking；回答生成遵循
`DOUBAO_THINKING_TYPE`；隐藏 reasoning 只转换成一次无内容 `thinking` 状态事件。

自动化与 fake 结果：

- `pytest`：59 项通过
- fake 的 direct、grounded、no-result、retrieval-error 四条路径全部通过
- 真实 Ark identity 与 grounded synthetic evidence 两条路径全部通过
- 路由请求的 `extra_body.thinking.type` 固定为 `disabled`
- 重复 reasoning chunks 只产生一个 `AnswerThinkingStarted`
- prompt-injection 防护为共享的两句短系统约束，没有增加模型调用

真实 disabled RAG：

- HTTP：200
- Status：`route -> retrieval -> generation`，thinking 事件 0 个
- Retrieval/Basis：`success / knowledge_base`
- References：2，包含 `document_name` 和 `chunk_meta.page_number`
- `generation_started -> first_token`：2957 ms
- reasoning 字段：未暴露
- `[DONE]`：收到

真实 enabled RAG 使用隔离端口 8131，测试后已关闭：

- HTTP：200
- Status：`route -> retrieval -> generation -> thinking`
- Thinking 事件：1 个，在请求开始后 3.979 秒到达
- 首个正文：7.474 秒到达，thinking 状态领先正文 3.495 秒
- Retrieval/Basis：`success / knowledge_base`
- References：2
- reasoning 字段：未暴露
- `[DONE]`：收到
- review 修正后以真实身份问题覆盖通识声明路径，事件顺序为
  `route -> generation -> thinking -> content`，thinking 早于声明和所有可见正文

最终兼容性复跑：

- HTTP SSE：通过，`knowledge_base`、2 个 references、2 个 chunks、debug 和 `[DONE]` 均正常，
  latency 4007 ms
- OpenAI Python SDK：通过，标准 chunk 可迭代、顶层 `rag` 可见、身份回答正确，latency 3525 ms

## 结论

核心问答、真实 Ark strict 路由、真实检索、引用、HTTP SSE、debug 和开发面板代理均通过烟测。
首次真实路由失败已通过提示词约束和回归测试修复，没有发现需要记录到检索服务项目的失败案例。
