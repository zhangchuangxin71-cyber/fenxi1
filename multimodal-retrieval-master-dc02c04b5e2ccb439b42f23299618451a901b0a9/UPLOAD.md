# 上传与交付

## 接收方（Linux 复现）

解压 → 进入含 `start_embedding_service.py` 的目录 → **读 [`QUICKSTART.md`](QUICKSTART.md)**。

```bash
tar -xzf multimodal-retrieval-*.tar.gz && cd multimodal-retrieval-*
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && cp .env.example .env
python scripts/verify_reproduction.py --skip-api
python start_embedding_service.py   # 另开终端
python scripts/verify_reproduction.py
```

## 交付方（上传前）

```bash
bash scripts/check_before_upload.sh
# 测试 → 清理 runtime → 生成 release/*.tar.gz
```

### 必传

源码（`embed_core/`、`embedding_service/`、`picture/`、`video_retrieval/`）、`scripts/`、`tests/`、`requirements.txt`、`start_embedding_service.py`、`.env.example`、**`QUICKSTART.md`**、`README.md`、`DEPLOY.md`

### 禁传

| 路径 | 原因 |
|------|------|
| `.env` | 含密钥 |
| `.editorconfig` | 编辑器格式配置，与运行无关 |
| `.venv/`、`models/` | 本地生成 |
| `runtime/*`、`release/` | 运行时/打包产物 |

Milvus 地址由运维提供，写入对方 `.env` 的 `MILVUS_URI`。

详见 [`DEPLOY.md`](DEPLOY.md)。
