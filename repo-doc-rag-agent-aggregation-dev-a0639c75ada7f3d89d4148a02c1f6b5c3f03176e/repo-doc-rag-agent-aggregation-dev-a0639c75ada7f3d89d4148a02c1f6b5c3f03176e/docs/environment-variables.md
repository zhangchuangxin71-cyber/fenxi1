# 统一部署环境变量

部署脚本读取 `env/*.env`，Git 只保存相邻的 `*.env.example`。不要把真实 env、数据库密码、
Ark key 或 OSS key 提交到仓库。
部署入口会在读取配置前把七个真实 env 文件收紧为 `0600`，并在宿主文件系统无法保持
该权限时直接终止。

## 准备方法

```bash
cp env/mineru.env.example env/mineru.env
cp env/ingestion.env.example env/ingestion.env
cp env/retrieval.env.example env/retrieval.env
cp env/knowledge-chat.env.example env/knowledge-chat.env
cp env/report-agent.env.example env/report-agent.env
cp env/wechat-article.env.example env/wechat-article.env
cp env/wechat-article-db.env.example env/wechat-article-db.env
```

## 必填项

| 文件 | 必填配置 | 说明 |
|---|---|---|
| `mineru.env` | `MINERU_MODEL_SOURCE` | CPU 默认 `modelscope`；模型下载和缓存能力需在现场确认。 |
| `ingestion.env` | `POSTGRES_DSN`、`ARK_API_KEY`、`OSS_BUCKET`、`OSS_ENDPOINT`、`OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET` | DSN 指向已有生产数据库。 |
| `retrieval.env` | `POSTGRES_DSN`、`ARK_API_KEY` | 新检索服务没有服务级 API Key 鉴权，必须位于可信网络边界。 |
| `knowledge-chat.env` | `ARK_API_KEY`；`AUTH_ENABLED=true` 时还需 `KNOWLEDGE_CHAT_INBOUND_API_KEYS` | 入站 key 使用逗号分隔的 `caller:key`，部署脚本会条件预检；不会转发给检索服务。 |
| `report-agent.env` | `PG_DSN`、`ARK_API_KEY` | retrieval backend 固定为 remote。 |
| `wechat-article.env` | `DATABASE_URL`、`ARK_API_KEY` | 使用独立 PostgreSQL database；生产还必须保持 `APP_PORT=8140`、`AGENT_SERVER_URL=http://wechat-article-runtime:8141` 和 `RETRIEVAL_BASE_URL=http://rag-retrieval-service:8120`。 |
| `wechat-article-db.env` | `DATABASE_ADMIN_URL` | 仅一次性数据库初始化容器使用；连接维护库的管理员账号必须具备 `CREATE ROLE` 和 `CREATE DATABASE`，不会注入 runtime/adapter。 |

大体积运行数据统一绑定到 `/mnt/data`：MinerU 模型缓存与配置位于 `/mnt/data/mineru/models`，输出位于 `/mnt/data/mineru/output`，入库 workspace 位于 `/mnt/data/ingestion/workspace`，入库和报告日志分别位于 `/mnt/data/logs/ingestion`、`/mnt/data/logs/report-agent`。这些路径由部署脚本创建。微信文章 runtime 的短期 run registry 使用 `wechat-article-runtime-data` named volume，持久化 checkpoint 和 artifact 使用独立 PostgreSQL。`mineru/model` 是随镜像发布的 Python 源码包，不是模型权重目录。

数据库 DSN 不能写 `127.0.0.1` 或 `localhost`，因为那指向容器自身。填写生产数据库的内网 DNS/IP，并确认防火墙允许 Docker bridge 网段连接。
部署时 `wechat-article-db-init` 会先创建微信文章独立 role/database、业务表和 LangGraph checkpointer 表，
成功后才启动 runtime 与 adapter；重复部署可幂等执行。

服务日志级别由 `LOG_LEVEL` 控制，生产保持 `INFO`；只有短时排障才使用 `DEBUG`，排障完成后立即恢复，避免输出量和潜在上下文信息增加。

## 服务间固定地址

以下值不应改成宿主 IP：

```env
MINERU_API_URL=http://mineru-api:8135
RAG_RETRIEVAL_SERVICE_URL=http://rag-retrieval-service:8120
RAG_MAX_RETURN_TOKENS=180000
AGENT_SERVER_URL=http://wechat-article-runtime:8141
RETRIEVAL_BASE_URL=http://rag-retrieval-service:8120
WEB_SEARCH_ENABLED=false
```

`WEB_SEARCH_ENABLED` 是微信文章服务的可选全局联网开关。默认关闭；显式开启后，素材不足时会复用
`ARK_API_KEY` 调用 Ark Responses 内置 `web_search`，不需要另一套搜索 API Key。

## 容量初值

- MinerU CPU API 并发 4，入库 MinerU client 并发 4。
- 入库普通/临时 worker 为 4/2，单任务索引并发 1，单文档 Ark 并发 5。
- 检索 LLM 并发 16、数据库池最大 30。
- 检索 query 分类默认使用 `RAG_QUERY_CLASSIFICATION_STRATEGY=robust`；低延迟对照可切为 `fast`，修改后需重启进程。
- 知识问答 Ark 并发 30。
- 报告 thinking 默认开启，远程检索 timeout 180 秒。
- 微信文章 adapter 活跃/排队 run 为 30/90，Ark 并发 30、检索并发 30、Seedream 并发 8；
  runtime 仅在容器网络暴露 8141，adapter 对宿主机公开 8140。

这些都是单实例初值，不代表 Ark、数据库或 CPU 的真实配额。上线压测出现 429、CPU 饱和或数据库连接不足时，应按对应服务子目录中的详细文档调整：

- `src/MinerU/docs/environment-variables.md`
- `src/repo-doc-ingestion/docs/environment-variables.md`
- `src/rag-retrieval-service/docs/environment-variables.md`
- `src/rag-knowledge-chat/docs/environment-variables.md`
- `src/rag-report-agent/docs/environment-variables.md`
- `src/wechat-article-agent/docs/environment-variables.md`
