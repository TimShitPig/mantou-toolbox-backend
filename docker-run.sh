#!/usr/bin/env sh
set -eu

IMAGE="${IMAGE:-ghcr.io/timshitpig/mantou-toolbox-backend:latest}"
NAME="${NAME:-mantou-toolbox}"
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
  cat > "$PWD/.env" <<EOF
NODE_ENV=production
HOST=0.0.0.0
PORT=8787
PUBLIC_PORT=${PUBLIC_PORT}
APP_BASE_URL=${APP_BASE_URL}
APP_SECRET=${APP_SECRET}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
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
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -itd \
  --restart unless-stopped \
  --env-file "$PWD/.env" \
  -p "${PUBLIC_PORT}:8787" \
  -v "$PWD/data:/app/storage" \
  -v "$PWD/content:/app/content:ro" \
  -v /etc/localtime:/etc/localtime:ro \
  -v /etc/timezone:/etc/timezone:ro \
  --name "$NAME" \
  "$IMAGE"
