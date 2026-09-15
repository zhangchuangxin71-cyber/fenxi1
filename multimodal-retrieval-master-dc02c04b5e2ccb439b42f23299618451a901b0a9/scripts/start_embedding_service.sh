#!/usr/bin/env bash
# 裸机后台启动 embedding service（推荐替代在终端里直接 nohup ... &）
# 在子 shell 中拉起进程，当前终端不会出现 [1]+ Killed 等 job 提示。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/runtime/embedding_service.pid"
LOG_FILE="${EMBEDDING_SERVICE_LOG:-/tmp/embedding_service.log}"
PORT="${EMBEDDING_SERVICE_PORT:-8030}"

cd "$ROOT"

if [[ ! -f "$ROOT/.venv/bin/activate" ]]; then
  echo "未找到 .venv，请先在项目根目录创建虚拟环境"
  exit 1
fi

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE")"
  if kill -0 "$old_pid" 2>/dev/null; then
    echo "服务已在运行 (PID $old_pid)"
    echo "  停止: bash scripts/stop_embedding_service.sh"
    echo "  健康: curl http://127.0.0.1:${PORT}/health"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

mkdir -p "$ROOT/runtime"

# 在子 shell 内启动，与操作者的交互式 shell 作业表脱钩
(
  source "$ROOT/.venv/bin/activate"
  export EMBEDDING_SERVICE_PORT="$PORT"
  exec nohup python "$ROOT/start_embedding_service.py" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
)

sleep 1
new_pid="$(cat "$PID_FILE")"
if ! kill -0 "$new_pid" 2>/dev/null; then
  echo "启动失败，请查看日志: $LOG_FILE"
  tail -20 "$LOG_FILE" 2>/dev/null || true
  rm -f "$PID_FILE"
  exit 1
fi

echo "Embedding service 已启动"
echo "  PID:  $new_pid"
echo "  Port: $PORT"
echo "  Log:  $LOG_FILE"
echo "  停止: bash scripts/stop_embedding_service.sh"
echo "  健康: curl http://127.0.0.1:${PORT}/health"
