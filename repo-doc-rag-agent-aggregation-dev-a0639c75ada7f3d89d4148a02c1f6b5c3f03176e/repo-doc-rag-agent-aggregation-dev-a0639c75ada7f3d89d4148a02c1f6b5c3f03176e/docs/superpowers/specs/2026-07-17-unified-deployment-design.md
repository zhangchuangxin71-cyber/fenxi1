# RAG Platform 统一部署设计

## 目标

在单台生产服务器上，通过一个 `scripts/deploy.sh` 和一个 `docker-compose.yaml` 现场构建并启动五个业务容器：

- MinerU CPU API：8135
- 文档入库：8100
- 报告生成：8115
- RAG 检索：8120
- 知识库问答：8130

PostgreSQL 已独立部署且包含真实数据，不属于本 Compose。

## 仓库边界

`rag-platform` 保存五个服务的代码快照，每个子项目继续使用自己的 Dockerfile 安装系统和 Python 依赖。快照不包含子仓库 `.git`、真实 `.env`、虚拟环境、缓存、日志、测试输出或 MinerU 解析产物。

原子项目的独立 Compose 和 deploy.sh 保留在快照中，但生产操作入口只有顶层 `scripts/deploy.sh`。README 必须明确禁止混用两种编排方式，避免容器名、端口和网络冲突。

## 容器与网络

五个服务加入同一条 `rag-platform-network` bridge 网络，通过 Compose service name 访问：

- 入库 -> `http://mineru-api:8135`
- 知识库问答 -> `http://rag-retrieval-service:8120`
- 报告生成 -> `http://rag-retrieval-service:8120`

五个宿主端口与容器端口保持相同。按主管要求使用普通 `HOST:CONTAINER` 映射，因此默认监听宿主所有网卡。MinerU、入库和报告没有内置服务鉴权，生产必须通过服务器防火墙或上游网关限制来源。

## 配置与 Secret

Git 只跟踪 `env/*.env.example`。部署前由主管复制成 `env/*.env` 并填写：

- 外部 PostgreSQL DSN
- Ark key、模型 endpoint
- OSS 凭据
- 检索服务 caller allowlist
- 问答/报告访问检索服务的独立 key

Compose 的 `env_file` 将对应文件注入各容器。服务间 URL 在模板中使用固定 Compose service name。部署脚本只校验必需变量存在、占位符已替换、数据库 DSN 未使用容器内 localhost；不会打印 Secret。

## 数据与持久化

PostgreSQL 数据不由本项目挂载或初始化。MinerU 输出使用 named volume，入库 workspace 和报告日志分别使用 named volume。应用镜像和容器可重建，外部数据库与必要产物保持独立。

## 启动与健康检查

`deploy.sh` 执行：

1. 检查 Docker 和 Compose plugin。
2. 检查五个 env 文件与关键配置。
3. 执行 `docker compose config --quiet`。
4. 执行 `docker compose up -d --build --remove-orphans`。
5. 轮询五个容器的 health 状态并在超时后输出 `compose ps` 和最近日志。

依赖关系只用于启动顺序：

- 入库等待 MinerU healthy。
- 问答和报告等待检索 healthy。

外部 PostgreSQL 不由 Compose 管理；检索 healthcheck 使用 `/readyz` 验证数据库，入库启动后由其 `/healthz` 验证进程。

## OpenAPI

`openapi/` 归档五份独立 OpenAPI YAML。保留独立文件而不是合并，避免多个服务的 `/health`、schema 名和 server URL 冲突。文件命名固定：

- `mineru.openapi.yaml`
- `ingestion.openapi.yaml`
- `retrieval.openapi.yaml`
- `knowledge-chat.openapi.yaml`
- `report-agent.openapi.yaml`

YAML 可分别导入同一个 Apifox 项目的五个模块。

## 报告服务首版范围

首版承诺健康检查、SSE、知识库检索、生成报告大纲、修改大纲、按确认大纲撰写报告。暂不把以下既有问题作为上线阻塞：

- 身份/闲聊事件与引用行为不一致。
- 报告全文编辑规则存在失败用例。
- 问答与报告组合的多意图顺序存在失败用例。
- 报告 prompt 的部分格式保护测试与实现不一致。

远程检索适配本身的成功、限流、鉴权失败、连接失败和 SSE 测试必须通过。
