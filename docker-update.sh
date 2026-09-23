#!/usr/bin/env sh
set -eu

IMAGE="${IMAGE:-ghcr.io/timshitpig/mantou-toolbox-backend:latest}"
NAME="${NAME:-mantou-toolbox}"
PORT="${PORT:-8787}"
APP_BASE_URL="${APP_BASE_URL:-http://127.0.0.1:${PORT}}"
APP_SECRET="${APP_SECRET:-change-this-secret-before-production}"

mkdir -p "$PWD/data" "$PWD/content"
docker pull "$IMAGE"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -itd \
  --restart unless-stopped \
  -p "${PORT}:8787" \
  -v "$PWD/data:/app/storage" \
  -v "$PWD/content:/app/content:ro" \
  -v /etc/localtime:/etc/localtime:ro \
  -v /etc/timezone:/etc/timezone:ro \
  -e "APP_BASE_URL=${APP_BASE_URL}" \
  -e "APP_SECRET=${APP_SECRET}" \
  --name "$NAME" \
  "$IMAGE"
