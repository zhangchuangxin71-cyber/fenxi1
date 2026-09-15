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
  port="$(read_env_var API_PORT)"
  port="${port:-8787}"
  if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    echo "Invalid API_PORT in .env: $port"
    return 1
  fi
  return 0
}

oss_upload_enabled() {
  local flag
  flag="$(read_env_var OSS_UPLOAD_ENABLED)"
  flag="${flag:-1}"
  flag="$(printf '%s' "$flag" | tr '[:upper:]' '[:lower:]')"
  case "$flag" in
    0|false|no|off) return 1 ;;
    *) return 0 ;;
  esac
}

check_oss_config() {
  if ! oss_upload_enabled; then
    echo "Note: OSS_UPLOAD_ENABLED is off; compose will not upload deliverables to OSS."
    return 0
  fi

  local missing=()
  local key value
  for key in \
    ALIYUN_OSS_ENDPOINT \
    ALIYUN_OSS_ACCESS_KEY_ID \
    ALIYUN_OSS_ACCESS_KEY_SECRET \
    ALIYUN_OSS_BUCKET; do
    value="$(read_env_var "$key")"
    if [ -z "$value" ]; then
      missing+=("$key")
    fi
  done

  if [ "${#missing[@]}" -eq 0 ]; then
    return 0
  fi

  echo "Error: OSS upload is enabled but .env is missing required variables:"
  for key in "${missing[@]}"; do
    echo "  - $key"
  done
  echo
  echo "Compose deliverables upload to OSS after success. Fill the values in .env"
  echo "(see .env.example) or set OSS_UPLOAD_ENABLED=0 to skip this check."
  return 1
}

warn_workspaces_dir_permissions() {
  mkdir -p workspaces fonts
  local owner_uid
  owner_uid="$(stat -c '%u' workspaces 2>/dev/null || echo "")"
  if [ "$owner_uid" = "$CONTAINER_UID" ] && [ -w workspaces ]; then
    return 0
  fi

  echo "Note: ./workspaces is bind-mounted; the container runs as UID ${CONTAINER_UID} (appuser)."
  echo "If workspace writes fail, run:"
  echo "  sudo chown -R ${CONTAINER_UID}:${CONTAINER_GID} workspaces"
}

warn_fonts_dir() {
  local count
  count="$(find fonts -maxdepth 1 -type f \( -name '*.otf' -o -name '*.ttf' -o -name '*.ttc' \) 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${count:-0}" -ge 17 ]; then
    return 0
  fi
  if [ "${count:-0}" -eq 0 ]; then
    echo "Warning: ./fonts is empty. Fonts are tracked in Git — run: git checkout origin/master -- fonts/"
  else
    echo "Warning: ./fonts has only ${count} font file(s) (expected ~17). Partial fonts cause tofu for some styles."
    echo "  Run: git checkout origin/master -- fonts/"
  fi
  echo "  See fonts/README.md"
}

wait_for_health() {
  local port="$1"
  local url="http://127.0.0.1:${port}/api/v1/health"
  local max_wait=120
  local interval=3
  local elapsed=0
  local response=""

  echo "Waiting for health check (${url})..."
  while [ "$elapsed" -lt "$max_wait" ]; do
    if response="$(curl -sf --max-time 5 "$url" 2>/dev/null)"; then
      echo "Health check OK."
      echo "$response"
      return 0
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done

  echo "Error: health check did not pass within ${max_wait}s."
  echo "Check logs: docker compose logs --tail 80 app"
  return 1
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
check_oss_config
warn_workspaces_dir_permissions
warn_fonts_dir

service_port="$(read_env_var API_PORT)"
service_port="${service_port:-8787}"

echo "Building and starting videoaudiotext-api..."
docker compose up -d --build
docker compose ps

wait_for_health "$service_port"

echo
echo "Service started."
echo "Health check: curl http://127.0.0.1:${service_port}/api/v1/health"
echo "Swagger:      http://127.0.0.1:${service_port}/docs"
echo "Logs:         docker compose logs -f app"
