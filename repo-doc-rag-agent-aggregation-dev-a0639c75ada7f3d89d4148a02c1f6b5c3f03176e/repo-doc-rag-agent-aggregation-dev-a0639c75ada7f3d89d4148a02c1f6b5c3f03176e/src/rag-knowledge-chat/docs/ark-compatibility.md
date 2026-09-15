# Ark 兼容性记录

验证日期：2026-07-15

## 环境

- Base URL：`https://ark.cn-beijing.volces.com/api/v3`
- Model：`doubao-seed-2-0-lite-260215`
- OpenAI Python SDK：2.45.0
- Thinking：路由固定 `disabled`；回答生成由 `DOUBAO_THINKING_TYPE` 控制
- API Key：已配置，未写入本文档或任何测试输出

配置直接复用了 `rag-report-agent/.env` 中的
`ARK_API_KEY/ARK_BASE_URL/MODEL_SMART/DOUBAO_THINKING_TYPE`。

## 受控路由

以下调用方式已通过真实请求验证：

- `chat.completions.create`
- `temperature=0`
- `response_format.type=json_schema`
- `response_format.json_schema.strict=true`
- Schema 使用 `additionalProperties=false`
- 路由固定使用 `extra_body.thinking.type=disabled`

模型稳定返回并通过 Pydantic 校验：

```json
{
  "needs_retrieval": true,
  "query": "独立检索问题",
  "reason_code": "knowledge_base"
}
```

身份问题能够返回 `needs_retrieval=false` 和空 query。事实性知识问答在收紧系统提示词后能够返回
`needs_retrieval=true`。

## 流式生成

以下调用方式已通过真实请求验证：

- `stream=true`
- 正文从 `choices[0].delta.content` 增量读取
- 第一个原生 reasoning 分片转换为一次 `stage=thinking` 状态，reasoning 分片随后通过
  `choices[0].delta.reasoning_content` 转发
- `temperature` 和 `max_tokens` 使用调用者请求中的受控值

本项目不依赖 Ark 的原生 tool-call streaming。内部“是否调用检索工具”由第一次 strict JSON Schema
路由结果决定，因此不会出现 tool arguments 分片拼接或前端误执行内部工具的问题。

## Thinking 延迟复测

同一模型、同一短问答提示分别使用两种 thinking 配置进行真实流式请求：

| 配置 | 建立流 | 首个上游 chunk | 首个正文 token | 正文前 reasoning chunks |
|---|---:|---:|---:|---:|
| `enabled` | 1.660s | 1.674s | 15.534s | 567 |
| `disabled` | 0.887s | 0.888s | 0.888s | 0 |

`ArkClient` 会将原生 reasoning 以 SSE 增量转发。因此启用 thinking 时，上游虽然很快开始
发送 reasoning，用户仍可能要等待 reasoning 完成后才能看到第一个正文 token。路由调用不会消费或
暴露 reasoning；回答配置为 enabled/auto 时，会先发送一次“正在思考”状态。延迟敏感场景推荐
`DOUBAO_THINKING_TYPE=disabled`，质量优先场景可以开启。修正后
disabled 模式真实 RAG 全链路的
`generation_started -> first_token` 为 1.771 秒。

## 结论

当前 Ark endpoint/model 支持本项目需要的 strict 受控路由和文本流式生成。供应商差异被限制在
`app/integrations/ark.py`；业务编排只接收校验后的 `RouteDecision`、thinking marker、reasoning delta
和正文 delta。路由不应沿用 `rag-report-agent` 的 enabled 配置；回答生成可按质量与延迟目标自行选择。
