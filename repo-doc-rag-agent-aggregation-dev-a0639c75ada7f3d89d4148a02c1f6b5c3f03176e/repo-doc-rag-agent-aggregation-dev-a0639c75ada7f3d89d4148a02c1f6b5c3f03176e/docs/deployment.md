# 生产部署说明

## 前置条件

- 单台 Linux 服务器，已安装 Docker Engine 和 Compose plugin。
- 生产 PostgreSQL 已存在：PageIndex database 的 schema 与现有入库/检索兼容，不由统一部署初始化或
  清理；微信文章使用独立 database，应用启动时只初始化自己的 checkpoint 和 `article_artifacts` 表。
- 至少准备 CPU MinerU 所需的内存、磁盘和构建时间；建议 16GB 以上内存，实际以真实 PDF 压测为准。
- 服务器能访问 Debian/Python/PyTorch/ModelScope 等 Dockerfile 使用的依赖源。
- 8135、8100、8115、8120、8130、8140 未被旧服务占用；微信文章 runtime 8141 不映射宿主机。

## 构建模型

顶层 Compose 对每个 service 使用 `build.context` 和子项目 Dockerfile。所谓“现场安装依赖”发生在 Docker build 阶段，依赖仍封装在本机生成的容器镜像层中；不需要从公司仓库拉取预构建业务镜像。

首次构建最慢的是 MinerU CPU。部署脚本在 MinerU 镜像不存在时自动构建；镜像存在时，
普通部署默认构建入库、检索、知识库问答、报告生成，以及微信文章 runtime/adapter 六个应用容器。修改 MinerU 源码、
`pyproject.toml` 或 Dockerfile 后使用：

```bash
bash scripts/deploy.sh --rebuild-mineru
```

MinerU Dockerfile 会先根据 `pyproject.toml` 安装稳定的 PyTorch 和 pipeline 依赖层，再复制
源码并以 `--no-deps` 安装项目。普通源码变化因此不会重复生成约数 GB 的依赖层。

## 构建镜像加速

统一 Compose 默认覆盖首次部署时的主要外部下载点：

- Python 基础镜像：DaoCloud Docker Hub 代理。
- LangGraph API 基础镜像：默认使用官方镜像名，可通过构建变量切换到公司 Harbor 或可用的
  Docker Hub 代理，避免在业务 Dockerfile 中绑定某个不稳定的公共代理域名。
- Debian 与 Debian Security：阿里云。
- pip 与 uv Python 依赖：清华 PyPI；MinerU 的额外依赖使用阿里云 PyPI。
- PyTorch CPU wheels：清华 PyTorch 镜像。
- uv 工具本身：从清华 PyPI 安装固定版本，不再从 GHCR 拉取工具镜像。
- MinerU 运行时模型：`MINERU_MODEL_SOURCE=modelscope`，并持久化到 `/mnt/data/mineru/models`。

现场有公司内网 Harbor、APT 或 PyPI 时，可以在执行部署脚本前通过以下宿主环境变量覆盖，
不需要修改 Dockerfile：

```bash
export RAG_BUILD_PYTHON_SLIM_IMAGE=harbor.example.com/library/python:3.12-slim
export RAG_BUILD_PYTHON_SLIM_BOOKWORM_IMAGE=harbor.example.com/library/python:3.12-slim-bookworm
export RAG_BUILD_LANGGRAPH_API_IMAGE=harbor.example.com/langchain/langgraph-api:0.12.3-py3.12-bookworm
export RAG_BUILD_PYPI_MIRROR_BASE=https://mirror.example.com/pypi
export RAG_BUILD_DEBIAN_MIRROR=https://mirror.example.com/debian
export RAG_BUILD_DEBIAN_SECURITY_MIRROR=https://mirror.example.com/debian-security
export RAG_BUILD_PIP_INDEX_URL=https://mirror.example.com/pypi/simple
export RAG_BUILD_PIP_EXTRA_INDEX_URL=https://mirror.example.com/pypi/simple
export RAG_BUILD_PIP_TRUSTED_HOST=mirror.example.com
export RAG_BUILD_PYTORCH_INDEX_URL=https://mirror.example.com/pytorch-wheels/cpu
bash scripts/deploy.sh
```

检索服务和知识库问答使用 `uv sync --frozen`，依赖制品地址已锁定在各自的 `uv.lock`
中。若现场必须改用内网 PyPI，需要在两个项目目录分别执行
`uv lock --default-index https://mirror.example.com/pypi/simple` 并提交更新后的锁文件；随后可设置
`RAG_BUILD_UV_DEFAULT_INDEX`，使构建阶段安装 uv 工具时也使用同一内网源。仅设置该环境变量
不会重写已锁定的依赖地址，这是保持可复现构建所必需的限制。

这些变量只控制 Docker build，不会注入业务容器运行环境。部署慢时使用
`docker compose build --progress=plain` 查看具体停在基础镜像、apt、pip/uv、PyTorch 还是
MinerU 模型下载阶段。

微信文章 runtime 明确使用 `langgraph-runtime-inmem` 执行调度，并通过自定义 PostgreSQL
checkpointer 持久化状态，不依赖 Redis。不要给该容器配置 `REDIS_URI`；LangGraph 的通用分布式
部署模板会使用 Redis，但不适用于当前 runtime。生产镜像仓库地址应通过
`RAG_BUILD_LANGGRAPH_API_IMAGE` 注入，避免把仓库凭据或临时公共代理写进 Compose。

固定版本的 `langchain/langgraph-api` 基础镜像自带面向分布式 runtime 的
`/storage/entrypoint.sh`。`Runtime.Dockerfile` 必须使用 `ENTRYPOINT []` 清除该继承入口，再由明确的
`python -m langgraph_api.cli --runtime-edition inmem` 启动。只覆盖 `CMD` 会继续进入基础镜像的
uvicorn 启动路径，表现为强制读取 `REDIS_URI`，并可能在内置 SQLite 初始化前启动 run sweeper、
出现 `no such table: run`。不得通过配置一个实际 Redis 地址掩盖该启动入口错误。

本地 `langgraph.json` 使用相对路径，服务于项目目录中的 `langgraph dev`，不能直接在工作目录为
`/runtime-data` 的生产容器中使用。生产 CLI 固定读取 `langgraph.runtime.json`；该文件把 graph 与
checkpointer 路径写成 `/deps/wechat-article-agent/...` 的镜像内绝对路径。若错误使用本地配置，日志会
把 checkpointer 解析为不存在的 `/runtime-data/app/persistence/checkpointer.py`。部署契约测试必须
同时校验 ENTRYPOINT、runtime edition、运行配置文件和全部容器绝对路径。

生产 Compose 为七个容器统一使用 Docker `json-file` 日志轮转：单文件上限 `50MB`，最多
保留 `3` 个文件。BuildKit 缓存属于宿主机全局资源，不由部署脚本自动清理，以免影响同机
其他项目；应由运维按保留周期单独执行 `docker builder prune`。

## 启动顺序

1. MinerU CPU 先启动并通过 `/health`。
2. 入库服务启动。
3. 检索服务启动并通过 `/readyz` 验证外部 PostgreSQL。
4. 知识问答和报告生成在检索 healthy 后启动。
5. 微信文章 runtime 连接独立 PostgreSQL 并通过 `/ok` 后，adapter 才启动；adapter 同时依赖检索 healthy。

Compose 的依赖只覆盖容器启动顺序。外部 Ark、OSS 和 PostgreSQL 的运行状态仍需由各服务 timeout、日志和监控观察。

启动前，顶层脚本只读取 `env/*.env`，并将这六个文件权限收紧和复核为 `0600`。子项目
目录中的 `.env` 只服务于独立本地运行，不会被顶层 Compose 注入容器。

## 端口冲突

部署脚本不会自动停止旧容器，因为无法安全判断旧服务是否仍承载流量。主管应先停掉原 8100 入库和 8115 报告服务，以及其他占用目标端口的进程。若端口冲突，Compose 会明确失败，不应通过改端口绕过既定接口契约。

## 防火墙

当前 Compose 的 `"PORT:PORT"` 映射监听所有宿主网卡。至少应做到：

- 8135 只允许入库服务或运维来源。
- 8100 只允许内部上传/任务系统。
- 8115 只允许前端网关或内部调用方。
- 8120 没有服务级 API Key 鉴权，必须限制为可信内网调用；8130 默认也没有应用层鉴权，
  必须限制为受控网关或内网来源。
- 8140 只允许产品网关或可信上游；微信文章 runtime 8141 只使用 Compose `expose`，不映射宿主机。
- 不向公网直接开放 PostgreSQL。

## 数据盘目录

部署脚本会在启动容器前创建以下 bind mount 源目录，并在不可写时直接失败：

- `/mnt/data/mineru/models`：MinerU 模型缓存与生成的模型配置
- `/mnt/data/mineru/output`
- `/mnt/data/ingestion/workspace`
- `/mnt/data/logs/ingestion`
- `/mnt/data/logs/report-agent`

这些目录不会被 `docker compose down` 删除。数据库继续使用已有生产 PostgreSQL 的 DSN。

微信文章 runtime 的短期 run registry 使用 `wechat-article-runtime-data` named volume；持久化
checkpoint 和业务 artifact 使用独立 PostgreSQL database。运维使用普通 `docker compose down`，
不要使用 `down -v` 删除 runtime volume。

## 报告服务第一版

已验证的最小路径：

- 健康检查和 SSE。
- 远程检索及上游 429/鉴权/连接错误映射。
- 生成报告大纲。
- 修改报告大纲。
- 根据确认大纲撰写报告。

已知但不阻塞当前低优先级首版的问题：

- 身份/闲聊的引用事件行为与旧测试不一致。
- 已有报告的全文编辑存在失败用例。
- 问答与报告组合多意图存在失败用例。
- 部分报告 prompt 格式保护测试与实现不一致。

甲方演示应优先走“选择文档 -> 生成大纲 -> 确认大纲 -> 生成报告”标准路径，不把上述高级组合场景作为第一版承诺。

报告服务生产固定使用 `RAG_RETRIEVAL_BACKEND=remote`。独立检索服务只返回 page 原文
或工具生成的元信息 chunk，不返回 node chunk；报告服务会动态计算
`max_return_tokens=4096–32768`。接口 `session_id` 会透传给检索服务校验临时文档，
但不会恢复报告 Agent 历史。

## 故障定位

```bash
docker compose -f docker-compose.yaml ps
docker compose -f docker-compose.yaml logs --tail=200 mineru-api
docker compose -f docker-compose.yaml logs --tail=200 repo-doc-ingestion
docker compose -f docker-compose.yaml logs --tail=200 rag-retrieval-service
docker compose -f docker-compose.yaml logs --tail=200 rag-knowledge-chat
docker compose -f docker-compose.yaml logs --tail=200 rag-report-agent
docker compose -f docker-compose.yaml logs --tail=200 wechat-article-runtime
docker compose -f docker-compose.yaml logs --tail=200 wechat-article-agent
```

常见问题：

- 检索一直 unhealthy：检查 PageIndex DSN 的生产主机、账号、密码、网络 ACL 和 schema。
- 微信文章数据库失败：检查其独立 `DATABASE_URL`、建表权限、连接池配额和三天 TTL 清理日志。
- 入库无法调用 MinerU：确认使用 `http://mineru-api:8135`，不要使用宿主端口。
- 问答/报告返回检索 503：检查检索容器、容器 DNS、HTTP timeout、PostgreSQL、LLM
  熔断状态和网络策略。当前检索服务不返回鉴权 401。
- MinerU 首次启动慢：检查模型下载、磁盘空间和容器日志，必要时增加 health timeout。
- Ark 429：降低单实例并发或申请上游配额，不要盲目增加重试。
- 微信文章 adapter 未就绪：先检查私有 runtime `/ok`、独立 `DATABASE_URL`、
  `AGENT_SERVER_URL=http://wechat-article-runtime:8141` 和检索服务 DNS。
