#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/runtime/embedding_service.pid"

stopped=0

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "已停止 embedding service (PID $pid)"
    stopped=1
  fi
  rm -f "$PID_FILE"
fi

# 清理残留（例如未写 pid 文件的手动启动）
if pkill -0 -f "python start_embedding_service.py" 2>/dev/null; then
  pkill -TERM -f "python start_embedding_service.py" 2>/dev/null || true
  sleep 2
  pkill -9 -f "python start_embedding_service.py" 2>/dev/null || true
  echo "已清理残留的 start_embedding_service 进程"
  stopped=1
fi

if [[ "$stopped" -eq 0 ]]; then
  echo "未发现运行中的 embedding service"
fi
