#!/usr/bin/env bash
# 清理本地运行时产物（不上传）；保留 runtime/tmp、runtime/logs 占位
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

rm -rf runtime/docker-data 2>/dev/null || true
rm -f runtime/*.log runtime/*.db runtime/dockerd.log 2>/dev/null || true
rm -rf runtime/logs/* runtime/tmp/* 2>/dev/null || true
touch runtime/tmp/.gitkeep runtime/logs/.gitkeep

find "$ROOT" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
echo "[done] runtime 与 __pycache__ 已清理（models/、.venv/ 未动）"
