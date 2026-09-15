#!/usr/bin/env bash
# 生成可上传的源码包（排除密钥、venv、模型权重、运行时数据）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NAME="${PACKAGE_NAME:-multimodal-retrieval}"
VERSION="${PACKAGE_VERSION:-$(date +%Y%m%d)}"
OUT_DIR="$ROOT/release"
ARCHIVE="$OUT_DIR/${NAME}-${VERSION}.tar.gz"

if [[ -f "$ROOT/.env" ]]; then
  echo "[check] 发现 .env（不会打入包内，请确认未手动拷贝进 release/）"
fi

mkdir -p "$OUT_DIR"

echo "打包: $ARCHIVE"
tar -czf "$ARCHIVE" \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='runtime/logs/*' \
  --exclude='runtime/tmp/*' \
  --exclude='runtime/docker-data' \
  --exclude='runtime/milvus_lite_data' \
  --exclude='runtime/milvus_lite.db' \
  --exclude='runtime/*.log' \
  --exclude='runtime/tasks.db' \
  --exclude='volumes' \
  --exclude='models/*.bin' \
  --exclude='models/*.safetensors' \
  --exclude='models/*.pt' \
  --exclude='models/**/.mv' \
  --exclude='models/**/.msc' \
  --exclude='picture/profiles' \
  --exclude='video_retrieval/profiles' \
  --exclude='video_retrieval/models' \
  --exclude='video_retrieval/artifacts' \
  --exclude='video_retrieval/videos' \
  --exclude='release' \
  --exclude='.git' \
  --exclude='.editorconfig' \
  -C "$ROOT" \
  .

SIZE=$(du -h "$ARCHIVE" | awk '{print $1}')
echo ""
echo "[done] $ARCHIVE ($SIZE)"
echo "上传前请阅读 UPLOAD.md；对方解压后: cp .env.example .env && pip install -r requirements.txt"
