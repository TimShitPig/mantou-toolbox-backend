#!/usr/bin/env sh
set -eu

RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main}"
COMPOSE_URL="${RAW_BASE%/}/compose.yaml"
PUBLIC_PORT="${PUBLIC_PORT:-8787}"
UPDATE_IMAGE_REPOSITORY="${UPDATE_IMAGE_REPOSITORY:-ghcr.nju.edu.cn/timshitpig/mantou-toolbox-backend}"
BACKEND_IMAGE="${BACKEND_IMAGE:-${UPDATE_IMAGE_REPOSITORY}:latest}"
UPDATE_AGENT_IMAGE="${UPDATE_AGENT_IMAGE:-$BACKEND_IMAGE}"
WECHAT_APP_ID="${WECHAT_APP_ID:-wx35d2ab50302daa5f}"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  printf '%s\n' 'Docker Compose v2 is required.' >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  printf '%s\n' 'curl is required.' >&2
  exit 1
fi

COMPOSE_TEMP="$PWD/.compose.yaml.tmp.$$"
trap 'rm -f "$COMPOSE_TEMP"' EXIT HUP INT TERM
curl -fsSL --connect-timeout 10 --max-time 60 "$COMPOSE_URL" -o "$COMPOSE_TEMP"
if ! grep -q '^services:' "$COMPOSE_TEMP"; then
  printf '%s\n' 'Downloaded Compose file is invalid.' >&2
  exit 1
fi
mv "$COMPOSE_TEMP" "$PWD/compose.yaml"
trap - EXIT HUP INT TERM

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

detect_public_ipv4() {
  for endpoint in https://api.ipify.org https://ifconfig.me/ip; do
    address="$(curl -4 -fsS --connect-timeout 2 --max-time 3 "$endpoint" 2>/dev/null | tr -d '\r\n ' || true)"
    if printf '%s' "$address" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
      printf '%s' "$address"
      return 0
    fi
  done
  return 1
}

mkdir -p content
if [ ! -f .env ]; then
  APP_BASE_URL="${APP_BASE_URL:-}"
  if [ -z "$APP_BASE_URL" ]; then
    PUBLIC_IP="$(detect_public_ipv4 || true)"
    if [ -n "$PUBLIC_IP" ]; then
      APP_BASE_URL="http://${PUBLIC_IP}:${PUBLIC_PORT}"
    else
      APP_BASE_URL="http://127.0.0.1:${PUBLIC_PORT}"
      printf '%s\n' 'Public IP detection failed; set APP_BASE_URL in .env if remote access is needed.' >&2
    fi
  fi

  umask 077
  APP_SECRET="${APP_SECRET:-$(generate_secret)}"
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(generate_secret)}"
  UPDATE_AGENT_SECRET="${UPDATE_AGENT_SECRET:-$(generate_secret)}"
  cat > .env <<EOF
PUBLIC_PORT=${PUBLIC_PORT}
APP_BASE_URL=${APP_BASE_URL}
APP_SECRET=${APP_SECRET}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
UPDATE_AGENT_SECRET=${UPDATE_AGENT_SECRET}
BACKEND_IMAGE=${BACKEND_IMAGE}
UPDATE_AGENT_IMAGE=${UPDATE_AGENT_IMAGE}
UPDATE_IMAGE_REPOSITORY=${UPDATE_IMAGE_REPOSITORY}
UPDATE_DEPLOY_DIR=${PWD}
ALLOW_DEVELOPMENT_LOGIN=false
WECHAT_APP_ID=${WECHAT_APP_ID}
WECHAT_APP_SECRET=${WECHAT_APP_SECRET:-}
CONTENT_DIR=./content
EOF
  chmod 600 .env
  printf 'Admin password: %s\n' "$ADMIN_PASSWORD"
else
  chmod 600 .env
  printf '%s\n' 'Existing .env was preserved.'
fi

SAVED_APP_BASE_URL="$(sed -n 's/^APP_BASE_URL=//p' .env | tail -n 1)"
printf 'Prepared Compose deployment in %s\n' "$PWD"
printf 'Admin UI: %s/admin\n' "${SAVED_APP_BASE_URL:-http://127.0.0.1:${PUBLIC_PORT}}"
printf 'Next: docker compose up -d\n'
