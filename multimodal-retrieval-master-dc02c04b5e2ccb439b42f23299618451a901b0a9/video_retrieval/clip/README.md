# clip — Chinese-CLIP 视频检索

从 Milvus **`media_video_clip`** 读取整片视频向量（`embedding_service` 写入），对文本 query 做相似检索。

| 文件 | 作用 |
|------|------|
| `milvus_index.py` | 加载 `media_video_clip` |
| `retrieval.py` | `VideoRetriever` 查询与结果组装 |
| `retriever_factory.py` | 构建检索器 |
| `embedding.py` | Query 侧 CLIP 编码 |

向量入库请使用 **embedding_service**（`POST /v1/videos/clip/embed-jobs`，`write_vector=true`）。
