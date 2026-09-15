# repo-agent-qa

Local multi-document QA assistant built on PageIndex, with PostgreSQL as the persistent index store.
基于 PageIndex 的本地多文档问答助手，使用 PostgreSQL 作为持久化索引存储。

## Clone

Use the SSH repository URL below:
使用下面的 SSH 地址克隆仓库：

```bash
git clone git@example.com:org/pageindex-rag.git
cd pageindex-rag
```

## Project layout

项目结构如下：

```text
repo-agent-qa/
├── app/                    # business code, prompts, local data directory, PageIndex integration
├── tests/                  # lightweight regression tests
├── Dockerfile
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
└── CHANGELOG.md
```

## What is inside `app/`

`app/` 目录的主要内容：

- `document_assistant_agent.py`: main CLI entrypoint
- `document_assistant_config.yaml`: default runtime config
- `pageindex/`: indexing and retrieval primitives
- `core/`, `qa/`, `models/`, `prompts/`, `utils/`, `cli/`: assistant runtime modules
- `data/`: local document directory for indexing; sample files are not committed

## Local setup

本地运行前，按下面步骤准备环境：

1. Use Python 3.11 and install dependencies:
   使用 Python 3.11，并安装依赖：

```bash
pip install -r requirements.txt
```

2. Create `.env` from `.env.example` and fill in real values.
   从 `.env.example` 复制生成 `.env`，并填入真实配置。

3. Make sure PostgreSQL is reachable from `POSTGRES_DSN`.
   确保 `POSTGRES_DSN` 指向的 PostgreSQL 可以正常连接。

4. Put your own `.pdf`, `.docx`, or `.md` files under `app/data/`.
   将你自己的 `.pdf`、`.docx` 或 `.md` 文档放到 `app/data/` 目录下。

The repository does not include the large local sample documents used during development. If `app/data/` is empty after clone, that is expected.
仓库不会提交开发时使用的大体积本地样本文档，所以 clone 之后如果 `app/data/` 是空的，这是正常现象。

## Required environment variables

下面这些环境变量是最关键的：

```env
ARK_API_KEY=your_ark_api_key
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_MODEL=your_model_id
POSTGRES_DSN=postgresql://user:password@host:5432/pageindex
```

The runtime also bridges `ARK_*` values into OpenAI-compatible variables automatically.
运行时会自动把 `ARK_*` 变量桥接成 OpenAI 兼容变量。

By default, PostgreSQL retrieval is scoped by `user_id`. To search across all indexed documents, set `DA_USER_ID=*` or `DA_DISABLE_USER_SCOPE=1`.
默认情况下，PostgreSQL 检索会按 `user_id` 隔离。若要检索全库文档，可设置 `DA_USER_ID=*` 或 `DA_DISABLE_USER_SCOPE=1`。

## Run the assistant

下面是常用运行方式：

Index missing documents from `app/data/` and ask a question:
先索引 `app/data/` 中缺失的文档，再发起提问：

```bash
python app/document_assistant_agent.py --index-missing --question "光明区旅游手册讲了什么？"
```

Ask against already indexed documents:
直接基于已经入库的索引提问：

```bash
python app/document_assistant_agent.py --question "比较旅游手册和报告里对光明区的描述"
```

Ask against only selected documents:
只针对指定文档范围进行问答：

```bash
python app/document_assistant_agent.py --question "光明区讲了什么？" --restrict-docs "光明区光明街道旅游手册"
```

`--restrict-docs` supports `doc_id`, exact file name, or the displayed document name in the catalog. After this flag is provided, retrieval and answering stay within the selected document scope only.
`--restrict-docs` 支持传入 `doc_id`、完整文件名，或资料库中展示的文档名称。传入该参数后，检索和回答都会限制在指定文档范围内。

Delete indexed documents by file name:
按文件名删除已经入库的文档：

```bash
python app/document_assistant_agent.py --delete-file "光明区光明街道旅游手册.docx" --delete-only
```

Show all CLI options:
查看全部命令行参数：

```bash
python app/document_assistant_agent.py --help
```

## Run tests

运行测试：

```bash
pytest
```

## Minimal smoke check after clone

同事 clone 仓库后，建议至少做一次最小冒烟检查：

Confirm the CLI can start:
先确认 CLI 入口能正常启动：

```bash
python app/document_assistant_agent.py --help
```

Then run a real indexing / QA flow after `.env` is configured, PostgreSQL is reachable, and documents have been placed in `app/data/`:
确认 `.env` 已配置、PostgreSQL 可连、且 `app/data/` 中已有文档后，再执行一次真实索引和问答流程：

```bash
python app/document_assistant_agent.py --index-missing --question "你的问题"
```

## Docker

如果需要容器方式运行，可以使用下面的命令：

Build:
构建镜像：

```bash
docker build -t repo-agent-qa .
```

Run with an env file:
带 `.env` 文件启动：

```bash
docker run --rm --env-file .env repo-agent-qa \
  python app/document_assistant_agent.py --help
```

For real QA runs, mount your own documents into `app/data/` and point `POSTGRES_DSN` to a reachable PostgreSQL instance.
如果要在 Docker 中执行真实问答，请把自己的文档挂载到 `app/data/`，并确保 `POSTGRES_DSN` 指向可访问的 PostgreSQL 实例。
