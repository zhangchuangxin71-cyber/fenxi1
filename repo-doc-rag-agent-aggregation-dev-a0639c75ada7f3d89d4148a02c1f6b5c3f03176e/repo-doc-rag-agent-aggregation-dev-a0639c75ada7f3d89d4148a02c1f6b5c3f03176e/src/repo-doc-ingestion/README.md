# repo-doc-ingestion

多格式文档解析与入库服务。服务面向 Linux 服务器部署，文档来源统一为阿里云 OSS `oss_key`：接口接收 OSS 对象 Key，服务端下载到临时目录，解析为统一的 PageIndex 结构，写入 PostgreSQL，任务完成后自动清理下载文件和临时文件。

完整调用契约见 [API 文档](docs/api.md)。

## 功能

- 支持格式：`pdf`、`doc`、`docx`、`md/markdown`、`txt`、`html`、`xlsx`、`pptx`
- 文档来源：阿里云 OSS `oss_key`
- 输出统一结构：文档元信息、树节点、页内容
- PostgreSQL 持久化：`documents`、`doc_nodes`、`doc_pages`
- 入库任务记录：`ingestion_tasks`、`ingestion_task_events`
- FastAPI 入库接口：普通文档入库、临时文档入库、任务状态查询、文档状态查询、文档删除
- 异步任务处理：提交接口快速返回，后台 worker 执行下载、解析和入库
- 临时文件清理：OSS 文件下载到任务临时目录，任务完成后自动清理
- 支持 Ark/豆包模型：用于节点摘要、复杂 PDF/OCR PDF 视觉解析
- 支持 Docker 部署，镜像内置 LibreOffice 和中文字体依赖

## 目录结构

```text
repo-doc-ingestion/
├─ app/
│  ├─ api/                  # FastAPI 入口
│  ├─ config/               # 环境变量加载
│  ├─ ingestion/            # 入库 API、任务队列、worker、OSS 下载
│  ├─ parser/               # 多格式解析入口与 conversion_core
│  ├─ storage/              # PostgreSQL 存储适配
│  └─ workspace/            # 运行期临时工作目录，不入库
├─ tests/
│  ├─ fixtures/             # 测试文件
│  ├─ test_parser.py
│  ├─ test_ingestion_db.py
│  └─ test_api_ingestion.py
├─ Dockerfile
├─ .dockerignore
├─ .env.example
├─ .gitignore
├─ pytest.ini
├─ requirements.txt
└─ README.md
```

## 一、Linux 环境准备

### 1. 系统依赖

Ubuntu/Debian 示例：

```bash
apt-get update
apt-get install -y libreoffice fonts-noto-cjk fonts-wqy-zenhei
```

说明：

- `libreoffice`：用于 DOCX/PPTX 转换或复杂 Office 文档处理。
- `fonts-noto-cjk`、`fonts-wqy-zenhei`：用于中文字体渲染，避免中文乱码或排版错位。

### 2. Python 环境

建议使用 Python `3.12`。

```bash
cd /path/to/rag-platform/src/repo-doc-ingestion
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

如果服务器访问 PyPI 慢，可使用国内源：

```bash
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```


## 二、PostgreSQL 准备

服务依赖 PostgreSQL 存储文档、节点、分页及入库任务信息。连接串通过 `.env` 中的 `POSTGRES_DSN` 配置。

### 方式 A：使用 Docker 启动 PostgreSQL

```bash
docker run -d \
  --name pg-pageindex \
  -e POSTGRES_PASSWORD=postgres123 \
  -e POSTGRES_DB=pageindex \
  -p 5432:5432 \
  -v pgdata:/var/lib/postgresql/data \
  postgres:16
```

验证：

```bash
docker exec -it pg-pageindex \
  psql -U postgres -d pageindex -c "SELECT version();"
```

对应 `.env`：

```env
POSTGRES_DSN=postgresql://postgres:change_me@127.0.0.1:5432/pageindex
```

### 方式 B：使用已有 PostgreSQL

如果服务器已有 PostgreSQL，请确认数据库已创建：

```sql
CREATE DATABASE pageindex;
```

然后将 `.env` 中的连接串改成真实账号密码：

```env
POSTGRES_DSN=postgresql://your_user:your_password@your_host:5432/pageindex
```

可以用 Python 验证连接：

```bash
cd /path/to/rag-platform/src/repo-doc-ingestion
source .venv/bin/activate
python - <<'PY'
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv('.env', override=True)
dsn = os.getenv('POSTGRES_DSN')
print('POSTGRES_DSN =', dsn)
conn = psycopg2.connect(dsn)
print('db connected ok')
conn.close()
PY
```

### 自动创建的表

服务首次启动或首次入库时会自动创建以下表，无需手工建表：

```text
documents
document_bindings
doc_nodes
doc_pages
ingestion_tasks
ingestion_task_events
```

## 三、配置环境变量

完整字段含义、生产推荐值和上线检查见 [环境变量说明](docs/environment-variables.md)。

大项目统一使用 `env/ingestion.env`，当前目录的 `.env` 是指向该文件的本地软链接。确认并编辑统一配置：

```bash
cd /path/to/rag-platform/src/repo-doc-ingestion
test -f ../../env/ingestion.env
vim ../../env/ingestion.env
```

至少需要配置以下内容：

```env
# PostgreSQL
POSTGRES_DSN=postgresql://postgres:change_me@127.0.0.1:5432/pageindex

# Ark / Doubao
ARK_API_KEY=your_ark_api_key_here
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_MODEL=doubao-seed-2-0-mini-260215
ARK_VISION_MODEL=doubao-seed-2-0-mini-260215

# Aliyun OSS
OSS_BUCKET=your_bucket_name
OSS_ENDPOINT=https://oss-cn-shenzhen.aliyuncs.com
OSS_REGION=cn-shenzhen
OSS_ACCESS_KEY_ID=your_access_key_id
OSS_ACCESS_KEY_SECRET=your_access_key_secret
```

如果服务和 OSS Bucket 在同一区域的阿里云服务器上，优先使用内网 Endpoint，速度更快、也更稳定：

```env
OSS_ENDPOINT=https://oss-cn-shenzhen-internal.aliyuncs.com
```

说明：

- `.env` 包含密钥和数据库密码，不要提交到 Git。
- `OSS_BUCKET` 也兼容写成 `OSS_BUCKET_NAME`。
- OSS 凭证使用 `oss2` 的环境变量凭证读取方式，常用变量为 `OSS_ACCESS_KEY_ID` 和 `OSS_ACCESS_KEY_SECRET`。
- 文档入库接口现在使用 `oss_key`，不再推荐使用服务器本地路径或 Windows 路径。


### MinerU 解析后端

默认解析后端由 `.env` 中的 `INGEST_PARSER_BACKEND` 控制：

```env
# native 使用本仓库自带解析器；mineru 调用外部 MinerU API。
INGEST_PARSER_BACKEND=mineru
MINERU_API_URL=http://127.0.0.1:8135
MINERU_BACKEND=pipeline
MINERU_PARSE_METHOD=auto
MINERU_LANG=ch
MINERU_USE_ASYNC_TASKS=true
MINERU_FALLBACK_TO_NATIVE=true
MINERU_CLIENT_CONCURRENCY=8
MINERU_PARSE_TIMEOUT_SECONDS=1800
MINERU_PARSE_RETRY_TIMES=2
MINERU_TASK_POLL_INTERVAL_SECONDS=2

# 可选：仅对 MinerU 解析出的 md/txt/html 超长叶子节点做弱标题后处理。
INGEST_WEAK_HEADING_SPLIT_ENABLED=false
INGEST_WEAK_HEADING_SPLIT_MIN_CHARS=800
INGEST_WEAK_HEADING_SPLIT_MAX_CHARS=8000
```

MinerU 只替换“文档解析”这一步。解析结果会被 adapter 转换回本服务既有的 PageIndex 结构，后续仍然写入同一套 `documents`、`document_bindings`、`doc_nodes`、`doc_pages` 表，不改变数据库 schema。

对于历史文档缺失 `raw.raw_mineru` 的情况，检索服务可以调用内部修复接口异步补齐。修复复用原 `doc_id` 和既有 OSS 源文件，仅更新 `documents.raw.raw_mineru`；不会重新运行 LLM 摘要、创建新文档或重建 page/node。接口契约见 [API 文档](docs/api.md#内部-mineru-raw-修复接口)。

字段语义保持不变：

- `doc_pages.content`：按页聚合后的文本内容。
- `doc_nodes.title/level/start_index/end_index/text/summary/nodes`：文档树节点、层级、页码范围、节点正文和摘要。
- adapter 会正常生成并传递 `doc_nodes.text` 对应的节点正文；存储层会把该字段写入 `doc_nodes.text`。为避免重复保存大段正文，`documents.raw` 会瘦身：移除 `pages`、binding 作用域字段，以及嵌套 `structure[].text`，完整页文本和节点正文分别以 `doc_pages.content`、`doc_nodes.text` 为准。

- MinerU 后端额外在 <code>documents.raw.raw_mineru</code> 中保留必要的原始解析产物：<code>md_content</code>、<code>content_list</code> 和 <code>middle_json</code>。其中 Markdown 是 MinerU 的原始渲染结果，后两项分别用于顺序内容消费和结构化二次开发。
- 不保存 MinerU <code>model_output</code>、裁剪图片或 Base64 图片。OCR 已识别的文字仍会进入上述结构化结果；纯视觉图片语义和未识别的图片内文字不属于当前入库范围。
- 检索服务通过 <code>POST /rag/v1/documents/raw</code> 读取 <code>raw_mineru</code>，并结合 <code>doc_pages</code> 与 <code>doc_nodes</code> 重建完整文本解析结果。

弱标题后处理：

- `INGEST_WEAK_HEADING_SPLIT_ENABLED=true` 时，仅处理 MinerU 后端解析出的 `md`/`txt` 文档，以及入库前被规范化为 Markdown 的 `html`/`htm` 文档。
- 只处理 leaf node，且只有 `text` 长度超过 `INGEST_WEAK_HEADING_SPLIT_MAX_CHARS` 时才触发。
- 优先识别保守弱标题，例如 `一、标题`、`(一) 标题`、`1.2 标题`、Markdown `# 标题`；代码块内的 Markdown 注释不会被当作标题。
- 切分后短于 `INGEST_WEAK_HEADING_SPLIT_MIN_CHARS` 的片段会与相邻片段合并，避免切得过碎。
- 如果没有可用弱标题，才回退到段落切分和固定长度兜底。
- 该后处理暂不支持 overlap、按 token 切分、强制重切 PDF/DOCX 等策略；这些字段目前不会影响 MinerU 的 md/txt 弱标题后处理。

并发和稳定性：

- MinerU 服务自身有任务队列，并通过 `MINERU_API_MAX_CONCURRENT_REQUESTS` 限制同时执行的解析任务；入库服务侧通过 `MINERU_CLIENT_CONCURRENCY` 限制同时提交到 MinerU 的请求数。
- Ark 调用上界会按“worker 总数 × `INGEST_INDEX_CONCURRENCY` × 单文档摘要/视觉并发”相乘。单实例生产模板使用 `(4 + 2) × 1 × 5 = 30`，不要把每个配置都独立改成 30。
- `INGEST_SUMMARY_RATE_LIMIT_PER_SEC` 和 `INGEST_PDF_VISION_RATE_LIMIT_PER_SEC` 是进程级平滑速率保护，不等于并发上限。
- 批量入库时 `INGEST_INDEX_CONCURRENCY` 控制单个任务内同时解析/索引的文档数，调整前必须重新计算上述乘积。
- 解析请求支持超时、5xx/网络错误重试和退避。
- `MINERU_FALLBACK_TO_NATIVE=true` 时，MinerU 失败且文件格式也被 native parser 支持，会回退到原解析器；设置为 `false` 可用于测试 MinerU 链路是否严格可用。

当前就绪范围与限制：

- 在 `INGEST_PARSER_BACKEND=mineru` 且 MinerU 服务可用时，API 支持的 PDF、DOC、DOCX、PPTX、XLSX、MD、TXT 等格式会优先交给 MinerU 解析；HTML/HTM 会先由本地预处理规范化，再保持原始 `doc_type` 写库；legacy `.doc` 会作为 Word 文档输入接收，内部按既有 `docx` 类型入库。MinerU adapter 内部可识别图片扩展名，但当前入库 API 尚未把图片作为正式 `file_type` 暴露。
- native parser 仍然是生产兜底路径。PDF native 路径会先用 PyMuPDF/PyPDF2 提取文本；当 `PDF_TABLE_MODE=auto` 或 `vision` 且检测到疑似表格页时，会调用 Ark vision 模型补充 Markdown 表格文本。`PDF_PARSER=ark_vision` 时会对页面图片做 OCR/版面文本提取。
- native parser 的 Ark vision 能力主要用于 OCR、版面文本和表格抽取，不会对任意图片、照片、图示生成完整语义描述。MinerU 返回的 image/chart block 目前也只会持久化为 `[图片]` / `[图表]` 标记以及已有 caption/footnote。
- 表格解析当前以 MinerU 输出和 native PDF 表格增强为主，普通 PDF/Word 中的常规表格通常可得到 Markdown 形式文本；复杂扫描表格、跨页表格、嵌入图片中的图表仍需要用评测集持续观察。
- 如果 MinerU 对某个 PDF 失败，入库服务会记录失败 stage 和异常堆栈，并在允许 fallback 时切到 native parser。fallback 可能影响解析质量，但比整篇入库失败更适合当前生产稳定性要求。
- 首次 MinerU 失败但 native fallback 成功时，会在 `raw.raw_mineru_repair` 中记录可重试失败和冷却时间，避免重复入库立即再次调用 MinerU；首次 MinerU 成功不创建 repair 标记。
- 已有文档缺失 `raw_mineru` 时，重复入库与检索 `/raw` 触发的补解析共用同一原子 claim、租约和失败标记，只更新 raw 字段，不修改 doc ID、description、page 或 node。

## 四、启动 FastAPI 服务

前台启动，方便查看日志：

```bash
cd /path/to/rag-platform/src/repo-doc-ingestion
source .venv/bin/activate
uvicorn app.api.app:app --host 0.0.0.0 --port 8100
```

健康检查：

```bash
curl http://127.0.0.1:8100/healthz
```

正常返回：

```json
{"ok": true}
```

后台启动：

```bash
cd /path/to/rag-platform/src/repo-doc-ingestion
source .venv/bin/activate
nohup uvicorn app.api.app:app --host 0.0.0.0 --port 8100 > app.log 2>&1 &
```

查看日志：

```bash
tail -f app.log
```

服务正常停止时会保留本地 `app.log`，并额外上传一份 Markdown 日志到 OSS：

```text
prod/doc-ingestion/logs/YYYYMMDD_HHMMSS.md
```

可通过环境变量调整：

```env
INGEST_LOG_FILE=app.log
INGEST_LOG_ARCHIVE_ENABLED=true
INGEST_LOG_OSS_PREFIX=prod/doc-ingestion/logs
INGEST_LOG_TIMEZONE=Asia/Shanghai
```

注意：请使用普通 `kill <PID>` 停止服务，FastAPI 才有机会执行日志归档；`kill -9` 属于强制终止，不会触发上传。

停止服务：

```bash
ps -ef | grep uvicorn
kill <PID>
```

## 五、网页测试

如果服务器开放了 `8100` 端口，在浏览器访问：

```text
http://服务器IP:8100/docs
```

如果服务器端口不对外开放，可在本地建立 SSH 隧道：

```bash
ssh -p 3388 -L 8000:127.0.0.1:8100 root@172.16.11.40
```

然后本地浏览器打开：

```text
http://127.0.0.1:8100/docs
```

## 六、FastAPI 入库测试

打开 Swagger 页面：

```text
http://127.0.0.1:8100/docs
```

### 1. 普通文档入库

接口：

```text
POST /ingestion/v1/documents/ingest
```

请求体示例：

```json
{
  "user_id": "user-demo-001",
  "kb_id": "kb-demo",
  "oss_key": "prod/doc-ingestion/Agentic_RAG_Agent_Report_PRD_CN.pdf",
  "file_name": "Agentic_RAG_Agent_Report_PRD_CN.pdf",
  "file_type": "pdf",
  "config": {
    "summary_enabled": true,
    "enable_ocr": true,
    "extract_tables": true
  }
}
```

成功返回示例：

```json
{
  "code": 200,
  "message": "文档已提交处理",
  "data": {
    "task_id": "task_xxx",
    "doc_id": "451e790a-5066-5545-b0a1-8c940dcc3e48",
    "doc_ids": [
      "451e790a-5066-5545-b0a1-8c940dcc3e48"
    ],
    "user_id": "user-demo-001",
    "status": "queued",
    "estimated_seconds": 12
  }
}
```

说明：

- 请求体中的 `user_id` 表示平台用户 ID。
- 返回体中的 `doc_id` 是文档 UUID，用于查询文档状态和删除文档。
- UUID 只根据文档内容哈希稳定生成；相同文件内容会得到相同 `doc_id`。
- 用户、知识库和临时文档标记存放在 `document_bindings` 中；`documents` 只保存共享文档内容。
  文档可见范围由 `user_id + kb_id` 决定，`session_id` 只记录首次上传会话，不参与去重或读取授权。
- 服务会先从 OSS 下载文件到临时目录，计算 UUID 后提交后台任务；后台任务复用该临时文件，任务结束后自动清理。

### 2. 临时文档入库

接口：

```text
POST /ingestion/v1/documents/temp-ingest
```

请求体示例：

```json
{
  "user_id": "user-demo-001",
  "kb_id": "kb-demo",
  "session_id": "sess-demo-001",
  "oss_key": "prod/doc-ingestion/Agentic_RAG_Agent_Report_PRD_CN.pdf",
  "file_name": "Agentic_RAG_Agent_Report_PRD_CN.pdf",
  "file_type": "pdf",
  "config": {
    "summary_enabled": true,
    "enable_ocr": true,
    "extract_tables": true
  }
}
```

首次出现的临时文档会进入高优先级队列，并在任务中记录 `session_id`。同一用户和知识库
重复上传相同临时文档时，服务不报重复错误、不新增 binding、不重新解析，直接返回已有
`doc_id`。临时文档可以跨 session 读取；调用方负责根据自己的会话记录决定何时显式删除。

### 3. 查询任务状态

复制返回的 `task_id`，调用：

```text
GET /ingestion/v1/tasks/{task_id}
```

任务完成时：

```json
{
  "status": "completed",
  "progress": 100
}
```


### 4. 查询文档状态

使用入库返回体中的 `data.doc_id`：

调用：

```text
GET /ingestion/v1/documents/{doc_id}/status?user_id={user_id}&kb_id={kb_id}
```
示例参数：

```text
doc_id=451e790a-5066-5545-b0a1-8c940dcc3e48
user_id=user-demo-001
kb_id=kb-demo
```

任务完成时：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "doc_id": "451e790a-5066-5545-b0a1-8c940dcc3e48",
    "user_id": "user-demo-001",
    "task_id": "task_b26e46fcefd64b0f",
    "status": "completed",
    "progress": 100,
    "current_step": "ingestion completed (c4a32e97-2733-5dd9-8f1e-49deb2175251)",
    "total_pages": 30,
    "processed_pages": 30,
    "tree_node_count": 30,
    "error_message": null,
    "started_at": "2026-05-08T03:38:09Z",
    "completed_at": "2026-05-08T03:39:21.069870Z"
  }
}
```

### 5. 删除会话临时文档

调用：

```text
DELETE /ingestion/v1/sessions/{session_id}/temp-documents
```

示例参数：

```text
session_id=sess-demo-001
kb_id=kb-demo
```

该接口是由调用方显式触发的清理操作，不表示文档读取按 session 隔离。调用方应依据自身
数据库中的完整会话引用确认可以删除后再调用。

### 6. 删除文档

调用：

```text
DELETE /ingestion/v1/documents/{doc_id}
```

示例参数：

```text
doc_id=451e790a-5066-5545-b0a1-8c940dcc3e48
user_id=user-demo-001
kb_id=kb-demo
```

## 七、命令行完整测试

### 1. 测试 OSS 下载配置

在正式调用接口前，可以先验证服务器是否能通过当前 `.env` 下载 OSS 文件：

```bash
cd /path/to/rag-platform/src/repo-doc-ingestion
source .venv/bin/activate
python - <<'PY'
from pathlib import Path
from dotenv import load_dotenv
from app.ingestion.oss_client import download_oss_key

load_dotenv('.env', override=True)
target = Path('/tmp/oss-ingestion-test.pdf')
path = download_oss_key('prod/doc-ingestion/Agentic_RAG_Agent_Report_PRD_CN.pdf', target)
print(path, path.exists(), path.stat().st_size)
PY
```

如果这里报错，优先检查：

- `OSS_BUCKET` 是否正确
- `OSS_ENDPOINT` 是否正确
- `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` 是否正确

### 2. 普通文档入库

```bash
curl -X POST "http://127.0.0.1:8100/ingestion/v1/documents/ingest" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-demo-001",
    "kb_id": "kb-demo",
    "oss_key": "prod/doc-ingestion/Agentic_RAG_Agent_Report_PRD_CN.pdf",
    "file_name": "Agentic_RAG_Agent_Report_PRD_CN.pdf",
    "file_type": "pdf",
    "config": {
      "summary_enabled": true,
      "enable_ocr": true,
      "extract_tables": true
    }
  }'
```

### 3. 查询任务状态

```bash
curl "http://127.0.0.1:8100/ingestion/v1/tasks/task_xxx"
```

### 4. 查询文档状态

```bash
curl "http://127.0.0.1:8100/ingestion/v1/documents/451e790a-5066-5545-b0a1-8c940dcc3e48/status?user_id=user-demo-001&kb_id=kb-demo"
```

### 5. 删除文档

```bash
curl -X DELETE "http://127.0.0.1:8100/ingestion/v1/documents/451e790a-5066-5545-b0a1-8c940dcc3e48?user_id=user-demo-001&kb_id=kb-demo"
```

## 八、查看数据库内容

进入项目并激活环境：

```bash
cd /path/to/rag-platform/src/repo-doc-ingestion
source .venv/bin/activate
```

### 1. 查看文档列表

```bash
python - <<'PY'
import os
import psycopg
from dotenv import load_dotenv

load_dotenv('.env', override=True)
conn = psycopg.connect(os.getenv('POSTGRES_DSN'))
cur = conn.cursor()

cur.execute('''
select
  b.user_id,
  b.kb_id,
  d.doc_id,
  d.doc_name,
  d.doc_type,
  d.page_count,
  d.node_count,
  b.is_temporary,
  b.session_id,
  b.updated_at
from document_bindings b
join documents d on d.doc_id = b.doc_id
order by b.updated_at desc
limit 50
''')

for row in cur.fetchall():
    print(row)

cur.close()
conn.close()
PY
```

### 2. 查看某个文档的节点

```bash
python - <<'PY'
import os
import psycopg
from dotenv import load_dotenv

DOC_ID = '替换成入库返回的 doc_id'

load_dotenv('.env', override=True)
conn = psycopg.connect(os.getenv('POSTGRES_DSN'))
cur = conn.cursor()

cur.execute('''
select node_id, parent_node_id, level, title, summary, start_index, end_index, child_count
from doc_nodes
where doc_id = %s
order by id
''', (DOC_ID,))

for row in cur.fetchall():
    print('-' * 80)
    print('node_id:', row[0])
    print('parent:', row[1])
    print('level:', row[2])
    print('title:', row[3])
    print('summary:', row[4])
    print('start/end:', row[5], row[6])
    print('child_count:', row[7])

cur.close()
conn.close()
PY
```

### 3. 查看某个文档的 page 内容

```bash
python - <<'PY'
import os
import psycopg
from dotenv import load_dotenv

DOC_ID = '替换成入库返回的 doc_id'

load_dotenv('.env', override=True)
conn = psycopg.connect(os.getenv('POSTGRES_DSN'))
cur = conn.cursor()

cur.execute('''
select page_number, content
from doc_pages
where doc_id = %s
order by page_number
''', (DOC_ID,))

for page_number, content in cur.fetchall():
    print('=' * 80)
    print('PAGE:', page_number)
    print((content or '')[:2000])

cur.close()
conn.close()
PY
```

### 4. 查看任务表

```bash
python - <<'PY'
import os
import psycopg
from dotenv import load_dotenv

load_dotenv('.env', override=True)
conn = psycopg.connect(os.getenv('POSTGRES_DSN'))
cur = conn.cursor()

cur.execute('''
select doc_id, kb_id, status, progress, current_step, error_message, updated_at
from ingestion_tasks
order by updated_at desc
limit 20
''')

for row in cur.fetchall():
    print(row)

cur.close()
conn.close()
PY
```

## 九、运行测试

运行全部测试：

```bash
cd /path/to/rag-platform/src/repo-doc-ingestion
source .venv/bin/activate
python -m pytest tests -v -s -p no:cacheprovider
```

只测试解析：

```bash
python -m pytest tests/test_parser.py -v
```

测试数据库入库：

```bash
python -m pytest tests/test_ingestion_db.py -v
```

测试 API 提交流程：

```bash
python -m pytest tests/test_api_ingestion.py -v -s -p no:cacheprovider
```

说明：

- `test_parser.py` 不依赖数据库。
- `test_ingestion_db.py` 和 `test_api_ingestion.py` 需要 PostgreSQL 可连接。
- API 测试默认可使用测试 fixtures，不等同于 OSS 联调。
- 测试默认关闭 summary，避免调用 LLM 造成测试不稳定。

## 十、Docker Compose 生产部署

生产环境推荐用 Docker Compose 同时管理入库服务和 PostgreSQL。应用容器通过 Docker 内部网络访问 `postgres:5432`，PostgreSQL 默认不暴露宿主机端口。入库 API 当前没有内置服务鉴权，Compose 默认将它绑定到 `127.0.0.1`；跨主机调用时必须由鉴权网关、服务网格或严格的内网防火墙提供访问控制，不能直接暴露公网端口。

### 1. 在服务器配置 Git 仓库 SSH Key

推荐在服务器上创建一个只读 SSH Deploy Key，然后用 SSH clone。这样后续更新只需要 `git pull --ff-only`，不用在服务器上保存账号密码。

在服务器上生成专用于这个仓库的 key：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_repo_doc_ingestion -C "repo-doc-ingestion-deploy"
```

一路回车即可。然后查看公钥：

```bash
cat ~/.ssh/id_ed25519_repo_doc_ingestion.pub
```

把输出的整行公钥复制到 Git 仓库 仓库的 SSH 公钥/Deploy Key 配置中，建议只给只读权限。私钥 `~/.ssh/id_ed25519_repo_doc_ingestion` 只留在服务器，不要复制到其他地方。

如果服务器上已有多个 SSH Key，建议增加 SSH config：

```bash
cat >> ~/.ssh/config <<'EOF'
Host example-repo-doc-ingestion
  HostName git.example.com
  User git
  IdentityFile ~/.ssh/id_ed25519_repo_doc_ingestion
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
```

测试 SSH 是否可用：

```bash
ssh -T example-repo-doc-ingestion
```

### 2. 从 Git 仓库 拉取代码

服务代码由统一的 `rag-platform` 仓库交付；数据库数据目录单独配置到数据盘，不依赖代码目录位置。

```bash
cd /path/to/rag-platform
git checkout dev
cd src/repo-doc-ingestion
```

生产环境应使用主管指定的 `rag-platform` 发布分支或标签，不再拉取、维护或发布原独立入库仓库。

### 3. 确认数据盘挂载点

不要凭目录名判断是不是数据盘，必须看挂载信息。先在服务器执行：

```bash
lsblk -f
df -hT
findmnt -rno TARGET,SOURCE,FSTYPE,SIZE,AVAIL / /var /mnt /opt /home 2>/dev/null || true
docker info --format 'DockerRootDir={{.DockerRootDir}}' 2>/dev/null || true
```

判断原则：

- `df -hT /var` 如果显示的挂载源和 `/` 一样，说明 `/var` 还在系统盘上，不要把 PostgreSQL 数据放到 `/var/lib/pageindex/postgres`。
- 如果某个挂载点容量明显是数据盘，例如 `/mnt`、`/mnt/data`、`/var/lib/docker` 或你自行挂载的目录，就把 `PGDATA_HOST_PATH` 设置到这个挂载点下面。
- 如果数据盘还没有挂载，需要先在系统层完成分区、格式化、挂载和 `/etc/fstab` 持久化，再启动 Compose。

当前服务器上，`/dev/nvme1n1` 挂载在 `/mnt/data`，这是数据盘；`/var` 和 Docker 根目录 `/var/lib/docker` 都在系统盘 `/`。因此本服务的 PostgreSQL 数据目录使用：

```text
/mnt/data/pageindex/postgres
```

### 4. 准备环境变量

```bash
cd /path/to/rag-platform/src/repo-doc-ingestion
test -f ../../env/ingestion.env
vim ../../env/ingestion.env
```

生产环境至少改这些值：

```env
APP_HOST=127.0.0.1
APP_PORT=8100
POSTGRES_USER=postgres
POSTGRES_PASSWORD=换成强密码
POSTGRES_DB=pageindex
PGDATA_HOST_PATH=/mnt/data/pageindex/postgres

ARK_API_KEY=真实的 Ark API Key
INGEST_PARSER_BACKEND=mineru
MINERU_API_URL=http://mineru-api:8135
OSS_BUCKET=真实 bucket
OSS_ENDPOINT=https://oss-cn-shenzhen-internal.aliyuncs.com
OSS_REGION=cn-shenzhen
OSS_ACCESS_KEY_ID=真实 AccessKeyId
OSS_ACCESS_KEY_SECRET=真实 AccessKeySecret
```

说明：

- `.env` 留在服务器本地，不提交 Git。数据库密码、OSS Key、LLM API Key 都建议放 `.env`，不要写在启动命令里，避免 shell history 泄露。
- `docker-compose.yml` 会给应用容器自动设置 `POSTGRES_DSN=postgresql://...@postgres:5432/pageindex`，所以 Compose 部署时不用把 DSN 写成 `127.0.0.1`。
- `PGDATA_HOST_PATH` 必须明确填写，Compose 不再提供默认值，避免误把数据库数据写到系统盘。
- 如果 ECS 和 OSS Bucket 在同一区域，优先使用 OSS 内网 Endpoint，例如 `https://oss-cn-shenzhen-internal.aliyuncs.com`。
- MinerU 不在本仓库的 Compose 中部署；`MINERU_API_URL` 必须是应用容器实际可解析并访问的地址。`MINERU_FALLBACK_TO_NATIVE=true` 只提供可用性降级，不代表两种解析质量完全相同。
- 默认不要在安全组开放 `APP_PORT`。若必须绑定 `0.0.0.0`，应先完成鉴权网关和来源网段限制；PostgreSQL 不对公网开放。

### 5. 一条命令启动

第一次会自动 build 镜像并初始化 PostgreSQL，后续重复执行同一条命令即可滚动到最新镜像配置：

```bash
bash scripts/deploy.sh
```

等价命令：

```bash
docker compose up -d --build
```

查看状态和日志：

```bash
docker compose ps
docker compose logs -f app
docker compose logs -f postgres
```

健康检查：

```bash
curl http://127.0.0.1:8100/healthz
curl http://127.0.0.1:8100/docs
```

经鉴权网关访问（示例域名）：

```text
https://internal-api.example.com/ingestion/docs
```

### 6. 后续更新

每次从 Git 仓库 更新后执行：

```bash
git pull --ff-only
bash scripts/deploy.sh
```

如果希望从拉代码到重启合成一条命令：

```bash
git pull --ff-only && bash scripts/deploy.sh
```

### 7. 数据库维护

进入 PostgreSQL：

```bash
docker compose exec postgres psql -U postgres -d pageindex
```

备份数据库到服务器当前目录：

```bash
docker compose exec -T postgres pg_dump -U postgres -d pageindex > pageindex.sql
```

如果后续改用阿里云 RDS 或已有 PostgreSQL，可以删除/停用 Compose 里的 `postgres` 服务，并把 `app.environment.POSTGRES_DSN` 改成真实数据库连接串；这时数据库的持久化和备份由外部 PostgreSQL/RDS 负责。

## 十一、常见问题

### 1. OSS 下载报错：Unable to connect to proxy

如果看到类似：

```text
ProxyError: Failed to establish a new connection: 127.0.0.1:7890
```

说明 FastAPI 进程继承了不可用代理。停止服务后重新启动：

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
uvicorn app.api.app:app --host 0.0.0.0 --port 8100
```

### 2. OSS 下载报错：NoSuchKey

说明请求体中的 `oss_key` 不存在，或多写/少写了路径前缀。示例：

```text
prod/doc-ingestion/Agentic_RAG_Agent_Report_PRD_CN.pdf
```

不要在 `oss_key` 前面加 Bucket 名，也不要写成完整 HTTPS URL。

### 3. PDF 报错：PyCryptodome is required for AES algorithm

说明 PDF 内部包含 AES 加密结构，`PyPDF2` 需要 `pycryptodome`。

安装：

```bash
pip install pycryptodome -i https://mirrors.aliyun.com/pypi/simple/
```

建议确保 `requirements.txt` 中包含：

```text
pycryptodome
```

### 4. DOCX/PPTX 中文错位或乱码

确认安装了：

- LibreOffice
- Noto CJK 字体
- 文泉驿字体
