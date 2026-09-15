# Changelog

## 2026-07-24

- 生产检索主链路接入独立 `rag-retrieval-service`，保留内置 PostgreSQL retriever 仅作
  legacy 兼容，不修改报告 Agent 的意图、改写、大纲和写作主流程。
- remote 请求新增动态 `max_return_tokens`：根据 query、文档数量和 broad 标记在
  `4096–32768` 范围内取值，并继续传递 `top_k`、`search_mode` 和文档范围。
- 兼容新检索响应：不再依赖 node chunk，只消费公开 page/元信息 chunk 的统一字段。
- `session_id` 原样透传给检索服务，用于 `temp_doc_ids` 归属校验；它仍会在
  `stream_start` 中回显，但不会恢复历史、报告或 LangGraph state。
- 远程检索 timeout 调整为可配置的 `RAG_RETRIEVAL_TIMEOUT_SECONDS`，生产建议 180 秒，
  并统一映射 timeout、429 和上游 HTTP 错误。
- 删除向无内部鉴权的新检索服务发送兼容 Bearer 的遗留配置与调用逻辑。
