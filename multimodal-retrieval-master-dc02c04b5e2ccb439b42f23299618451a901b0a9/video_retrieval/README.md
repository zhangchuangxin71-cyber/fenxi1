# video_retrieval — 视频检索

本目录仅保留**检索**：Chinese-CLIP 整片视频检索 + Hybrid（BGE 稠密 + SQLite 稀疏）。  
**不入库**：视频向量与描述由 [`embedding_service`](../embedding_service/README.md) 写入 Milvus。

## Milvus 集合

| 模式 | 集合 | 写入方式 |
|------|------|----------|
| CLIP | `media_video_clip` | `embedding_service` 视频 CLIP `write_vector=true` |
| Hybrid | `hybrid_video_bge` | embedding_service 视频 BGE 或其它上游 |

初始化：`python -m embedding_service.milvus_init`

入库与检索验证：`python scripts/oss_milvus_ingest_verify.py`

## 目录

```
video_retrieval/
├── clip/           # CLIP 检索（Milvus media_video_clip）
├── hybrid/         # BGE + FTS 混合检索
├── api.py
├── service.py
├── models/         # CLIP 权重（编码 query）
└── profiles/
```

## 启动检索 API

```bash
python -m video_retrieval.api \
  --profile my_media \
  --model-path video_retrieval/models \
  --port 8023
```

### POST /search

```json
{"query": "一个人在厨房做饭", "mode": "both", "top_k": 10}
```

`mode`: `clip` | `hybrid` | `both`

## 环境检查（可选）

```bash
python -m video_retrieval.clip.validate_env --model-path video_retrieval/models
```
