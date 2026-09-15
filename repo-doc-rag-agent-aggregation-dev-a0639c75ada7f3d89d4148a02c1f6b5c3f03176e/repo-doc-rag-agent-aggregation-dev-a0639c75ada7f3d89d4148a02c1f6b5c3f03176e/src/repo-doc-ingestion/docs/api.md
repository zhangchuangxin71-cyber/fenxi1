# 文档入库服务 API

本文档描述 `repo-doc-ingestion` 第一版 HTTP 接口。OpenAPI 页面位于服务的
`/docs`，机器可读 schema 位于 `/openapi.json`。

## 基本约定

- 默认地址：`http://127.0.0.1:8100`
- API 前缀：`/ingestion/v1`
- 请求和响应：`application/json; charset=utf-8`
- 文档来源：阿里云 OSS 对象 Key，不接受客户端本地路径
- 支持格式：`pdf`、`doc`、`docx`、`txt`、`md`、`markdown`、`html`、
  `xlsx`、`pptx`
- 服务目前没有内置 API Key 鉴权。生产环境必须绑定内网/回环地址，并由网关或防火墙控制访问。

所有业务接口统一返回：

```json
{
  "code": 200,
  "message": "success",
  "data": {}
}
```

FastAPI 参数校验错误返回 HTTP 422；业务冲突、资源不存在等使用对应 HTTP 状态码。

## 提交普通文档

`POST /ingestion/v1/documents/ingest`

接口快速返回任务信息，解析、摘要和落库由后台 worker 异步执行。单文件字段和
`documents` 批量字段必须二选一。

单文件请求：

```json
{
  "user_id": "user-1",
  "kb_id": "kb-1",
  "oss_key": "uploads/manual.pdf",
  "file_name": "manual.pdf",
  "file_type": "pdf",
  "idempotency_key": "upload-20260716-001",
  "config": {
    "enable_ocr": true,
    "language": "zh",
    "extract_tables": true,
    "extract_images": false,
    "max_tree_depth": 3,
    "summary_enabled": true,
    "table_parse_mode": "auto"
  }
}
```

批量请求：

```json
{
  "user_id": "user-1",
  "kb_id": "kb-1",
  "documents": [
    {
      "oss_key": "uploads/a.pdf",
      "file_name": "a.pdf",
      "file_type": "pdf"
    },
    {
      "oss_key": "uploads/b.docx",
      "file_name": "b.docx",
      "file_type": "docx"
    }
  ]
}
```

主要字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 文档所属用户 |
| `kb_id` | 是 | 文档所属知识库 |
| `oss_key` | 单文件时是 | OSS 对象 Key，开头的 `/` 会被移除 |
| `file_name` | 单文件时是 | 展示文件名 |
| `file_type` | 单文件时是 | 支持的文件类型 |
| `documents` | 批量时是 | 至少一个文档 |
| `callback_url` | 否 | 完成/失败回调；服务端必须启用且通过白名单 |
| `idempotency_key` | 否 | 同一 `kb_id` 范围内的幂等键 |
| `config` | 否 | 入库算法参数，未传时使用默认值 |

`config` 常用字段：

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `enable_ocr` | `true` | 启用 OCR/复杂页面处理 |
| `language` | `zh` | `zh`、`en`、`auto` |
| `extract_tables` | `true` | 提取表格 |
| `extract_images` | `false` | 当前仅兼容配置，不代表图片语义入库 |
| `max_tree_depth` | `3` | 允许 1、2、3 |
| `node_max_tokens` | `512` | 叶节点目标大小 |
| `summary_enabled` | `true` | 生成节点摘要和文档描述 |
| `table_parse_mode` | `auto` | 第一版稳定支持 `off`、`auto` |

成功响应：

```json
{
  "code": 200,
  "message": "文档已提交处理",
  "data": {
    "task_id": "task-uuid",
    "doc_id": "document-uuid",
    "doc_ids": ["document-uuid"],
    "user_id": "user-1",
    "status": "queued",
    "estimated_seconds": 30
  }
}
```

批量提交时以 `doc_ids` 为准；`task_id` 用于查询整个任务状态。

## 提交临时文档

`POST /ingestion/v1/documents/temp-ingest`

临时文档首次入库时进入高优先级队列。`session_id` 记录上传任务来源，但不参与文档去重或
读取授权。请求必须提供单个文档：

```json
{
  "user_id": "user-1",
  "kb_id": "kb-1",
  "session_id": "session-1",
  "oss_key": "temporary/chat.pdf",
  "file_name": "chat.pdf",
  "file_type": "pdf",
  "config": {
    "summary_enabled": true
  }
}
```

响应结构与普通入库相同，成功消息为“临时文档已提交处理（高优先级）”。同一
`user_id + kb_id` 重复上传相同临时文档时，接口直接返回已有 `doc_id` 和完成状态，不新增
binding，也不重新解析；不同 `session_id` 不会触发重复错误。

## 查询任务状态

按任务查询：

`GET /ingestion/v1/tasks/{task_id}`

按文档和租户范围查询：

`GET /ingestion/v1/documents/{doc_id}/status?user_id=user-1&kb_id=kb-1`

状态响应的 `data`：

```json
{
  "doc_id": "document-uuid",
  "user_id": "user-1",
  "task_id": "task-uuid",
  "status": "processing",
  "progress": 65,
  "current_step": "structuring",
  "total_pages": 20,
  "processed_pages": 13,
  "tree_node_count": 42,
  "error_message": null,
  "started_at": "2026-07-16T08:00:00Z",
  "completed_at": null
}
```

`status` 会经历 `queued`、`parsing`、`extracting`、
`structuring`、`storing`，最终为 `completed` 或 `failed`。

## 删除文档

`DELETE /ingestion/v1/documents/{doc_id}?user_id=user-1&kb_id=kb-1`

删除范围由 `user_id + kb_id + doc_id` 唯一确定。运行中的任务返回 HTTP 409，
不存在的文档返回 HTTP 404。

## 清理会话临时文档

`DELETE /ingestion/v1/sessions/{session_id}/temp-documents`

可选查询参数：`kb_id`。接口会取消记录在该会话下的临时任务并清理对应文档数据。文档读取
不按 session 隔离，因此调用方必须根据自己的完整会话引用记录确认可以删除后再显式调用。

## 内部 MinerU raw 修复接口

`POST /ingestion/v1/internal/documents/raw/repair` 仅供同一可信网络内的检索服务调用，不是面向
业务前端的公开入库接口。请求使用已有文档范围：

```json
{
  "user_id": "user-1",
  "kb_id": "kb-1",
  "doc_id": "doc-1"
}
```

接口原子认领修复租约并返回 HTTP 202。Worker 从文档已有的 `file_oss_key` 下载源文件，校验
源文件内容生成的文档 ID 必须与现有 `doc_id` 一致，只运行 MinerU 解析并补写
`documents.raw.raw_mineru`。它不会创建新文档，也不会更新摘要、page、node 或绑定关系。

标记语义按触发路径区分：首次 MinerU 成功入库只保存 `raw_mineru`，不创建 repair 标记；首次
MinerU 失败但 native fallback 成功时，会保存一个可重试的 `raw_mineru_repair=failed` 标记和
冷却时间。已有文档因重复入库或 `/raw` 请求触发补解析时，共用同一套原子 claim、租约、
`queued/processing/completed/failed` 状态和失败分类，并且都只补写 raw 字段。

若 `file_oss_key` 缺失，返回 422 `RAW_MINERU_SOURCE_MISSING`，错误信息会提示调用方删除旧文档并
通过标准入库重新创建。来源不匹配或 MinerU 结果不可用会成为不可重试终态；网络、OSS、MinerU
超时等瞬时错误会写入可重试状态和下一次允许尝试时间。修复状态由检索服务的
`GET /rag/v1/documents/raw/status` 对外提供，本内部接口不需要新增公开状态接口。

## 健康检查

- `GET /healthz`：服务健康状态
- `GET /`：服务名称和基础信息

## 常见错误

| HTTP 状态 | 场景 |
|---:|---|
| 400 | OSS Key、文件字段或回调配置非法 |
| 404 | 任务或租户范围内的文档不存在 |
| 409 | 重复入库、文档正在处理或运行中任务不可删除 |
| 422 | JSON 字段、枚举或类型不符合契约 |
| 500/503 | 存储或服务依赖不可用 |

客户端应保存 `task_id` 和 `doc_ids`，并以状态查询结果为准，不应根据
`estimated_seconds` 推断任务已经完成。
