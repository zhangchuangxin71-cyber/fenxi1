# rag-platform

六个业务服务的统一部署仓库。它在现场通过各子项目 Dockerfile 安装依赖并构建七个容器，不依赖预先
发布的业务镜像。微信公众号文章 Agent 由私有 LangGraph runtime 和公开 adapter 两个容器组成。

## 服务

| Compose service           | 宿主/容器端口 | 说明                |
| ------------------------- | ------------: | ------------------- |
| `mineru-api`            | `8135:8135` | MinerU CPU 文档解析 |
| `repo-doc-ingestion`    | `8100:8100` | 文档入库            |
| `rag-report-agent`      | `8115:8115` | 报告生成 SSE        |
| `rag-retrieval-service` | `8120:8120` | 统一检索            |
| `rag-knowledge-chat`    | `8130:8130` | 知识库问答          |
| `wechat-article-runtime` | 仅容器网络 `8141` | 私有 LangGraph Agent Server |
| `wechat-article-agent` | `8140:8140` | 微信公众号文章 Responses-like API |

核心调用链是 `rag-knowledge-chat/rag-report-agent -> rag-retrieval-service -> PostgreSQL`；微信文章链路为
`wechat-article-agent -> wechat-article-runtime/rag-retrieval-service -> PostgreSQL`。
知识库问答保持无状态，历史由每次请求的 `messages` 携带；报告服务的历史和报告状态由
`history/report_context` 携带。两者传给检索服务的 `session_id` 只用于临时文档归属，
不是服务端会话记忆。

PostgreSQL 不在本 Compose 中创建。入库、检索和报告使用既有 PageIndex database；微信文章使用独立
database。四个相关 env 都必须填写已经上线的生产数据库地址。

## 唯一生产部署入口

生产部署只使用：

```bash
bash scripts/deploy.sh
```

`src/*` 中保留的子项目 `compose.yaml/docker-compose.yml` 和 `scripts/deploy.sh` 只用于未来单服务开发、排障或扩展。不要在同一台服务器上同时运行顶层和子项目部署脚本，否则会产生端口、容器和网络冲突。

## 首次部署

1. 安装 Docker Engine 和 Docker Compose plugin。
2. 停止占用 8135、8100、8115、8120、8130、8140 的旧容器或进程。8141 不映射宿主机端口。
3. 准备配置：

```bash
for name in mineru ingestion retrieval knowledge-chat report-agent wechat-article; do
  cp "env/${name}.env.example" "env/${name}.env"
done
```

`env/*.env` 是统一 Compose 的唯一运行配置源。各 `src/<service>/.env` 只用于单服务本地开发，
可以是独立文件或开发者自行建立的软链接；顶层部署不会读取它们。真实 env 文件和软链接均被
Git 忽略，必须通过安全渠道提供到部署机。

4. 按 [环境变量说明](docs/environment-variables.md) 填入真实数据库、Ark 和 OSS 配置。
5. 确认主机可访问生产 PostgreSQL、Ark、OSS 和依赖源。
6. 执行：

```bash
bash scripts/deploy.sh
```

脚本会检查配置、构建并启动七个容器，然后等待健康状态。CPU MinerU 镜像不存在时会在
首次部署中自动构建；后续部署默认复用该镜像，只重建另外六个应用容器。MinerU 首次构建
和模型准备可能耗时较长，默认每个服务最多等待 900 秒；需要延长时：

```bash
DEPLOY_HEALTH_TIMEOUT_SECONDS=1800 bash scripts/deploy.sh
```

## 宿主机数据目录

大体积运行数据直接绑定到服务器数据盘：

- MinerU 模型缓存与配置：`/mnt/data/mineru/models`
- MinerU 输出：`/mnt/data/mineru/output`
- 入库工作目录：`/mnt/data/ingestion/workspace`
- 入库日志：`/mnt/data/logs/ingestion`
- 报告服务日志：`/mnt/data/logs/report-agent`

微信文章 runtime 另使用 `wechat-article-runtime-data` named volume 保存 Agent Server 的短期 run
registry；LangGraph checkpoint 和版本化文章产物仍写入外部 PostgreSQL。普通 `docker compose down`
不会删除该 volume，只有显式 `down -v` 才会删除。

`scripts/deploy.sh` 会创建并检查这些目录可写。生产 PostgreSQL 仍使用外部数据库，不由 Compose 管理。

## 网络与安全

容器加入 `rag-platform-network` 并通过 service name 通信。端口按主管要求直接映射到宿主所有网卡。

`mineru-api`、`repo-doc-ingestion`、`rag-report-agent` 和
`rag-retrieval-service` 当前都没有内置 API Key 鉴权。检索服务只按调用方传入的 doc_id
读取文档，不使用 `user_id`、`kb_id` 或 `session_id` 做 SQL 权限隔离，因此可信上游必须先
完成 doc_id 权限校验。上线前必须用主机防火墙、公司网关或安全组限制来源，不能直接暴露公网。
知识库问答入站鉴权默认关闭，因此 8130 同样必须限制为受控网关或内网来源；应用层
鉴权的可选配置见知识库问答子项目文档。微信文章 Agent 只映射 adapter 8140；私有 runtime 8141
只能由共享容器网络中的 adapter 通过 DNS 访问，不应映射宿主机或直接对调用方开放。

## 更新与运维

更新普通应用代码后重新运行顶层脚本即可。修改 MinerU 源码、依赖或 Dockerfile 后，必须
显式要求重建 MinerU：

```bash
bash scripts/deploy.sh --rebuild-mineru
```

七个容器的 Docker JSON 日志均限制为单文件 `50MB`、最多保留 `3` 个文件。常用只读命令：

```bash
docker compose -f docker-compose.yaml ps
docker compose -f docker-compose.yaml logs -f rag-retrieval-service
docker compose -f docker-compose.yaml logs --tail=200 repo-doc-ingestion
```

停止平台：

```bash
docker compose -f docker-compose.yaml down
```

`/mnt/data` 下的 bind mount 在普通 `down` 和 `down -v` 后均保留；但 `down -v` 会删除微信文章
runtime named volume，因此运维必须使用不带 `-v` 的 `down`。外部 PostgreSQL 不受影响。

完整步骤、故障处理和首版报告服务范围见 [部署说明](docs/deployment.md)。
近期跨服务接口和检索架构变更见 [Changelog](CHANGELOG.md)。

## Apifox

`openapi/` 目前归档五个传统服务的 `*.openapi.yaml`；微信文章的 Responses-like 与 HITL 契约见
[协议规范](src/wechat-article-agent/docs/responses-like-protocol.md) 和
[前端接入指南](src/wechat-article-agent/docs/frontend-integration.md)；联网素材搜索和冲突 HITL 见
[素材调研设计](src/wechat-article-agent/docs/web_search_design.md)。详见 [OpenAPI 归档说明](openapi/README.md)。
