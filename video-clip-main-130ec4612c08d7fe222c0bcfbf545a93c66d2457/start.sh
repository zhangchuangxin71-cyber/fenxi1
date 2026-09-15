#!/usr/bin/env bash
# 一键启停（Docker Compose）
# 用法：
#   ./start.sh              # 后台启动（不强制 rebuild；有镜像则复用）
#   ./start.sh up           # 同上
#   ./start.sh build        # 仅构建镜像（注入当前 git short SHA）
#   ./start.sh redeploy     # 【拉代码后用这个】build + 强制重建容器
#   ./start.sh down         # 停止
#   ./start.sh restart      # 仅重启容器（不会带上新代码）
#   ./start.sh logs         # 跟踪 api 日志
#   ./start.sh status       # 查看容器状态
#   ./start.sh health       # 探测 /api/v1/health（含 git_sha）
#   ./start.sh verify       # 对比宿主机 git 与容器 health.git_sha
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker，请先安装 Docker / Docker Compose。"
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "未找到 docker compose 插件，请升级 Docker。"
  exit 1
fi

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    echo "已从 .env.example 生成 .env，请填写 OSS_* 后再对外使用。"
  else
    echo "缺少 .env / .env.example"
    exit 1
  fi
fi

API_PORT="${API_PORT:-8010}"
IMAGE="${IMAGE:-video-clip-api:latest}"
if grep -qE '^[[:space:]]*API_PORT=' .env 2>/dev/null; then
  # shellcheck disable=SC1091
  API_PORT="$(set -a; source .env >/dev/null 2>&1; set +a; echo "${API_PORT:-8010}")"
fi
if grep -qE '^[[:space:]]*IMAGE=' .env 2>/dev/null; then
  # shellcheck disable=SC1091
  IMAGE="$(set -a; source .env >/dev/null 2>&1; set +a; echo "${IMAGE:-video-clip-api:latest}")"
fi

# 注入构建身份：health 可据此判断是否已换成新镜像
export APP_GIT_SHA
export APP_BUILD_TIME
APP_GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
APP_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

_health_json() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:${API_PORT}/api/v1/health"
  else
    python3 -c "
import urllib.request
print(urllib.request.urlopen('http://127.0.0.1:${API_PORT}/api/v1/health', timeout=10).read().decode())
"
  fi
}

cmd="${1:-up}"

case "$cmd" in
  build)
    echo "构建镜像 ${IMAGE}  git_sha=${APP_GIT_SHA}  build_time=${APP_BUILD_TIME}"
    docker compose build
    echo
    echo "镜像已构建: ${IMAGE}"
    echo "离线交付: docker save ${IMAGE} -o video-clip-api.tar"
    echo "下一步: ./start.sh up   或一次性更新: ./start.sh redeploy"
    ;;
  up|start)
    mkdir -p data/uploads data/outputs data/work
    if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
      echo "未找到镜像 ${IMAGE}，开始构建（首次或未 docker load 时）。"
      echo "构建参数 git_sha=${APP_GIT_SHA} build_time=${APP_BUILD_TIME}"
      docker compose build
    else
      image_sha="$(docker image inspect "${IMAGE}" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^APP_GIT_SHA=//p' | head -n 1)"
      if [[ -n "${image_sha}" && "${image_sha}" != "unknown" && "${image_sha}" != "${APP_GIT_SHA}" ]]; then
        echo "检测到镜像 git_sha=${image_sha} 与当前代码 ${APP_GIT_SHA} 不一致，自动重建。"
        docker compose build
        docker compose up -d --force-recreate
        echo "已用当前代码重建容器。"
        exit 0
      fi
      echo "提示: 已存在匹配镜像 ${IMAGE}，直接启动。"
    fi
    # 即使不 rebuild，也把当前环境的 SHA 写入容器 env（仅作对照；真代码仍以镜像内 COPY 为准）
    docker compose up -d
    echo
    echo "已启动（单 API：内存账本，不跨重启恢复）。"
    echo "健康检查: ./start.sh health"
    echo "版本核对: ./start.sh verify"
    echo "日志: ./start.sh logs    停止: ./start.sh down"
    ;;
  redeploy)
    mkdir -p data/uploads data/outputs data/work
    echo "redeploy: 重建镜像并强制换容器  ${IMAGE}"
    echo "          git_sha=${APP_GIT_SHA}  build_time=${APP_BUILD_TIME}"
    docker compose build
    docker compose up -d --force-recreate
    echo
    echo "已用最新镜像重建容器。"
    echo "请执行: ./start.sh verify"
    ;;
  down|stop)
    docker compose down
    ;;
  restart)
    echo "警告: restart 不会把 git pull 的新代码打进镜像。"
    echo "      更新代码请用: ./start.sh redeploy"
    docker compose restart
    ;;
  logs)
    docker compose logs -f api
    ;;
  status|ps)
    docker compose ps
    ;;
  health)
    _health_json
    echo
    ;;
  verify)
    if ! docker ps --filter name=video-clip-api --filter status=running -q | grep -q .; then
      echo "容器未在运行，请先 ./start.sh redeploy 或 ./start.sh up"
      exit 1
    fi
    echo "本地 git:     ${APP_GIT_SHA}"
    body="$(_health_json)" || {
      echo "FAIL 无法访问 /api/v1/health"
      exit 1
    }
    echo "health 原文:  ${body}"
    running_sha="$(
      python3 -c "
import json,sys
d=json.loads(sys.argv[1])
print((d.get('data') or {}).get('git_sha') or 'missing')
" "${body}"
    )"
    echo "容器 git_sha: ${running_sha}"

    miss=0
    if ! docker exec video-clip-api test -f /app/backend/storage/http_download.py; then
      echo "FAIL 缺少 http_download.py → 旧镜像"
      miss=1
    else
      echo "OK   找到 http_download.py"
    fi

    if [[ "${running_sha}" == "unknown" || "${running_sha}" == "missing" ]]; then
      echo "WARN 容器 git_sha=${running_sha}（镜像未注入构建参数，或未走 ./start.sh build/redeploy）"
      miss=1
    elif [[ "${running_sha}" != "${APP_GIT_SHA}" ]]; then
      echo "FAIL 容器 git_sha(${running_sha}) ≠ 本地(${APP_GIT_SHA}) → 未 redeploy 或拉错提交"
      miss=1
    else
      echo "OK   git_sha 与本地一致"
    fi

    if [[ "${miss}" -ne 0 ]]; then
      echo
      echo "请执行: git pull && ./start.sh redeploy && ./start.sh verify"
      exit 1
    fi
    echo "镜像校验通过。"
    ;;
  *)
    echo "用法: $0 {up|build|redeploy|down|restart|logs|status|health|verify}"
    exit 1
    ;;
esac
