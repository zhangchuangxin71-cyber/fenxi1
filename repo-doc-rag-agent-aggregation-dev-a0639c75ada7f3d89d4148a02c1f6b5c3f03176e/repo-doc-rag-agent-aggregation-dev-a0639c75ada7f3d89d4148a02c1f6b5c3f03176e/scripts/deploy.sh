#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE=(docker compose --project-directory "$ROOT_DIR" -f "$ROOT_DIR/docker-compose.yaml")
MINERU_IMAGE="rag-platform-mineru-api:latest"
SERVICES=(
  mineru-api
  repo-doc-ingestion
  rag-retrieval-service
  rag-knowledge-chat
  rag-report-agent
  wechat-article-runtime
  wechat-article-agent
)
ENV_FILES=(
  env/mineru.env
  env/ingestion.env
  env/retrieval.env
  env/knowledge-chat.env
  env/report-agent.env
  env/wechat-article.env
  env/wechat-article-db.env
)
REBUILD_MINERU=false

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: bash scripts/deploy.sh [--rebuild-mineru]

By default, the existing MinerU image is reused. MinerU is built automatically
when its image is missing. Use --rebuild-mineru after changing MinerU source,
dependencies, or Dockerfile.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --rebuild-mineru)
      REBUILD_MINERU=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown argument: $1"
      ;;
  esac
  shift
done

read_env_var() {
  local file="$1"
  local key="$2"
  local value
  value="$(sed -n "s/^${key}=//p" "$file" | tail -n 1)"
  value="${value%$'\r'}"
  printf '%s' "$value"
}

prepare_data_dirs() {
  local dir
  for dir in /mnt/data/mineru/models /mnt/data/mineru/output /mnt/data/ingestion/workspace /mnt/data/logs/ingestion /mnt/data/logs/report-agent; do
    mkdir -p -- "$dir" || fail "Cannot create data directory: $dir"
    [ -d "$dir" ] || fail "Data path is not a directory: $dir"
    [ -w "$dir" ] || fail "Data directory is not writable: $dir"
  done
}

require_file() {
  local file="$1"
  [ -f "$file" ] || fail "Missing $file. Copy $file.example to $file and fill production values."
}

require_value() {
  local file="$1"
  local key="$2"
  local value
  value="$(read_env_var "$file" "$key")"
  [ -n "$value" ] || fail "Missing $key in $file."
  if printf '%s' "$value" | grep -Eq '<[^>]+>|change_me|replace_with|your_[a-z_]+'; then
    fail "$key in $file still contains a template placeholder."
  fi
}

check_knowledge_chat_inbound_auth() {
  local file="env/knowledge-chat.env"
  local enabled raw entry
  local -a entries
  enabled="$(read_env_var "$file" AUTH_ENABLED | tr '[:upper:]' '[:lower:]')"
  case "$enabled" in
    ""|0|false|no|off)
      return 0
      ;;
    1|true|yes|on)
      require_value "$file" KNOWLEDGE_CHAT_INBOUND_API_KEYS
      raw="$(read_env_var "$file" KNOWLEDGE_CHAT_INBOUND_API_KEYS)"
      IFS=',' read -r -a entries <<< "$raw"
      for entry in "${entries[@]}"; do
        if [[ ! "$entry" =~ ^[^,:[:space:]]+:[^,[:space:]]+$ ]]; then
          fail "KNOWLEDGE_CHAT_INBOUND_API_KEYS in $file must use comma-separated caller:key entries without whitespace."
        fi
      done
      ;;
    *)
      fail "AUTH_ENABLED in $file must be true or false."
      ;;
  esac
}

check_container_dsn() {
  local file="$1"
  local key="$2"
  local value
  value="$(read_env_var "$file" "$key")"
  if printf '%s' "$value" | grep -Eq 'postgres(ql)?://.*@(127\.0\.0\.1|localhost|\[::1\]|::1)(:|/)'; then
    fail "$key in $file points to localhost. Inside a container, use the production database hostname/IP."
  fi
}

show_diagnostics() {
  local service="${1:-}"
  printf '\nContainer status:\n' >&2
  "${COMPOSE[@]}" ps >&2 || true
  if [ -n "$service" ]; then
    printf '\nRecent logs for %s:\n' "$service" >&2
    "${COMPOSE[@]}" logs --tail=100 "$service" >&2 || true
  fi
}

wait_for_healthy() {
  local service="$1"
  local timeout_seconds="$2"
  local started now container_id status
  started="$(date +%s)"

  while true; do
    container_id="$("${COMPOSE[@]}" ps -q "$service")"
    if [ -n "$container_id" ]; then
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      case "$status" in
        healthy|running)
          printf '  %-26s %s\n' "$service" "$status"
          return 0
          ;;
        unhealthy|exited|dead)
          show_diagnostics "$service"
          fail "$service entered terminal state: $status"
          ;;
      esac
    fi

    now="$(date +%s)"
    if [ $((now - started)) -ge "$timeout_seconds" ]; then
      show_diagnostics "$service"
      fail "$service did not become healthy within ${timeout_seconds}s"
    fi
    sleep 3
  done
}

for file in "${ENV_FILES[@]}"; do
  require_file "$file"
done

chmod 600 -- "${ENV_FILES[@]}" || fail "Cannot restrict environment files to mode 600."
for file in "${ENV_FILES[@]}"; do
  [ "$(stat -c '%a' "$file")" = "600" ] \
    || fail "Environment file permissions must be 600: $file"
done

command -v docker >/dev/null 2>&1 || fail "Docker CLI is not installed."
docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is not installed."

prepare_data_dirs

require_value env/mineru.env MINERU_MODEL_SOURCE
require_value env/ingestion.env POSTGRES_DSN
require_value env/ingestion.env ARK_API_KEY
require_value env/ingestion.env OSS_BUCKET
require_value env/ingestion.env OSS_ENDPOINT
require_value env/ingestion.env OSS_ACCESS_KEY_ID
require_value env/ingestion.env OSS_ACCESS_KEY_SECRET
require_value env/retrieval.env POSTGRES_DSN
require_value env/retrieval.env ARK_API_KEY
require_value env/knowledge-chat.env ARK_API_KEY
require_value env/report-agent.env PG_DSN
require_value env/report-agent.env ARK_API_KEY
require_value env/wechat-article.env DATABASE_URL
require_value env/wechat-article.env ARK_API_KEY
require_value env/wechat-article-db.env DATABASE_ADMIN_URL
check_knowledge_chat_inbound_auth

check_container_dsn env/ingestion.env POSTGRES_DSN
check_container_dsn env/retrieval.env POSTGRES_DSN
check_container_dsn env/report-agent.env PG_DSN
check_container_dsn env/wechat-article.env DATABASE_URL
check_container_dsn env/wechat-article-db.env DATABASE_ADMIN_URL
if [ "$(read_env_var env/wechat-article.env APP_PORT)" != "8140" ]; then
  fail "APP_PORT in env/wechat-article.env must be 8140 for production deployment."
fi
if [ "$(read_env_var env/wechat-article.env DEBUG_ENABLED | tr '[:upper:]' '[:lower:]')" != "false" ]; then
  fail "DEBUG_ENABLED in env/wechat-article.env must be false for production deployment. /docs remains available without debug trace."
fi
if [ "$(read_env_var env/wechat-article.env AGENT_SERVER_URL)" != "http://wechat-article-runtime:8141" ]; then
  fail "AGENT_SERVER_URL in env/wechat-article.env must use the private runtime DNS name."
fi
if [ "$(read_env_var env/wechat-article.env RETRIEVAL_BASE_URL)" != "http://rag-retrieval-service:8120" ]; then
  fail "RETRIEVAL_BASE_URL in env/wechat-article.env must use retrieval service DNS."
fi

printf 'Validating Compose configuration...\n'
"${COMPOSE[@]}" config --quiet

build_services=(
  repo-doc-ingestion
  rag-retrieval-service
  rag-knowledge-chat
  rag-report-agent
  wechat-article-db-init
  wechat-article-runtime
  wechat-article-agent
)
if [ "$REBUILD_MINERU" = true ] || ! docker image inspect "$MINERU_IMAGE" >/dev/null 2>&1; then
  build_services=(mineru-api "${build_services[@]}")
  printf 'Building MinerU, database bootstrap and six application services...\n'
else
  printf 'Reusing existing MinerU image %s; building database bootstrap and six application services...\n' "$MINERU_IMAGE"
fi
"${COMPOSE[@]}" build "${build_services[@]}"

printf 'Initializing the WeChat article database and starting seven long-running services...\n'
"${COMPOSE[@]}" up -d --remove-orphans

health_timeout="${DEPLOY_HEALTH_TIMEOUT_SECONDS:-900}"
printf 'Waiting for service health (timeout=%ss per service)...\n' "$health_timeout"
for service in "${SERVICES[@]}"; do
  wait_for_healthy "$service" "$health_timeout"
done

printf '\nAll platform services are running.\n'
"${COMPOSE[@]}" ps
printf '\nEndpoints:\n'
printf '  MinerU:        http://SERVER:8135/docs\n'
printf '  Ingestion:     http://SERVER:8100/docs\n'
printf '  Report agent:  http://SERVER:8115/docs\n'
printf '  Retrieval:     http://SERVER:8120/docs\n'
printf '  Knowledge chat:http://SERVER:8130/docs\n'
printf '  WeChat article:http://SERVER:8140/health/live\n'
