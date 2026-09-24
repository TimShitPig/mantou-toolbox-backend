#!/usr/bin/env sh
set -eu

IMAGE="${IMAGE:-ghcr.nju.edu.cn/timshitpig/mantou-toolbox-backend:latest}"
NAME="${NAME:-mantou-toolbox}"
UPDATER_IMAGE="${UPDATER_IMAGE:-ghcr.nju.edu.cn/timshitpig/mantou-toolbox-backend:latest}"
UPDATE_AGENT_NAME="${UPDATE_AGENT_NAME:-${NAME}-updater}"
UPDATE_NETWORK="${UPDATE_NETWORK:-${NAME}-network}"
PUBLIC_PORT="${PUBLIC_PORT:-${PORT:-}}"
if [ -z "$PUBLIC_PORT" ] && [ -f "$PWD/.env" ]; then
  PUBLIC_PORT="$(sed -n 's/^PUBLIC_PORT=//p' "$PWD/.env" | tail -n 1)"
fi
PUBLIC_PORT="${PUBLIC_PORT:-8787}"
WECHAT_APP_ID="${WECHAT_APP_ID:-wx35d2ab50302daa5f}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

detect_public_ipv4() {
  for endpoint in https://api.ipify.org https://ifconfig.me/ip https://checkip.amazonaws.com; do
    address="$(curl -4 -fsS --max-time 5 "$endpoint" 2>/dev/null | tr -d '\r\n ' || true)"
    if printf '%s' "$address" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
      printf '%s' "$address"
      return 0
    fi
  done
  return 1
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

mkdir -p "$PWD/data" "$PWD/content"

if [ ! -f "$PWD/.env" ]; then
  umask 077
  if [ -z "${APP_BASE_URL:-}" ]; then
    PUBLIC_IP="${PUBLIC_IP:-$(detect_public_ipv4 || true)}"
    if [ -n "$PUBLIC_IP" ]; then
      APP_BASE_URL="http://${PUBLIC_IP}:${PUBLIC_PORT}"
    else
      APP_BASE_URL="http://127.0.0.1:${PUBLIC_PORT}"
      printf '%s\n' 'Could not detect the public IP. Set APP_BASE_URL in .env before using the mini program remotely.' >&2
    fi
  fi
  APP_SECRET="${APP_SECRET:-$(generate_secret)}"
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(generate_secret)}"
  UPDATE_AGENT_SECRET="${UPDATE_AGENT_SECRET:-$(generate_secret)}"
  cat > "$PWD/.env" <<EOF
NODE_ENV=production
HOST=0.0.0.0
PORT=8787
PUBLIC_PORT=${PUBLIC_PORT}
APP_BASE_URL=${APP_BASE_URL}
APP_SECRET=${APP_SECRET}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
UPDATE_AGENT_SECRET=${UPDATE_AGENT_SECRET}
ALLOW_DEVELOPMENT_LOGIN=false
WECHAT_APP_ID=${WECHAT_APP_ID}
WECHAT_APP_SECRET=${WECHAT_APP_SECRET:-}
EOF
  chmod 600 "$PWD/.env"
  printf 'Generated server configuration: %s/.env\n' "$PWD"
  if [ -z "${WECHAT_APP_SECRET:-}" ]; then
    printf '%s\n' 'WeChat profile login stays disabled until WECHAT_APP_SECRET is added to .env.'
  fi
fi

SAVED_UPDATE_AGENT_SECRET="$(sed -n 's/^UPDATE_AGENT_SECRET=//p' "$PWD/.env" | tail -n 1)"
if [ -z "$SAVED_UPDATE_AGENT_SECRET" ]; then
  UPDATE_AGENT_SECRET="${UPDATE_AGENT_SECRET:-$(generate_secret)}"
  if grep -q '^UPDATE_AGENT_SECRET=' "$PWD/.env"; then
    TEMP_ENV="$(mktemp)"
    sed "s/^UPDATE_AGENT_SECRET=.*/UPDATE_AGENT_SECRET=${UPDATE_AGENT_SECRET}/" "$PWD/.env" > "$TEMP_ENV"
    cat "$TEMP_ENV" > "$PWD/.env"
    rm -f "$TEMP_ENV"
  else
    printf '\nUPDATE_AGENT_SECRET=%s\n' "$UPDATE_AGENT_SECRET" >> "$PWD/.env"
  fi
  chmod 600 "$PWD/.env"
  SAVED_UPDATE_AGENT_SECRET="$UPDATE_AGENT_SECRET"
fi

SAVED_ADMIN_PASSWORD="$(sed -n 's/^ADMIN_PASSWORD=//p' "$PWD/.env" | tail -n 1)"
if [ -z "$SAVED_ADMIN_PASSWORD" ]; then
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(generate_secret)}"
  if grep -q '^ADMIN_PASSWORD=' "$PWD/.env"; then
    TEMP_ENV="$(mktemp)"
    sed "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=${ADMIN_PASSWORD}/" "$PWD/.env" > "$TEMP_ENV"
    cat "$TEMP_ENV" > "$PWD/.env"
    rm -f "$TEMP_ENV"
  else
    printf '\nADMIN_PASSWORD=%s\n' "$ADMIN_PASSWORD" >> "$PWD/.env"
  fi
  chmod 600 "$PWD/.env"
fi

SAVED_APP_BASE_URL="$(sed -n 's/^APP_BASE_URL=//p' "$PWD/.env" | tail -n 1)"
printf 'Admin UI: %s/admin\n' "${SAVED_APP_BASE_URL:-http://127.0.0.1:${PUBLIC_PORT}}"
printf 'Admin password is stored in %s/.env (read with: sudo grep ^ADMIN_PASSWORD= %s/.env)\n' "$PWD" "$PWD"

docker pull "$IMAGE"
docker pull "$UPDATER_IMAGE"
docker network create "$UPDATE_NETWORK" >/dev/null 2>&1 || true
docker rm -f "$UPDATE_AGENT_NAME" >/dev/null 2>&1 || true
docker run -d \
  --restart unless-stopped \
  --user 0:0 \
  --network "$UPDATE_NETWORK" \
  --network-alias updater \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD:$PWD" \
  -v "$PWD/data:/app/storage" \
  -w "$PWD" \
  --env-file "$PWD/.env" \
  -e UPDATE_AGENT_NAME="$UPDATE_AGENT_NAME" \
  -e UPDATE_TARGET_NAME="$NAME" \
  -e UPDATE_NETWORK="$UPDATE_NETWORK" \
  -e UPDATE_DEPLOY_DIR="$PWD" \
  -e UPDATE_MODE=run \
  -e UPDATE_AGENT_PORT=8787 \
  -e UPDATE_AGENT_STATE_FILE=/app/storage/update-agent-state.json \
  --name "$UPDATE_AGENT_NAME" \
  "$UPDATER_IMAGE" node --no-warnings src/update-agent.js
AGENT_READY=false
ATTEMPT=0
while [ "$ATTEMPT" -lt 30 ]; do
  if docker exec "$UPDATE_AGENT_NAME" node --input-type=module -e "const r=await fetch('http://127.0.0.1:8787/healthz'); if (!r.ok) process.exit(1)" >/dev/null 2>&1; then
    AGENT_READY=true
    break
  fi
  ATTEMPT=$((ATTEMPT + 1))
  sleep 1
done
if [ "$AGENT_READY" != 'true' ]; then
  printf '%s\n' 'Update agent did not become healthy; existing backend was left running.' >&2
  exit 1
fi
docker rm -f "$NAME" >/dev/null 2>&1 || true
if [ "$(id -u)" -eq 0 ]; then
  chown -R 1000:1000 "$PWD/data"
  CONTAINER_USER='1000:1000'
else
  CONTAINER_USER="$(id -u):$(id -g)"
fi
docker run -itd \
  --restart unless-stopped \
  --init \
  --user "$CONTAINER_USER" \
  --env-file "$PWD/.env" \
  -e UPDATE_AGENT_URL=http://updater:8787 \
  -p "${PUBLIC_PORT}:8787" \
  --network "$UPDATE_NETWORK" \
  -v "$PWD/data:/app/storage" \
  -v "$PWD/content:/app/content:ro" \
  -v /etc/localtime:/etc/localtime:ro \
  -v /etc/timezone:/etc/timezone:ro \
  --name "$NAME" \
  "$IMAGE"
