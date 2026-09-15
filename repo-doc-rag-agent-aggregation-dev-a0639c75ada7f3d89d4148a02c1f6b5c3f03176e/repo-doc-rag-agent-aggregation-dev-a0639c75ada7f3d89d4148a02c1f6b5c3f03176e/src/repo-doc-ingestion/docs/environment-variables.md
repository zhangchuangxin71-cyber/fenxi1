# 入库服务环境变量说明

本文对应当前 `.env.example`，生产部署前应复制为受权限保护且不进入 Git 的 `.env`。推荐值以“单实例、6 个入库 worker、Ark 单文档并发 5”为基线；真实的数据库地址、磁盘、模型配额、OSS 和密钥必须由部署环境提供。

## 服务与 PostgreSQL 容器

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `APP_HOST` | `127.0.0.1` | Compose 对宿主机的绑定地址。服务本身没有 API Key 鉴权，只应由网关或私网访问；统一 Compose 内部调用不需要暴露宿主端口。 |
| `APP_PORT` | `8100` | FastAPI 监听/映射端口。 |
| `POSTGRES_USER` | 专用数据库账号，如 `pageindex_app` | PostgreSQL 容器初始化用户，不建议生产沿用超级用户。 |
| `POSTGRES_PASSWORD` | 随机强密码或 Secret 注入 | PostgreSQL 密码，禁止提交 Git。 |
| `POSTGRES_DB` | `pageindex` | PageIndex 数据库名。 |
| `PGDATA_HOST_PATH` | 数据盘绝对路径，如 `/mnt/data/pageindex/postgres` | PostgreSQL 持久化目录；必须先确认磁盘容量、权限和备份方案。 |
| `TZ` | `Asia/Shanghai` | 容器时区。数据库时间字段仍应按 UTC 约定使用。 |

## Ark / LLM

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `ARK_API_KEY` | Secret 注入 | 摘要与视觉 OCR 使用的 Ark 密钥。 |
| `ARK_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | Ark OpenAI-compatible 地址。 |
| `ARK_MODEL` | `doubao-seed-2-0-mini-260428` | 摘要模型。上线前确认账号已开通该 endpoint。 |
| `ARK_ENDPOINT_ID` | 与 `ARK_MODEL` 一致 | 兼容旧调用路径的模型/endpoint 别名。 |
| `ARK_VISION_MODEL` | `doubao-seed-2-0-mini-260428` | 复杂 PDF 页面/OCR 的视觉模型。 |
| `DA_LLM_TIMEOUT_SECONDS` | `45` | 单次 LLM 请求超时。 |
| `INGEST_SUMMARY_CONCURRENCY` | `5` | 单文档摘要并发。 |
| `DA_SUMMARY_CONCURRENCY` | `5` | 兼容及 ETA 计算使用的摘要并发别名，应与上一项一致。 |
| `INGEST_SUMMARY_RATE_LIMIT_PER_SEC` | `5.0` | 单进程摘要调用节流，约 300 次/分钟。若 Ark 配额更低必须下调。 |
| `INGEST_PDF_VISION_CONCURRENCY` | `5` | 单文档视觉页并发。 |
| `INGEST_PDF_VISION_RATE_LIMIT_PER_SEC` | `5.0` | 单进程视觉调用节流，约 300 次/分钟。 |
| `DA_PDF_VISION_CONCURRENCY` | `5` | 兼容及 ETA 计算使用的视觉并发别名。 |

总体理论上限约为 `(INGEST_WORKER_CONCURRENCY + TEMP_INGEST_WORKER_CONCURRENCY) * INGEST_INDEX_CONCURRENCY * 单文档模型并发`，当前为 30。多实例部署会按实例数倍增，不能把进程内节流当成账号级全局限流。

## 数据库与对象存储

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `POSTGRES_DSN` | `postgresql://<user>:<secret>@postgres:5432/pageindex` | PageIndex 与入库任务库连接串。统一 Compose 中使用服务名，容器内不要写 `127.0.0.1`。 |
| `DA_USER_ID` | `system` | 没有显式租户时的兼容默认 user；业务 API 应始终显式传 user_id。 |
| `PAGEINDEX_USER_ID` | `system` | PageIndex 兼容默认 user，应与上一项一致。 |
| `OSS_BUCKET` | 实际 bucket 名 | 文档输入和日志归档 bucket。 |
| `OSS_ENDPOINT` | 实际地域 endpoint | 例如 `https://oss-cn-shenzhen.aliyuncs.com`。 |
| `OSS_REGION` | 实际地域，如 `cn-shenzhen` | OSS 地域。 |
| `OSS_ACCESS_KEY_ID` | Secret 注入 | OSS 访问 ID，建议最小权限 RAM 账号。 |
| `OSS_ACCESS_KEY_SECRET` | Secret 注入 | OSS 密钥，禁止提交 Git。 |

## MinerU 与解析

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `INGEST_PARSER_BACKEND` | `mineru` | 使用 MinerU 主解析链路；`native` 仅作为内置解析器。 |
| `MINERU_API_URL` | `http://mineru-api:8000` | 当前 MinerU Compose 的 API 容器地址。服务间使用容器端口，不使用宿主映射端口。 |
| `MINERU_BACKEND` | `pipeline` | MinerU 后端。 |
| `MINERU_PARSE_METHOD` | `auto` | 自动选择解析方式。 |
| `MINERU_LANG` | `ch` | 默认文档语言。 |
| `MINERU_USE_ASYNC_TASKS` | `true` | 使用 MinerU 异步任务接口。 |
| `MINERU_FALLBACK_TO_NATIVE` | `true` | MinerU 不可用时允许原生解析，保证可用性；结果质量可能降级，应记录告警。 |
| `MINERU_CLIENT_CONCURRENCY` | `4` | 本实例同时提交/轮询 MinerU 的上限。需结合 MinerU GPU/CPU 容量调整。 |
| `MINERU_PARSE_TIMEOUT_SECONDS` | `1800` | 单文档 MinerU 解析上限。 |
| `MINERU_PARSE_RETRY_TIMES` | `2` | 可重试错误的重试次数。 |
| `MINERU_PARSE_RETRY_BACKOFF_BASE` | `1.5` | 指数退避基数（秒）。 |
| `MINERU_TASK_POLL_INTERVAL_SECONDS` | `2` | 异步任务轮询间隔。 |
| `INGEST_WEAK_HEADING_SPLIT_ENABLED` | `true` | 对 MinerU 的超长叶节点启用保守弱标题切分。 |
| `INGEST_WEAK_HEADING_SPLIT_MIN_CHARS` | `800` | 弱标题切分的最小节点字符数。 |
| `INGEST_WEAK_HEADING_SPLIT_MAX_CHARS` | `8000` | 超长节点的目标上界。 |

## 入库任务

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `INGEST_API_PREFIX` | `/ingestion/v1` | API 路由前缀。 |
| `INGEST_HEALTH_PATH` | `/healthz` | 健康检查路径。 |
| `INGEST_WORKER_CONCURRENCY` | `4` | 普通任务 worker 数。 |
| `TEMP_INGEST_WORKER_CONCURRENCY` | `2` | 临时文档高优先级 worker 数。 |
| `INGEST_DB_BATCH_SIZE` | `500` | 数据库批量写入大小。 |
| `INGEST_EVENT_PROGRESS_STEP` | `5` | 进度至少变化该百分点后持久化事件。 |
| `INGEST_EVENT_MIN_INTERVAL_SECONDS` | `1` | 进度事件最小时间间隔。 |
| `INGEST_INDEX_CONCURRENCY` | `1` | 每个任务同时处理的文档数；会乘算模型并发，当前不建议提高。 |
| `INGEST_TASK_TIMEOUT_SECONDS` | `2400` | 整个入库任务 deadline，应高于 MinerU timeout。 |
| `INGEST_EXPOSE_RUNTIME_ERROR` | `false` | 禁止向调用方暴露内部异常细节。 |
| `INGEST_WORKSPACE_DIR` | 留空或挂载临时数据盘目录 | 留空使用应用默认 workspace；大文件场景应确保容量和清理策略。 |

## 日志、回调与清理

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `INGEST_LOG_FILE` | `app.log` | 本地日志文件。 |
| `INGEST_LOG_ARCHIVE_ENABLED` | `true` | 正常关闭时将 Markdown 日志归档到 OSS。 |
| `INGEST_LOG_OSS_PREFIX` | `prod/doc-ingestion/logs` | OSS 日志前缀。 |
| `INGEST_LOG_TIMEZONE` | `Asia/Shanghai` | 日志展示时区。 |
| `INGEST_LOG_DELETE_ON_SHUTDOWN` | `true` | 成功归档后删除本地日志；异常退出仍依赖容器日志采集。 |
| `INGEST_CALLBACK_ALLOWED_SCHEMES` | `https`（内网确需 HTTP 时用 `http,https`） | 回调 URL 协议白名单。 |
| `INGEST_CALLBACK_WHITELIST` | 明确的业务域名 CSV | 防 SSRF 的 hostname 白名单；启用回调前必填。 |
| `INGEST_CALLBACK_ENABLED` | `false`，完成白名单和密钥配置后再改 `true` | 回调总开关。 |
| `INGEST_CALLBACK_SECRET` | 随机 Secret | 回调签名密钥。 |
| `INGEST_CALLBACK_CONNECT_TIMEOUT` | `3` | 回调建连超时。 |
| `INGEST_CALLBACK_READ_TIMEOUT` | `5` | 回调读取超时。 |
| `INGEST_CALLBACK_MAX_RETRIES` | `3` | 回调重试次数。 |
| `INGEST_CALLBACK_BASE_BACKOFF` | `2` | 回调指数退避基数（秒）。 |
| `INGEST_CLEANUP_SCHEMA` | `public` | 删除文档时清理的 schema。 |
| `INGEST_CLEANUP_TABLES` | `doc_nodes,doc_pages,documents` | 清理表及顺序，修改前必须核对外键。 |
| `INGEST_TREE_NODES_TABLE` | `doc_nodes` | 树节点表名。 |

## ETA 参数

以下值只影响提交响应中的预计耗时，不改变实际并发或 timeout。上线后应根据生产样本的 P50/P95 校准。

| 配置项 | 推荐初值 | 含义 |
|---|---|---|
| `INGEST_ETA_VISION_PAGE_SECONDS` | `8` | 每视觉页平均秒数。 |
| `INGEST_ETA_SUMMARY_NODE_SECONDS` | `0.8` | 每摘要节点平均秒数。 |
| `INGEST_ETA_NORMAL_PARSE_PAGE_SECONDS` | `0.05` | 普通解析每页平均秒数。 |
| `INGEST_ETA_STORE_SECONDS` | `2` | 持久化固定耗时估计。 |
| `INGEST_ETA_PDF_BYTES_PER_PAGE` | `200000` | 未知页数时 PDF 字节/页估算。 |
| `INGEST_ETA_PPTX_BYTES_PER_PAGE` | `400000` | PPTX 字节/页估算。 |
| `INGEST_ETA_XLSX_BYTES_PER_PAGE` | `120000` | XLSX 字节/页估算。 |
| `INGEST_ETA_DOCX_BYTES_PER_PAGE` | `80000` | DOCX 字节/页估算。 |
| `INGEST_ETA_TEXT_BYTES_PER_PAGE` | `3000` | TXT/MD/HTML 字节/页估算。 |

## 上线检查
