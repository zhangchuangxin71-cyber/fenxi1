#!/usr/bin/env bash
# 上传前自检：单元测试 + 打包（不打包 .env / .venv / 模型权重）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

echo "=== 1/3 单元测试 ==="
"$PY" -m unittest discover -s tests -v

echo ""
echo "=== 2/3 敏感文件检查 ==="
if [[ -f .env ]]; then
  echo "[ok] 本地存在 .env（不会打入 release 包，请勿手动上传）"
else
  echo "[hint] 无 .env，对方需 cp .env.example .env"
fi

echo ""
echo "=== 3/3 生成发布包 ==="
bash scripts/clean_runtime.sh
bash scripts/package_for_upload.sh

echo ""
echo "[done] 上传 release/multimodal-retrieval-*.tar.gz 即可（不要上传 .env、.venv、models 权重）"
