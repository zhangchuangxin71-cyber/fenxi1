# 微信公众号文章 Agent

这是一个基于 LangGraph 的微信公众号文章生成服务。项目使用 PostgreSQL 保存 LangGraph checkpoint
和版本化业务产物，通过 Responses-like SSE 向调用方提供运行过程、HITL 审批、产物和最终 HTML。

当前实现包括：

- 素材调研、任务书、大纲、Markdown 正文、AI 配图和 HTML 排版的单向工作流；
- 用户文档研读与可选 Ark 联网素材搜索的并行处理、来源展示和事实冲突 HITL；
- 文档澄清以及任务书、大纲、正文三个阶段的 HITL；
- 完成后的局部修改、artifact revision 和断点恢复；
- 面向前端的 Responses-like 流式接口和正式取消接口；
- 节点、工具、reasoning、产物和审批卡片的实时可观测事件；
- 仅在开发环境使用的本地调试面板。

## 文档

- [实现设计](docs/implement.md)：架构、图编排、持久化、容错和实施契约。
- [联网素材调研设计](docs/web_search_design.md)：Ark 搜索、素材模型、冲突处理和验收边界。
- [Responses-like 扩展协议规范](docs/responses-like-protocol.md)：供其他 Agent 的 adapter 实现者复用。
- [前端接入指南](docs/frontend-integration.md)：请求、SSE、HITL 卡片和取消操作的消费方式。
- [环境变量说明](docs/environment-variables.md)：本地/生产模板、容量初值、超时、熔断和上线检查。
- [真实模型烟测报告](docs/smoke-test-20260812.md)：真实 Ark、Seedream、检索服务和边界场景的验收结果。

后续新增或修改的项目文档统一使用中文编写。

## 本地开发

项目的 `.env` 是指向 `../../env/wechat-article.env` 的符号链接。在当前目录安装依赖并分别启动三个
进程：

```bash
uv sync
uv run langgraph dev --host 127.0.0.1 --port 8242 --no-browser --no-reload
uv run uvicorn app.main:app --host 127.0.0.1 --port 8240
uv run uvicorn dev.server:app --host 127.0.0.1 --port 8245
```

本地地址：

| 服务 | 地址 | 用途 |
| --- | --- | --- |
| 产品 API | `http://127.0.0.1:8240` | 对外提供 Responses-like 接口 |
| 内部 Agent Server | `http://127.0.0.1:8242` | 仅供 adapter 调用 LangGraph 协议 |
| 开发面板 | `http://127.0.0.1:8245` | 本地调试和人工验收 |

产品接口为 `POST /v1/responses`；正式取消运行使用
`POST /v1/responses/{response_id}/cancel`。开发面板是独立的开发进程，不会复制到生产镜像。
开发面板输入框的默认 Agent 地址、`user_id`、`kb_id` 和 `session_id` 配置在
`dev/config.yaml`，修改后重启面板即可生效。

`langgraph.json` 是需要提交的 LangGraph 图和 checkpointer 配置；`.langgraph_api/` 是本地 Agent
Server 生成的运行数据，已加入 `.gitignore`，不得提交。

## 验证

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app dev tests
uv run pytest -q
WECHAT_RUN_LIVE_CONTRACTS=1 uv run pytest -q tests/e2e/test_agent_server_runtime.py
```

## 部署边界

当前部署由两个内部进程组成，但只公开一个产品端口：

- `Dockerfile` 构建公开的 adapter，生产端口为 `8140`；
- `Runtime.Dockerfile` 构建私有的 LangGraph Agent Server，容器网络端口为 `8141`，不映射到宿主机。

私有 runtime 使用固定的进程内 Agent Server backend，不依赖 Redis。短期 run registry 挂载到
`/runtime-data/.langgraph_api`，持久化 Graph checkpoint 使用项目自定义的 PostgreSQL checkpointer。
runtime 必须保持单副本；adapter 的 TTL 清理会先删除 Agent Server thread，再删除该 session 的全部
artifact revision。

生产部署入口是仓库根目录的 `docker-compose.yaml` 和 `scripts/deploy.sh`。当前开发容器不包含
Docker CLI，因此这里只进行了部署文件静态检查，没有构建生产镜像。
