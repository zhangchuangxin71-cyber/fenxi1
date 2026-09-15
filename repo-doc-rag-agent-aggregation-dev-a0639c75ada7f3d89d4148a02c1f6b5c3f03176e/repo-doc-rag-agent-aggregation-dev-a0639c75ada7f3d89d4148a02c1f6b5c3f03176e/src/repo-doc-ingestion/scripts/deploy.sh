#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "Missing .env. Create it first: cp .env.example .env"
  exit 1
fi

docker compose up -d --build
docker compose ps
