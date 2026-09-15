# PageIndex 入库数据结构说明

本文档说明 `repo-doc-ingestion` 将文档写入 PostgreSQL 后的核心数据结构，以及这些字段在 `rag-retrieval-service` 检索服务中的使用方式。

当前核心表为：

```text
documents
document_bindings
doc_pages
doc_nodes
```

其中：

- `documents` 保存文档本体元信息。
- `document_bindings` 保存文档属于哪个用户和知识库。
- `doc_pages` 保存按页组织的正文。
- `doc_nodes` 保存 PageIndex 风格的结构节点树。

入库服务还会维护 `ingestion_tasks` 和 `ingestion_task_events`，它们用于入库任务状态管理，不属于 RAG 检索的核心内容表。

## 1. 数据流总览

典型入库链路：

```text
OSS / 本地临时文件
-> IngestionService
-> AgentAdapter
-> parse_document_to_structure()
-> DocumentParser / md_to_tree()
-> PageIndexClient.save_doc()
-> PostgresStore.save_doc()
-> documents / document_bindings / doc_pages / doc_nodes
```

文档解析后的中间结构大致是：

```json
{
  "id": "...",
  "type": "md",
  "path": "/tmp/example.md",
  "doc_name": "example.md",
  "doc_description": "文档整体描述",
  "page_count": 1,
  "line_count": 80,
  "structure": [
    {
      "title": "章节标题",
      "node_id": "0000",
      "start_index": 1,
      "end_index": 1,
      "text": "章节正文",
      "summary": "章节摘要",
      "nodes": []
    }
  ],
  "pages": [
    {"page": 1, "content": "完整页面正文"}
  ]
}
```

`PostgresStore.save_doc()` 会把它拆成四张表：

- 文档级字段写入 `documents`。
- 用户/知识库归属写入 `document_bindings`。
- `pages` 写入 `doc_pages`。
- `structure` 递归拍平后写入 `doc_nodes`。

## 2. 支持的文件类型

底层 parser 直接支持：

```text
.pdf
.doc
.docx
.md
.markdown
.xlsx
.txt
.pptx
```

API 请求模型中也允许 `html`，但 HTML 会先被转换为临时 Markdown，再走 Markdown 入库链路。
`.doc` 是 legacy Word 输入兼容类型，内部仍按既有 `docx` 类型写入 `documents.doc_type`。

当前不直接支持把 `.jpg` / `.png` 作为独立文件入库。扫描件支持主要针对扫描版 PDF：服务会将 PDF 页面渲染为图片，再调用 Ark Vision 模型做 OCR 和版面/表格解析。

## 3. `documents` 表

### 3.1 作用

`documents` 是文档本体表。它描述一篇文档是什么、从哪里来、解析后有多少页/节点，以及完整原始解析 JSON。

同一份文件内容会生成稳定的 `doc_id`。如果同一文档被多个用户或知识库复用，文档本体仍可共享，归属关系由 `document_bindings` 表表达。

### 3.2 字段说明

| 字段 | 类型 | 来源 | LLM 生成 | 含义 | 检索服务使用 |
| --- | --- | --- | --- | --- | --- |
| `doc_id` | uuid | 文件内容 hash 生成稳定 ID | 否 | 文档唯一 ID | 作为 `document_id` 返回；关联所有表 |
| `doc_name` | varchar(512) | 文件名或解析 payload | 否 | 展示名称 | 参与 metadata 打分；返回给上游展示引用 |
| `doc_type` | varchar(16) | 文件扩展名/解析类型 | 否 | `pdf/md/docx/txt/xlsx/pptx` 等 | 返回到 `document_meta` |
| `doc_description` | text | 文档前若干节点摘要再经 LLM 生成；失败时可用标题 fallback | 是/可 fallback | 文档整体描述，用于文档路由 | 参与 metadata 打分；`documents/meta` 返回给 doc router |
| `file_path` | text | 入库临时文件路径或源路径 | 否 | 原文件路径记录 | 当前检索服务不用 |
| `file_oss_key` | varchar(512) | OSS 输入 key | 否 | 原文件在 OSS 的对象 key | 当前检索服务不用 |
| `file_size` | bigint | 文件 fingerprint | 否 | 文件大小 | 当前检索服务不用 |
| `file_mtime` | double precision | 文件 fingerprint | 否 | 文件修改时间 | 当前检索服务不用 |
| `file_head_md5` | varchar(64) | 文件 fingerprint | 否 | 文件头部 hash | 当前检索服务不用 |
| `file_md5` | varchar(64) | 文件 fingerprint | 否 | 文件完整 MD5 | 当前检索服务不用 |
| `content_hash` | varchar(128) | fingerprint 或内容 hash | 否 | 去重/缓存辅助 | 当前检索服务不用 |
| `paragraph_count` | integer | parser 统计 | 否 | 段落数，格式相关 | 当前检索服务不用 |
| `page_count` | integer | parser 输出 pages 数 | 否 | 页数/合成页数 | 返回到 `document_meta` 和 chunk meta |
| `line_count` | integer | Markdown/TXT parser 统计 | 否 | 文本行数 | 当前检索服务不用 |
| `node_count` | integer | `doc_nodes` 行数 | 否 | 节点数 | 返回到 `document_meta` 和 chunk meta |
| `status` | varchar(32) | 入库状态 | 否 | 默认 `ready` | 检索 SQL 只查 `ready` 文档 |
| `raw` | jsonb | 完整解析 payload | 混合 | 保存完整结构，便于回放/调试 | 当前检索服务不用 |
| `created_at` | timestamptz | 数据库默认值 | 否 | 创建时间 | `documents/meta` 返回 |
| `updated_at` | timestamptz | upsert 更新时间 | 否 | 更新时间 | `documents/meta` 返回 |

### 3.3 字段来源示例

对于文件 `周报.md`：

```text
doc_name = 周报.md
doc_type = md
page_count = Markdown pages 数，默认每 120 行切一页，或由 [[PAGE N]] 显式标记决定
node_count = Markdown 标题解析后生成的节点数
raw = parser 产生的完整 PageIndex-style JSON
```

`doc_description` 的生成方式：

1. 先对 leaf node 生成或填充 `summary`。
2. 收集前几个 node summary。
3. 调用 Ark 生成文档级描述。
4. 如果生成失败，上层可能使用文档名/标题 fallback。

## 4. `document_bindings` 表

### 4.1 作用

`document_bindings` 表记录文档属于哪个用户、哪个知识库。前端及业务权限层可以使用这张表
维护归属关系；当前 `rag-retrieval-service` 已改为只按调用方给出的 doc_id 读取文档。

同一个 `doc_id` 可以绑定到多个 `user_id + kb_id`，因为 `documents` 是共享文档本体，`document_bindings` 是作用域关系。

### 4.2 字段说明

| 字段 | 类型 | 来源 | LLM 生成 | 含义 | 检索服务使用 |
| --- | --- | --- | --- | --- | --- |
| `doc_id` | uuid | `documents.doc_id` | 否 | 绑定的文档 | join 文档本体 |
| `user_id` | varchar(64) | 入库请求 | 否 | 用户 ID | 不参与检索过滤；meta 可返回代表 binding 的值 |
| `kb_id` | varchar(128) | 入库请求 | 否 | 知识库 ID | 不参与检索过滤；meta 可返回代表 binding 的值 |
| `session_id` | varchar(64) | 入库请求，可空 | 否 | 首次上传临时文档的会话记录 | 兼容透传，不参与读取授权 |
| `is_temporary` | boolean | 入库请求 | 否 | 是否临时文档 | 返回到 `document_meta` |
| `created_at` | timestamptz | 数据库默认值 | 否 | 绑定创建时间 | `documents/meta` 返回为 `bound_at` |
| `updated_at` | timestamptz | upsert 更新时间 | 否 | 绑定更新时间 | 多条 binding 时用于确定性选择代表记录 |

主键：

```text
(doc_id, user_id, kb_id)
```

索引：

```text
idx_document_bindings_user_kb(user_id, kb_id)
idx_document_bindings_session(session_id) where session_id is not null
```

### 4.3 当前检索读取边界

`rag-retrieval-service` 查询 documents、nodes、pages 时只使用调用方显式传入的 doc_id，
不使用 binding 中的 `user_id`、`kb_id` 或 `session_id` 做权限过滤。若一个 doc_id 对应多条
binding，检索服务只选择一条 binding 作为 `is_temporary` 等兼容元信息，不重复文档内容。

因此多用户权限边界已经上移到前端/业务调用方：调用方必须保证传入的 doc_id 已通过自己的
权限检查。检索服务仍应部署在可信内网，不能直接向不可信客户端开放。

## 5. `doc_pages` 表

### 5.1 作用

`doc_pages` 保存按页组织的完整正文。检索服务会把每一页作为一个 `page_chunk` 候选。

### 5.2 字段说明

| 字段 | 类型 | 来源 | LLM 生成 | 含义 | 检索服务使用 |
| --- | --- | --- | --- | --- | --- |
| `id` | bigserial | 数据库自增 | 否 | 内部行 ID | 当前检索服务不用 |
| `doc_id` | uuid | 文档 ID | 否 | 所属文档 | join 文档本体 |
| `page_number` | integer | parser 输出 | 否 | 页码或合成页码 | 组成 chunk_id/path |
| `content` | text | parser 提取正文 | 否，扫描 PDF 可能来自 Ark Vision OCR | 页面完整文本 | 作为 `page` chunk content 检索 |

唯一约束：

```text
(doc_id, page_number)
```

### 5.3 不同文件类型的 page 来源

- Markdown：如果存在 `[[PAGE N]]` 标记，则按标记切页；否则默认每 120 行合成一页。
- TXT：按 chunk 生成 page，每个文本片段对应一个 page。
- PDF：文本型 PDF 来自 PyMuPDF/PyPDF2；扫描 PDF 可来自 Ark Vision OCR。
- DOCX：按段落集合合成 page，当前大致每 5 个段落一页。
- PPTX：每张 slide 对应一个 page。
- XLSX：按 sheet/区域生成 page。

### 5.4 检索服务使用方式

`rag-retrieval-service` 会查询 `doc_pages`，构造：

```text
chunk_id = {doc_id}:page:{page_number}
source_type = page
path = page:{page_number}
content = doc_pages.content
metadata_text = doc_name + doc_description
chunk_meta = {"page_number": page_number}
```

page chunk 的优势是保留完整正文；缺点是可能过长，主题混杂。

## 6. `doc_nodes` 表

### 6.1 作用

`doc_nodes` 保存结构化节点树。它来源于文档标题、目录、页窗口、段落块、slide、sheet 区域等。检索服务会把每个 node 作为一个 `node_chunk` 候选。

`doc_nodes` 是一棵树拍平后的结果，树关系由 `parent_node_id`、`sibling_order`、`level`、`child_count` 表达。

### 6.2 字段说明

| 字段 | 类型 | 来源 | LLM 生成 | 含义 | 检索服务使用 |
| --- | --- | --- | --- | --- | --- |
| `id` | bigserial | 数据库自增 | 否 | 内部行 ID，保留插入顺序 | 查询排序使用 `n.id` |
| `doc_id` | uuid | 文档 ID | 否 | 所属文档 | join 文档本体 |
| `node_id` | varchar(64) | parser 生成；通常如 `0000`、`0001` | 否 | 文档内节点 ID | 组成 chunk_id/path |
| `parent_node_id` | varchar(64) | flatten 树结构时生成 | 否 | 父节点 ID；根节点为空 | 重建结构树；当前检索服务不直接用 |
| `sibling_order` | integer | 当前节点在同父节点下的顺序 | 否 | 兄弟节点排序 | 重建结构树；当前检索服务不直接用 |
| `level` | smallint | 标题层级/树深度 | 否 | Markdown `#`/`##` 等层级，或 parser 推断层级 | 当前检索服务不直接用 |
| `title` | text | 标题、页标题、slide 标题、sheet/文本片段标题 | 否 | 节点标题 | node chunk content 的第一部分 |
| `text` | text | 节点正文 | 否，扫描 PDF text 可能来自 Ark Vision OCR | 节点对应正文 | 当 summary 为空时用于 node chunk content |
| `summary` | text | Ark summary 或本地 fallback | 是/可 fallback | 节点摘要 | 优先用于 node chunk content |
| `paragraph_index` | integer | DOCX/TXT 等段落 parser | 否 | 节点起始段落编号 | 当前检索服务不用 |
| `start_index` | integer | parser 输出 | 否 | 起始页/段/slide/sheet 区域编号 | 组成 node path，返回 chunk_meta |
| `end_index` | integer | parser 输出 | 否 | 结束页/段/slide/sheet 区域编号 | 组成 node path，返回 chunk_meta |
| `child_count` | smallint | flatten 时统计 children 数量 | 否 | 子节点数量 | 重建结构树时判断是否保留 `nodes` |

唯一约束：

```text
(doc_id, node_id)
```

索引：

```text
idx_doc_nodes_doc_id(doc_id)
idx_doc_nodes_parent(doc_id, parent_node_id)
```

### 6.3 树结构示例

原始 Markdown：

```markdown
# 项目报告

## 背景
背景正文。

## 方案
方案总述。

### 方案 A
方案 A 正文。

### 方案 B
方案 B 正文。
```

可能生成的 `doc_nodes`：

```text
node_id | parent_node_id | sibling_order | level | title    | child_count
0000    | null           | 0             | 1     | 项目报告 | 2
0001    | 0000           | 0             | 2     | 背景     | 0
0002    | 0000           | 1             | 2     | 方案     | 2
0003    | 0002           | 0             | 3     | 方案 A   | 0
0004    | 0002           | 1             | 3     | 方案 B   | 0
```

树结构为：

```text
项目报告
  背景
  方案
    方案 A
    方案 B
```

### 6.4 Markdown node 生成规则

Markdown parser 会识别 ATX 标题：

```text
#
##
###
...
```

每个标题到下一个标题之间的文本作为该 node 的 `text`。没有标题时，会生成一个标题为 `全文` 的节点。

随后：

1. 根据标题层级构建树。
2. 写入 node_id。
3. 可选生成 summary。
4. 保存到 `doc_nodes`。

### 6.5 node summary 生成规则

入库时如果 `summary_enabled=true`：

- leaf node 会生成 summary。
- 短文本 leaf node 会使用本地截断 summary，不调用 LLM。
- 长文本 leaf node 会调用 Ark 模型生成 summary。
- parent node 的 summary 主要由子节点标题/summary 拼接得到。
- 文档级 `doc_description` 会基于前几个 node summary 调用 Ark 生成。

因此 summary 字段是检索效果的重要变量。

### 6.6 检索服务使用方式

`rag-retrieval-service` 查询 `doc_nodes` 后构造 node chunk：

```text
chunk_id = {doc_id}:node:{node_id}
source_type = node
path = node:{node_id} page:{start_index}-{end_index}
content = title + "\n" + (summary or text)
metadata_text = doc_name + doc_description
chunk_meta = {"node_id": node_id, "start_index": start_index, "end_index": end_index}
```

注意当前逻辑是：

```text
summary 优先于 text
```

如果 summary 存在，node chunk 的正文部分使用 summary；只有 summary 为空时才使用 text。

## 7. 检索服务如何打分

当前 `rag-retrieval-service` 的 keyword baseline 不是数据库全文检索，也不是向量检索。

流程：

1. SQL 按调用方给出的 doc_id 和 `ready` 状态读取文档；`doc_ids/temp_doc_ids` 合并处理。
2. SQL 不使用 query 做候选过滤。
3. Python 对 query 做英文词和中文 2/3/4 gram 切词。
4. Python 对每个 candidate chunk 进行字符串包含式打分。
5. 返回 top_k。

打分逻辑：

```text
完整 query 命中 chunk.content: +40
完整 query 命中 metadata_text: +20
term 命中 chunk.content: +10 + min(count, 5)
term 命中 metadata_text: +5 + min(count, 3)
node chunk: +1
query 包含 “谁” 时，若内容含负责人/研究员/博士等字段额外加分
content 长度超过 3000: -0.5
```

其中：

```text
metadata_text = doc_name + doc_description
```

因此检索效果会受到以下字段影响：

- `doc_nodes.title`
- `doc_nodes.summary`
- `doc_nodes.text`
- `doc_pages.content`
- `documents.doc_name`
- `documents.doc_description`

## 8. 入库字段与检索字段的关系

| 入库字段 | 是否用于检索 | 使用方式 |
| --- | --- | --- |
| `documents.doc_name` | 是 | metadata_text，字段加权较低 |
| `documents.doc_description` | 是 | metadata_text，影响文档路由和 chunk 打分 |
| `documents.status` | 是 | 只检索 `ready` |
| `document_bindings.user_id` | 否 | 不参与检索过滤；仅保留归属记录和 meta 兼容字段 |
| `document_bindings.kb_id` | 否 | 不参与检索过滤；仅保留归属记录和 meta 兼容字段 |
| `doc_pages.content` | 是 | page chunk 正文 |
| `doc_nodes.title` | 是 | node chunk 标题 |
| `doc_nodes.summary` | 是 | node chunk 正文优先来源 |
| `doc_nodes.text` | 是 | summary 为空时的 node chunk 正文来源 |
| `doc_nodes.start_index/end_index` | 部分使用 | path 和 chunk_meta，用于引用展示 |
| `doc_nodes.parent_node_id/sibling_order/level/child_count` | 暂不用于检索 | 用于表达和重建树结构 |
| `documents.raw` | 暂不用于检索 | 调试/回放完整结构 |

## 9. 评测集标注设计建议

当前不建议为评测 query 修改生产表结构。建议将 query 标注保存在外部 manifest 或评测专用表中。

推荐外部 manifest 格式：

```json
{
  "query_id": "eval_q_000001",
  "query": "这段内容适合回答的问题是什么？",
  "doc_id": "...",
  "node_id": "0003",
  "node_title": "...",
  "source": "generated_from_node_text",
  "generator_model": "doubao-or-local-model",
  "confidence": 0.88
}
```

这样可以直接评估：

```text
query_id -> doc_id -> node_id
```

对于 page chunk，可以通过 `doc_nodes.start_index/end_index` 派生出期望 page：

```text
query_id -> doc_id -> page_number
```

## 10. 后续可能的 schema 扩展方向

主管当前要求不修改表结构，因此本节只作为后续技术改型参考。

如果未来允许扩展 schema，优先级最高的是：

### 10.1 `doc_nodes.keywords`

建议类型：

```text
text[] 或 jsonb
```

用途：保存 node 的实体、术语、别名、主题词。

价值：

- 对 keyword 检索直接有用。
- 可和 summary 同一次 LLM 调用生成。
- 比 generated queries 更稳定。
- 可用于 debug、文档路由、Agentic Retrieval 的候选解释。

### 10.2 `documents.routing_keywords`

建议类型：

```text
text[] 或 jsonb
```

用途：保存文档级主题词，用于不传 doc_ids 时的文档级 shortlist。

价值：

- 对多文档路由很有用。
- 可避免在大量文档场景下直接全量扫 node/page。
- 可由 node summary/keywords 聚合生成。

### 10.3 不建议优先新增的字段

不建议第一批直接把以下字段塞进生产核心表：

```text
doc_nodes.generated_queries
doc_nodes.embedding
doc_nodes.answer
doc_nodes.score
documents.generated_queries
```

原因：

- `generated_queries` 更像评测/训练标签，应放外部 manifest 或专用 eval 表。
- `embedding` 与当前不用向量库的技术路线不一致。
- `score` 是查询时动态值，不应入库。
- 评测数据和生产索引字段应隔离。

## 11. 持久化注意事项

数据库中的核心内容都写入 PostgreSQL。只要 PostgreSQL 数据目录存在，重启入库服务 tmux、重启 API 进程、重启 PostgreSQL 进程都不会丢失已入库数据。

但如果 PostgreSQL 数据目录位于容器 overlay 文件系统中，容器被删除并重新创建时数据可能丢失。正式执行高成本入库任务前，应确认 PostgreSQL 数据目录挂载在持久化磁盘或正式数据库服务上，并做好备份。
