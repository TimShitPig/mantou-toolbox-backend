#!/usr/bin/env sh
set -eu

RAW_BASE="${RAW_BASE:-https://gh-proxy.com/https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main}"
SOURCE_ARCHIVE_URL="${SOURCE_ARCHIVE_URL:-https://gh-proxy.com/https://github.com/TimShitPig/mantou-toolbox-backend/archive/refs/heads/main.tar.gz}"
PUBLIC_PORT="${PUBLIC_PORT:-8787}"
NODE_BASE_IMAGE="${NODE_BASE_IMAGE:-m.daocloud.io/docker.io/library/node:24-bookworm-slim}"
WECHAT_APP_ID="${WECHAT_APP_ID:-wx35d2ab50302daa5f}"
TEMP_DIR=''

cleanup() {
  if [ -n "$TEMP_DIR" ]; then rm -rf "$TEMP_DIR"; fi
}

trap cleanup EXIT HUP INT TERM

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  printf '%s\n' 'Docker Compose v2 is required.' >&2
  exit 1
fi

for command_name in curl tar; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Required command missing: %s\n' "$command_name" >&2
    exit 1
  fi
done

TEMP_DIR="$(mktemp -d "$PWD/.deploy-prepare.XXXXXX")"
curl -fsSL --connect-timeout 10 --max-time 60 "${RAW_BASE%/}/compose.yaml" -o "$TEMP_DIR/compose.yaml"
curl -fsSL --connect-timeout 10 --max-time 60 "${RAW_BASE%/}/deploy/update-source.sh" -o "$TEMP_DIR/update-source.sh"
curl -fsSL --connect-timeout 10 --max-time 120 "$SOURCE_ARCHIVE_URL" -o "$TEMP_DIR/source.tar.gz"

if ! grep -q '^services:' "$TEMP_DIR/compose.yaml"; then
  printf '%s\n' 'Downloaded Compose file is invalid.' >&2
  exit 1
fi
sh -n "$TEMP_DIR/update-source.sh"
mkdir -p "$TEMP_DIR/unpacked" "$TEMP_DIR/source"
tar -xzf "$TEMP_DIR/source.tar.gz" --strip-components=1 -C "$TEMP_DIR/unpacked"
for file in Dockerfile package.json server.js; do
  if [ ! -f "$TEMP_DIR/unpacked/$file" ]; then
    printf 'Source archive is missing %s.\n' "$file" >&2
    exit 1
  fi
done
cp "$TEMP_DIR/unpacked/Dockerfile" "$TEMP_DIR/unpacked/package.json" "$TEMP_DIR/unpacked/server.js" "$TEMP_DIR/source/"
cp -R "$TEMP_DIR/unpacked/src" "$TEMP_DIR/unpacked/public" "$TEMP_DIR/source/"
APP_BUILD_VERSION="v$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$TEMP_DIR/source/package.json" | head -n 1)"
if [ "$APP_BUILD_VERSION" = 'v' ]; then
  printf '%s\n' 'Could not read app version from source archive.' >&2
  exit 1
fi

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

set_env_value() {
  key="$1"
  value="$2"
  if grep -q "^${key}=" .env; then
    sed "s|^${key}=.*|${key}=${value}|" .env > "$TEMP_DIR/env"
    chmod 600 "$TEMP_DIR/env"
    mv "$TEMP_DIR/env" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
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
  cat > .env <<EOF
PUBLIC_PORT=${PUBLIC_PORT}
APP_BASE_URL=${APP_BASE_URL}
APP_SECRET=${APP_SECRET}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
APP_BUILD_VERSION=${APP_BUILD_VERSION}
NODE_BASE_IMAGE=${NODE_BASE_IMAGE}
DEPLOY_DIR=${PWD}
ALLOW_DEVELOPMENT_LOGIN=false
WECHAT_APP_ID=${WECHAT_APP_ID}
WECHAT_APP_SECRET=${WECHAT_APP_SECRET:-}
CONTENT_DIR=./content
EOF
  chmod 600 .env
  printf 'Admin password: %s\n' "$ADMIN_PASSWORD"
else
  ENV_TEMP="$TEMP_DIR/env"
  sed \
    -e 's#^UPDATE_DEPLOY_DIR=#DEPLOY_DIR=#' \
    -e '/^BACKEND_IMAGE=/d' \
    -e '/^IMAGE_REPOSITORY=/d' \
    -e '/^UPDATE_AGENT_SECRET=/d' \
    -e '/^UPDATE_AGENT_IMAGE=/d' \
    -e '/^UPDATE_IMAGE_REPOSITORY=/d' \
    -e '/^APP_BUILD_VERSION=/d' \
    -e '/^NODE_BASE_IMAGE=/d' \
    .env > "$ENV_TEMP"
  chmod 600 "$ENV_TEMP"
  mv "$ENV_TEMP" .env
  set_env_value APP_BUILD_VERSION "$APP_BUILD_VERSION"
  set_env_value NODE_BASE_IMAGE "$NODE_BASE_IMAGE"
  set_env_value DEPLOY_DIR "$PWD"
  printf '%s\n' 'Existing secrets and settings were preserved; deployment now builds from local source.'
fi

mv "$TEMP_DIR/compose.yaml" "$PWD/compose.yaml"
mv "$TEMP_DIR/update-source.sh" "$PWD/update-source.sh"
chmod 700 update-source.sh
if [ -d source ]; then mv source "$TEMP_DIR/source.previous"; fi
mv "$TEMP_DIR/source" "$PWD/source"

SAVED_APP_BASE_URL="$(sed -n 's/^APP_BASE_URL=//p' .env | tail -n 1)"
printf 'Source code: %s/source\n' "$PWD"
printf 'Admin UI: %s/admin\n' "${SAVED_APP_BASE_URL:-http://127.0.0.1:${PUBLIC_PORT}}"
printf 'Next: docker compose up -d --remove-orphans\n'
