# 知识库问答环境变量说明

本文对应当前 `.env.example`。知识库问答可选使用自己的入站 API Key；新检索服务已移除
内部 API Key 鉴权，因此不存在需要配置或转发的检索出站 key。

## 服务与安全

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `APP_ENV` | `prod` | 运行环境，只接受 `dev/prod/test`。 |
| `APP_HOST` | `127.0.0.1` | Compose 的宿主绑定地址。通过统一网关访问时可不暴露宿主端口。 |
| `APP_PORT` | `8130` | API 端口。 |
| `RAG_DOCKER_NETWORK` | `pageindex` | 单仓库 Compose 使用的共享网络名；统一 Compose 可使用默认网络。 |
| `DEBUG_ENABLED` | `false` | 生产关闭 debug 输出。 |
| `AUTH_ENABLED` | `false` | 问答接口入站 API Key 鉴权总开关。 |
| `KNOWLEDGE_CHAT_INBOUND_API_KEYS` | 默认留空；启用入站鉴权时注入 `caller:key,caller:key` | 仅用于知识库问答入站鉴权，不会转发给检索服务；启用时缺失或格式错误会阻止启动/部署。 |

## Ark 模型

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `ARK_API_KEY` | Secret 注入 | 意图识别/回答生成使用的 Ark key。 |
| `ARK_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | Ark OpenAI-compatible 地址。 |
| `MODEL_SMART` | `doubao-seed-2-0-lite-260215` | 回答生成模型；上线前确认账号已开通。 |
| `DOUBAO_THINKING_TYPE` | `enabled` | 最终回答发送“正在思考”状态并通过 SSE 转发 reasoning；意图路由仍固定关闭 thinking。延迟优先时可设为 `disabled`。 |

## 检索服务

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `RAG_RETRIEVAL_SERVICE_URL` | `http://rag-retrieval-service:8120` | 检索服务内网地址，使用统一 Compose 的 service name。 |
| `RAG_RETRIEVAL_TIMEOUT_SECONDS` | `180` | 调用独立检索服务的 HTTP 超时秒数；整请求 deadline 会自动为后续回答和收尾预留时间，不会更早截断检索。 |
| `RAG_MAX_RETURN_TOKENS` | `180000` | `rag.max_return_tokens` 的请求校验上限，也是调用者未传该字段时动态预算的最大值。默认值针对 doubao-2.0-lite 的 224K 最大输入预留约 44K token 给对话历史、系统提示词和消息封装；更换模型后可按模型输入能力调整。 |

## 限流与并发

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `REQUEST_RPM_LIMIT` | `600` | 按 caller fingerprint + user_id 的入站请求预算。当前为单进程内存限流，多实例会各自计数。 |
| `ARK_RPM_LIMIT` | `1800` | 进程级 Ark 调用预算；单次 chat 最多约两次调用。不得高于账号可用 RPM。 |
| `ARK_MAX_CONCURRENCY` | `30` | 单进程 Ark 并发上限，代码允许最大 32。多副本会倍增，需结合 RPM/TPM 和上游 429 压测。 |

## 上线检查

1. 默认 `AUTH_ENABLED=false`，由网关或防火墙控制访问；若改为 `true`，必须配置格式正确的 `KNOWLEDGE_CHAT_INBOUND_API_KEYS`。
2. 检索 URL 使用容器 DNS，8120 只允许可信内部调用方访问。
3. debug 关闭；若无需应用层入站鉴权，确认 8130 仅对受控网关或内网开放。网关 timeout
   应高于 `RAG_RETRIEVAL_TIMEOUT_SECONDS + Ark timeout + 收尾预留`。
4. `ARK_MAX_CONCURRENCY` 与副本数乘算后仍在 Ark 配额内；发生 429 时先降低并发或申请配额。
5. SSE 代理关闭响应缓冲并设置足够的读取 timeout，否则前端会看不到及时的状态和文本分片。
