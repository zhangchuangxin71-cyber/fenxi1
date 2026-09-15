# 全部项目目录说明

本目录是一个项目归档与面试材料集合，集中保存了 6 个核心工程项目和若干项目讲解文档。整体方向围绕 AI 视频处理、多模态检索、知识库问答、报告生成和多服务 RAG 平台部署。

目录总大小约 203M。当前没有发现超过 20M 的单个大文件，主要体量来自 `media-editor-master-*` 和 `repo-doc-rag-agent-aggregation-*` 两个目录。

## 公开脱敏说明

本归档已按公开仓库口径做脱敏处理：

- 本地个人路径已替换为通用示例路径。
- 私有 Git 地址、公司业务域名、个人 OSS Bucket 名称已替换为 `example.com`、`git@example.com`、`example-bucket` 等占位值。
- 生成测试产物中的临时图片签名 URL 已删除或替换为示例 URL。
- 第三方文档中的邮箱已替换为 `contact@example.com` / `noreply@example.com`。
- `.env.example` 只保留占位变量，真实 API Key、数据库密码、OSS Access Key 等需要在本地或服务器环境中自行配置，不应提交到仓库。

## 一、目录结构

| 目录/文件 | 类型 | 说明 |
| --- | --- | --- |
| `media-editor-master-a89f9578b6263d471456be1a1b28ab74324044a6/` | 工程项目 | 文案自动成片 Flow B FastAPI 服务。 |
| `multimodal-retrieval-master-dc02c04b5e2ccb439b42f23299618451a901b0a9/` | 工程项目 | 中文多模态向量化与检索项目，包含 embedding API、图片检索、视频检索、Milvus/CLIP/BGE 相关模块。 |
| `pageindex-rag-master-fddbdbb4cd990f0340cf3b08767fe3d189cf828b/` | 工程项目 | 基于 PageIndex + PostgreSQL 的本地多文档问答助手。 |
| `rag-report-agent-master-d60e2231b31cb4052e4656017e0a62cba8269174/` | 工程项目 | 面向知识库问答和报告生成的 FastAPI + LangGraph RAG Agent。 |
| `repo-doc-rag-agent-aggregation-dev-a0639c75ada7f3d89d4148a02c1f6b5c3f03176e/` | 工程项目 | RAG 平台聚合部署仓库，统一编排多个业务服务。 |
| `video-clip-main-130ec4612c08d7fe222c0bcfbf545a93c66d2457/` | 工程项目 | 视频智能切片 FastAPI 服务，支持 OSS 预览、分镜、切片和对象列表返回。 |
| `项目经历优化版.md` | 面试/简历材料 | 项目经历优化稿。 |
| `项目功能技术栈与学习路线.md` | 学习材料 | 项目功能、技术栈和学习路线梳理。 |
| `全部项目面试题索引.md` | 面试材料 | 各项目面试题索引。 |
| `文案自动成片项目面试题.md` | 面试材料 | 文案自动成片项目问答准备。 |
| `多模态检索项目面试题.md` | 面试材料 | 多模态检索项目问答准备。 |
| `视频智能切片项目面试题.md` | 面试材料 | 视频切片项目问答准备。 |
| `大模型与知识库项目面试题.md` | 面试材料 | 大模型与知识库项目问答准备。 |
| `大模型与知识库项目实现详解.md` | 技术说明 | 大模型与知识库方向的实现细节说明。 |

## 二、核心项目说明

### 1. 文案自动成片

目录：

```text
media-editor-master-a89f9578b6263d471456be1a1b28ab74324044a6/
```

这是一个文案自动成片服务，主流程是：

```text
口播文案 -> LLM 语义分句 -> 外部 TTS wav URL -> 素材绑定 -> FFmpeg 裁切/拼接 -> 硬/软字幕 -> 输出 MP4
```

主要技术点：

- FastAPI 后端服务。
- FFmpeg 视频裁切、拼接、字幕合成。
- LLM 语义分句，默认可使用豆包/火山方舟相关配置。
- OSS 素材拉取与成片上传。
- Docker Compose 部署。
- `workspaces/` 作为运行时工作区。

常用入口：

```bash
cd media-editor-master-a89f9578b6263d471456be1a1b28ab74324044a6
cp .env.example .env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m videoaudiotext.api
```

API 文档默认在：

```text
http://127.0.0.1:8787/docs
```

### 2. 多模态检索 / Chinese-CLIP

目录：

```text
multimodal-retrieval-master-dc02c04b5e2ccb439b42f23299618451a901b0a9/
```

这是面向中文多模态检索的工程化项目，核心是向量化 HTTP API 和图片/视频检索能力。

主要模块：

- `embed_core/`：CLIP、BGE、视频分段、Milvus 等共享编码能力。
- `embedding_service/`：FastAPI 向量化服务。
- `picture/`：图片检索。
- `video_retrieval/`：视频检索。
- `models/`：模型权重目录，通常首次启动下载或本地准备。
- `scripts/`：部署、验证、模型下载等脚本。

主要技术点：

- Chinese-CLIP 视觉向量。
- BGE 文本向量。
- Milvus 向量库。
- 图片/视频 embedding jobs。
- OSS 图片/视频输入。
- Docker 部署与本地 Python 运行。

常用入口：

```bash
cd multimodal-retrieval-master-dc02c04b5e2ccb439b42f23299618451a901b0a9
cp .env.example .env
python -m embedding_service.milvus_init
python start_embedding_service.py
```

服务启动后：

```text
健康检查：http://127.0.0.1:8030/health
Swagger：http://127.0.0.1:8030/docs
```

### 3. PageIndex 多文档问答

目录：

```text
pageindex-rag-master-fddbdbb4cd990f0340cf3b08767fe3d189cf828b/
```

这是一个基于 PageIndex + PostgreSQL 的本地多文档问答助手。它支持把本地 `.pdf`、`.docx`、`.md` 文档放入 `app/data/`，索引后进行问答。

主要技术点：

- Python CLI 入口。
- PageIndex 文档解析与索引。
- PostgreSQL 持久化索引。
- 火山方舟 `ARK_*` 模型配置。
- 支持按文档范围限制检索。

常用入口：

```bash
cd pageindex-rag-master-fddbdbb4cd990f0340cf3b08767fe3d189cf828b
pip install -r requirements.txt
cp .env.example .env
python app/document_assistant_agent.py --index-missing --question "文档讲了什么？"
```

### 4. RAG 报告生成 Agent

目录：

```text
rag-report-agent-master-d60e2231b31cb4052e4656017e0a62cba8269174/
```

这是一个私有知识库问答与报告生成服务，技术路线是 FastAPI + LangGraph。它复用 PageIndex/PostgreSQL 中已经入库的文档数据，通过豆包 Ark 模型完成意图识别、问题改写、文档路由、RAG 问答、大纲生成、报告撰写和报告编辑。

主要能力：

- `/agent/v1/chat/completions` 统一聊天入口。
- SSE 流式输出。
- 知识库问答。
- 多问题 query rewrite。
- 多意图识别。
- 报告大纲生成、报告正文生成、报告编辑。
- 引用返回。
- 可选报告自动配图。

常用入口：

```bash
cd rag-report-agent-master-d60e2231b31cb4052e4656017e0a62cba8269174
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

关键配置：

- `ARK_API_KEY`
- `ARK_BASE_URL`
- `MODEL_FAST`
- `MODEL_SMART`
- `PG_DSN`
- `RAG_TOP_K`

### 5. RAG 平台聚合部署

目录：

```text
repo-doc-rag-agent-aggregation-dev-a0639c75ada7f3d89d4148a02c1f6b5c3f03176e/
```

该项目是一个统一部署仓库，用于把多个 RAG 业务服务组合成一套平台。内部实际项目位于同名子目录：

```text
repo-doc-rag-agent-aggregation-dev-a0639c75ada7f3d89d4148a02c1f6b5c3f03176e/
└── repo-doc-rag-agent-aggregation-dev-a0639c75ada7f3d89d4148a02c1f6b5c3f03176e/
```

包含的服务：

| 服务 | 端口 | 说明 |
| --- | --- | --- |
| `mineru-api` | 8135 | MinerU CPU 文档解析 |
| `repo-doc-ingestion` | 8100 | 文档入库 |
| `rag-report-agent` | 8115 | 报告生成 SSE |
| `rag-retrieval-service` | 8120 | 统一检索 |
| `rag-knowledge-chat` | 8130 | 知识库问答 |
| `wechat-article-runtime` | 8141 容器内 | 私有 LangGraph Agent Server |
| `wechat-article-agent` | 8140 | 微信公众号文章 API |

生产部署入口：

```bash
cd repo-doc-rag-agent-aggregation-dev-a0639c75ada7f3d89d4148a02c1f6b5c3f03176e/repo-doc-rag-agent-aggregation-dev-a0639c75ada7f3d89d4148a02c1f6b5c3f03176e
bash scripts/deploy.sh
```

注意：该项目 README 明确说明生产部署只使用顶层 `scripts/deploy.sh`，不要同时运行子项目部署脚本，避免端口、容器和网络冲突。

### 6. 视频智能切片 API

目录：

```text
video-clip-main-130ec4612c08d7fe222c0bcfbf545a93c66d2457/
```

这是纯 FastAPI 视频切片服务，支持 OSS 远程预览、手动/自动分镜、流式切片和 OSS 对象列表返回。

主要技术点：

- FastAPI 单进程服务。
- FFmpeg 远程读取 OSS 源片。
- stdout multipart 方式把切片写回 OSS。
- 任务状态保存在内存，不跨重启恢复。
- `frontend/reference-ui/` 是对接参考界面。
- Docker 镜像内置 FFmpeg。

常用入口：

```bash
cd video-clip-main-130ec4612c08d7fe222c0bcfbf545a93c66d2457
cp .env.example .env
./start.sh
./start.sh health
```

默认服务：

```text
API：http://127.0.0.1:8010
Swagger：http://127.0.0.1:8010/docs
```

## 三、面试材料说明

本目录下的 Markdown 文档不是代码项目，而是围绕上述项目整理的面试材料：

- `项目经历优化版.md`：适合用于简历项目经历描述。
- `项目功能技术栈与学习路线.md`：适合快速复盘每个项目的功能、技术栈和学习路径。
- `全部项目面试题索引.md`：所有面试题的总入口。
- `文案自动成片项目面试题.md`：对应 `media-editor`。
- `多模态检索项目面试题.md`：对应 `multimodal-retrieval`。
- `视频智能切片项目面试题.md`：对应 `video-clip`。
- `大模型与知识库项目面试题.md`：对应 PageIndex/RAG/报告生成方向。
- `大模型与知识库项目实现详解.md`：更偏技术实现讲解，可用于深挖问题准备。

建议面试准备顺序：

1. 先读 `项目经历优化版.md`，确定简历表达。
2. 再读 `项目功能技术栈与学习路线.md`，建立全局技术地图。
3. 按目标岗位选择具体项目面试题。
4. 最后回到对应项目 README 和源码，补足接口、数据流、异常处理和部署细节。

## 四、项目之间的关系

这些项目可以按业务方向分成三组：

### AI 视频处理

相关项目：

- `media-editor-master-*`
- `video-clip-main-*`

能力链路：

```text
视频素材/文案输入 -> 分句/分析 -> 素材绑定/分镜 -> FFmpeg 处理 -> 字幕/切片/成片输出 -> OSS 交付
```

### 多模态检索

相关项目：

- `multimodal-retrieval-master-*`

能力链路：

```text
图片/视频输入 -> CLIP/BGE 向量化 -> Milvus/本地索引 -> 检索 API -> 返回相似图片/视频片段
```

### 大模型知识库与报告生成

相关项目：

- `pageindex-rag-master-*`
- `rag-report-agent-master-*`
- `repo-doc-rag-agent-aggregation-dev-*`

能力链路：

```text
文档解析入库 -> PageIndex/PostgreSQL 存储 -> 检索服务 -> 知识问答/报告生成/微信文章 Agent
```

## 五、运行环境汇总

常见依赖：

- Python 3.11 优先。
- Docker + Docker Compose plugin。
- FFmpeg / ffprobe，视频项目必需。
- PostgreSQL，RAG 和 PageIndex 项目必需。
- Milvus，多模态向量写库场景必需。
- OSS 配置，视频素材、切片、成片和 embedding jobs 常用。
- 火山方舟/豆包 `ARK_*` 配置，LLM 能力常用。

常见环境变量：

```text
ARK_API_KEY
ARK_BASE_URL
ARK_MODEL
MODEL_FAST
MODEL_SMART
PG_DSN / POSTGRES_DSN
OSS_ENDPOINT
OSS_ACCESS_KEY_ID
OSS_ACCESS_KEY_SECRET
OSS_BUCKET
ALIYUN_OSS_ENDPOINT
ALIYUN_OSS_ACCESS_KEY_ID
ALIYUN_OSS_ACCESS_KEY_SECRET
ALIYUN_OSS_BUCKET
MILVUS_URI
```

真实 `.env` 文件不要提交仓库；只保留 `.env.example`。

## 六、维护建议

1. 每个子项目保留自己的 README，不要只依赖本说明文件。
2. 如果要对外展示，优先展示 `media-editor`、`multimodal-retrieval`、`rag-report-agent` 和 `video-clip`，它们主题清晰、技术链路完整。
3. `repo-doc-rag-agent-aggregation-*` 更适合作为“多服务生产部署与工程集成”案例讲。
4. 大文件、模型、数据库、运行日志、OSS 下载缓存不应提交到 Git。
5. 面试讲解时不要把所有项目平铺，应按“视频智能化”和“知识库/RAG”两条主线组织。

## 七、快速索引

| 目标 | 推荐查看 |
| --- | --- |
| 看所有项目总览 | 本文件 |
| 准备简历项目描述 | `项目经历优化版.md` |
| 准备整体技术路线 | `项目功能技术栈与学习路线.md` |
| 准备面试问答 | `全部项目面试题索引.md` |
| 跑文案成片服务 | `media-editor-master-*/README.md` |
| 跑多模态检索服务 | `multimodal-retrieval-master-*/QUICKSTART.md` |
| 跑知识库问答 | `pageindex-rag-master-*/README.md` |
| 跑报告生成 Agent | `rag-report-agent-master-*/README.md` |
| 部署 RAG 平台 | `repo-doc-rag-agent-aggregation-dev-*/.../README.md` |
| 跑视频切片 API | `video-clip-main-*/README.md` |
