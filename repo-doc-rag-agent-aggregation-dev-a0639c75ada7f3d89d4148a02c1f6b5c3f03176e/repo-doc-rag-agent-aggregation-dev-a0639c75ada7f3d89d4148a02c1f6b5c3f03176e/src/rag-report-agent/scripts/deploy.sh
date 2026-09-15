#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

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

check_postgres_dsn_for_docker() {
  local key="$1"
  local value
  value="$(read_env_var "$key")"
  if [ -z "$value" ]; then
    echo "Missing ${key} in .env"
    return 1
  fi
  if printf '%s' "$value" | grep -Eq 'postgres(ql)?://.*@(127\.0\.0\.1|localhost|\[::1\]|::1)(:|/)'; then
    echo "Invalid Docker PostgreSQL DSN in ${key}: do not use 127.0.0.1 or localhost inside the agent container."
    echo "When reusing repo-doc-ingestion PostgreSQL on the pageindex network, use host pageindex-postgres."
    echo "Example: ${key}=postgresql://USER:PASSWORD@pageindex-postgres:5432/pageindex"
    return 1
  fi
  return 0
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

docker_network="$(read_env_var DOCKER_NETWORK)"
docker_network="${docker_network:-pageindex}"
app_port="$(read_env_var APP_PORT)"
app_port="${app_port:-8100}"

check_postgres_dsn_for_docker PG_DSN

if ! docker network inspect "$docker_network" >/dev/null 2>&1; then
  echo "Creating Docker network: $docker_network"
  docker network create "$docker_network" >/dev/null
fi

echo "Building and starting rag-agent-codex..."
docker compose up -d --build
docker compose ps

echo
echo "Service started."
echo "Health check: curl http://127.0.0.1:${app_port}/health"
echo "Swagger:      http://127.0.0.1:${app_port}/docs"
echo "Logs:         docker compose logs -f app"
