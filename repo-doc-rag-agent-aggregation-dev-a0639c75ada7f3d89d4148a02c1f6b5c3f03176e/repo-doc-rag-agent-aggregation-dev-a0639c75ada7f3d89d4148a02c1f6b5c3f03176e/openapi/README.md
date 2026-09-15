# OpenAPI / Apifox 归档

本目录包含五个服务的独立 OpenAPI 3.1 YAML：

- `mineru.openapi.yaml`
- `ingestion.openapi.yaml`
- `retrieval.openapi.yaml`
- `knowledge-chat.openapi.yaml`
- `report-agent.openapi.yaml`

## 导入 Apifox

建议在同一个 Apifox HTTP 项目中创建五个模块，然后逐份导入。导入时启用 Security Scheme；各服务的 base URL 在 Apifox 环境中分别配置，不把真实 API Key 写入接口定义。

这些文件保留独立路径空间，避免不同服务的 `/health`、`/docs` 和 schema 名冲突。SSE 接口会以 OpenAPI 的 `text/event-stream` 响应导入，具体事件名仍以各服务 API 文档和示例为准。

知识库问答的 OpenAI-compatible 顶层保持无 `session_id`；临时文档作用域位于自定义
`rag.session_id`，并在 `rag.temp_doc_ids` 非空时条件必填。报告服务的顶层
`session_id` 是既有业务/SSE 契约，两者最终都只把该值用于检索临时文档归属，不能
据此推断服务会保存聊天历史。

## 刷新

五个统一部署容器启动后：

```bash
python -m pip install PyYAML
python scripts/export_openapi.py
```

脚本只读取公开的 `/openapi.json`，不读取或输出任何 API Key。若服务运行在不同地址，可重复指定覆盖：

```bash
python scripts/export_openapi.py \
  --service-url mineru=http://127.0.0.1:18000 \
  --service-url ingestion=http://127.0.0.1:8100
```

归档后运行：

```bash
python -m pytest -q tests/test_openapi_contract.py
```
