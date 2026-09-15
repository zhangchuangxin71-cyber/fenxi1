#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# Must match Dockerfile appuser (--uid/--gid 1000)
CONTAINER_UID=1000
CONTAINER_GID=1000

read_env_var() {
  local key="$1"
  local value
  value="$(sed -n "s/^${key}=//p" .env 2>/dev/null | tail -n 1 || true)"
  value="${value%%#*}"
  value="${value%"${value##*[![:space:]]}"}"
  value="${value#${value%%[![:space:]]*}}"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

check_required_port() {
  local port
  port="$(read_env_var EMBEDDING_SERVICE_PORT)"
  if [ -z "$port" ]; then
    echo "Missing EMBEDDING_SERVICE_PORT in .env"
    echo "Example: EMBEDDING_SERVICE_PORT=8030"
    return 1
  fi
  if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    echo "Invalid EMBEDDING_SERVICE_PORT in .env: $port"
    return 1
  fi
  return 0
}

warn_if_models_missing() {
  local auto_download
  auto_download="$(read_env_var AUTO_DOWNLOAD_MODELS)"
  auto_download="${auto_download:-1}"
  if [ -d models ] && [ -f models/pytorch_model.bin ] && [ -d models/bge-large-zh-v1.5 ]; then
    return 0
  fi
  case "$auto_download" in
    0|false|no|off)
      echo "Warning: models/ is incomplete and AUTO_DOWNLOAD_MODELS is disabled."
      echo "Run: python scripts/download_models.py --use-mirror"
      echo "Or set AUTO_DOWNLOAD_MODELS=1 in .env before first start."
      ;;
    *)
      echo "Note: models/ will be populated on first start (AUTO_DOWNLOAD_MODELS=1)."
      ;;
  esac
}

warn_models_dir_permissions() {
  local auto_download owner_uid
  auto_download="$(read_env_var AUTO_DOWNLOAD_MODELS)"
  auto_download="${auto_download:-1}"
  case "$auto_download" in
    0|false|no|off) return 0 ;;
  esac

  mkdir -p models
  owner_uid="$(stat -c '%u' models 2>/dev/null || echo "")"
  if [ "$owner_uid" = "$CONTAINER_UID" ] && [ -w models ]; then
    return 0
  fi

  echo "Note: ./models is bind-mounted; the container runs as UID ${CONTAINER_UID} (appuser)."
  echo "If auto-download fails with permission errors, run:"
  echo "  sudo chown -R ${CONTAINER_UID}:${CONTAINER_GID} models"
}

if [ ! -f .env ]; then
  echo "Missing .env. Create it first: cp .env.example .env"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Missing docker command. Install Docker first."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Missing Docker Compose plugin. Install docker compose first."
  exit 1
fi

check_required_port
warn_if_models_missing
warn_models_dir_permissions

service_port="$(read_env_var EMBEDDING_SERVICE_PORT)"

echo "Building and starting multimodal-embedding-service (VECTOR_STORE=none)..."
sudo chown -R 1000:1000 ./models
docker compose up -d --build
docker compose ps

echo
echo "Service started."
echo "First deploy may take several minutes (model download + load)."
echo "docker compose ps may show 'unhealthy' until /health responds (up to ~10 min)."
echo "Health check: curl http://127.0.0.1:${service_port}/health"
echo "Swagger:      http://127.0.0.1:${service_port}/docs"
echo "Logs:         docker compose logs -f app  (stdout only; no embedding_service.log file)"
