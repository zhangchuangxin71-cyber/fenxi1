# Changelog

## 2026-07-31

- 强化增量文档 query 改写：提示词要求使用确切文件名，编排层对仍残留的“新增文档”或
  “刚上传的文档”等高确定性指代做代码后置修正，避免泛化路由参数导致检索为空。
- `rag.incremental_doc_ids` 支持混合声明正式与临时增量文档；知识库问答按当前
  `doc_ids/temp_doc_ids` 自动分区，并通过带 session 隔离的统一 meta 接口获取文件名。
- `rag.max_return_tokens` 上限改由 `RAG_MAX_RETURN_TOKENS` 配置；入站校验和省略该字段时的
  动态预算共用同一配置。非法值仍在模型调用和 SSE 开始前返回 HTTP 400。doubao-2.0-lite
  默认使用 `180000`，为对话历史、提示词和消息封装预留输入空间。
- 删除检索 422 的特殊解析、编排短路和 SSE `details` 透传扩展。
- 开发面板移除 `max_return_tokens` 的旧 `4096–32768` 前后端限制，输入值原样写入请求体，
  最终合法范围由知识库问答和检索服务的正式接口校验。
- `rag` 新增 `incremental_doc_ids`：调用方声明本轮新增正式文档后，知识库问答批量调用检索
  meta 接口取得真实文件名，并在同一次结构化 decide 调用中完成增量指代消解；服务保持无状态。

## 2026-07-30

- 完成事件的 `references` 改为面向调用方展示的精简契约，仅返回与正文 `[N]` 对应的
  `citation_index`、`doc_id`、`doc_name` 和 `page_number`；集合级引用固定使用
  `doc_id=null`、`doc_name=system`、`page_number=1`，完整证据仍保留在 `chunks` 中。
- 当当前请求范围只有一篇文档时，路由提示明确要求把文档代词消解为该唯一文档，且该范围
  规则优先于对话历史，不因缺少文档名要求用户澄清。
- 检索路由模型现在利用完整历史完成指代消解，把跨文档比较和复合问题拆为独立的 `query`
  数组，并完整保留“你能看到哪些文档”等请求范围问题。
- 问答路由拆分并改写子问题后，将 `string[]` 直接传给检索服务；检索客户端继续兼容两种
  query 类型，动态 `top_k` 与 `max_return_tokens` 基于所有子问题拼接后的统一文本计算。

## 2026-07-24

- 在自定义 `rag` 请求对象中增加可选 `session_id`；当 `temp_doc_ids` 非空时条件必填，
  并原样传给 `rag-retrieval-service` 做临时文档归属校验。
- 保持 Chat Completions 无状态：聊天历史仍完全由调用方通过 `messages` 提供，
  `session_id` 不用于恢复消息、记忆或模型上下文。
- 增加 `max_return_tokens` 兼容和按 query/文档数量计算的有界检索预算。
- 将检索 HTTP timeout 改为配置项 `RAG_RETRIEVAL_TIMEOUT_SECONDS`，并使请求总 deadline
  能够覆盖检索和 Ark 生成阶段。
- 检索 trace 与问答 trace 独立控制；即使问答 `DEBUG_ENABLED=false`，也会保留检索服务
  在双开关允许时返回的 `debug.retrieval`。
- 开发面板增加 `top_k`、`max_return_tokens`、改写后实际检索问题和检索 trace 可视化；引用详情
  优先展示 chunk hint。
- query 路由提示词和受控契约要求：根据完整历史强制消解指代、把跨文档比较拆成独立
  事实子问题，并保留复合请求中的全部元信息问题；无法唯一消解时使用 `clarification`
  路径询问用户，不调用检索服务。
- 入站 key 配置更名为 `KNOWLEDGE_CHAT_INBOUND_API_KEYS`，启用鉴权时强制校验
  `caller:key` 格式；移除向无内部鉴权的新检索服务发送 Bearer 的遗留逻辑。
- `model`、`stream`、`stream_options.include_usage`、`temperature`、`max_tokens` 和 debug
  均提供默认值；省略 `top_k` 时按 query 与文档范围在 `4–20` 内动态计算。
