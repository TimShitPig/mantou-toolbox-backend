#!/usr/bin/env sh
set -eu

RAW_BASE="${RAW_BASE:-https://gh-proxy.com/https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main}"
SOURCE_ARCHIVE_URL="${SOURCE_ARCHIVE_URL:-https://gh-proxy.com/https://github.com/TimShitPig/mantou-toolbox-backend/archive/refs/heads/main.tar.gz}"
PUBLIC_PORT="${PUBLIC_PORT:-8787}"
NODE_BASE_IMAGE="${NODE_BASE_IMAGE:-m.daocloud.io/docker.io/library/node:24-bookworm-slim}"
WECHAT_APP_ID="${WECHAT_APP_ID:-wx35d2ab50302daa5f}"
TEMP_DIR=''
DEPLOY_SUCCEEDED=0
PREVIOUS_IMAGE_ID=''
HAD_PREVIOUS_DEPLOYMENT=0

runtime_fingerprint() {
  fingerprint_dir="$1"
  {
    sha256sum \
      "$fingerprint_dir/source/Dockerfile" \
      "$fingerprint_dir/source/supervisor.js" \
      "$fingerprint_dir/compose.yaml" | awk '{print $1}'
    if [ -f "$fingerprint_dir/.env" ]; then
      grep -E '^(APT_MIRROR|NODE_BASE_IMAGE)=' "$fingerprint_dir/.env" || true
    fi
  } | sha256sum | awk '{print $1}'
}

cleanup() {
  if [ -n "$TEMP_DIR" ] && [ "$DEPLOY_SUCCEEDED" -ne 1 ]; then
    if [ -d "$TEMP_DIR/source.previous" ]; then
      rm -rf "$PWD/source"
      mv "$TEMP_DIR/source.previous" "$PWD/source"
    fi
    if [ -f "$TEMP_DIR/compose.previous" ]; then mv "$TEMP_DIR/compose.previous" "$PWD/compose.yaml"; fi
    if [ -f "$TEMP_DIR/env.previous" ]; then cp "$TEMP_DIR/env.previous" "$PWD/.env"; fi
    if [ -n "$PREVIOUS_IMAGE_ID" ]; then docker image tag "$PREVIOUS_IMAGE_ID" mantou-toolbox-backend:local >/dev/null 2>&1 || true; fi
    if [ -f "$TEMP_DIR/unit.previous" ]; then
      cp "$TEMP_DIR/unit.previous" /etc/systemd/system/mantou-toolbox-update-agent.service
      systemctl daemon-reload >/dev/null 2>&1 || true
    elif [ -f "$TEMP_DIR/mantou-toolbox-update-agent.service" ]; then
      systemctl stop mantou-toolbox-update-agent.service >/dev/null 2>&1 || true
      systemctl disable mantou-toolbox-update-agent.service >/dev/null 2>&1 || true
      rm -f /etc/systemd/system/mantou-toolbox-update-agent.service
      systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    if [ "$HAD_PREVIOUS_DEPLOYMENT" -eq 1 ]; then systemctl enable --now mantou-toolbox-update-agent.service >/dev/null 2>&1 || true; fi
    if [ "$HAD_PREVIOUS_DEPLOYMENT" -eq 1 ] && [ -f "$PWD/compose.yaml" ]; then
      docker compose up -d --no-build --remove-orphans >/dev/null 2>&1 || true
    fi
  fi
  if [ -n "$TEMP_DIR" ]; then rm -rf "$TEMP_DIR"; fi
}

trap cleanup EXIT HUP INT TERM

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  printf '%s\n' 'Docker Compose v2 is required.' >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ] || ! command -v systemctl >/dev/null 2>&1; then
  printf '%s\n' 'Root access and systemd are required for one-click Docker updates.' >&2
  exit 1
fi

for command_name in curl tar chown; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Required command missing: %s\n' "$command_name" >&2
    exit 1
  fi
done

TEMP_DIR="$(mktemp -d "$PWD/.deploy-prepare.XXXXXX")"
curl -fsSL --connect-timeout 10 --max-time 60 "${RAW_BASE%/}/compose.yaml" -o "$TEMP_DIR/compose.yaml"
curl -fsSL --connect-timeout 10 --max-time 120 "$SOURCE_ARCHIVE_URL" -o "$TEMP_DIR/source.tar.gz"

if ! grep -q '^services:' "$TEMP_DIR/compose.yaml"; then
  printf '%s\n' 'Downloaded Compose file is invalid.' >&2
  exit 1
fi
mkdir -p "$TEMP_DIR/unpacked" "$TEMP_DIR/source"
if [ -f "$PWD/compose.yaml" ] && [ -d "$PWD/source" ]; then
  HAD_PREVIOUS_DEPLOYMENT=1
  PREVIOUS_IMAGE_ID="$(docker compose images -q backend 2>/dev/null | head -n 1 || true)"
  cp "$PWD/compose.yaml" "$TEMP_DIR/compose.previous"
  if [ -f "$PWD/.env" ]; then cp "$PWD/.env" "$TEMP_DIR/env.previous"; fi
fi
tar -xzf "$TEMP_DIR/source.tar.gz" --strip-components=1 -C "$TEMP_DIR/unpacked"
for file in Dockerfile 宿主机更新代理.sh package.json server.js supervisor.js; do
  if [ ! -f "$TEMP_DIR/unpacked/$file" ]; then
    printf 'Source archive is missing %s.\n' "$file" >&2
    exit 1
  fi
done
cp "$TEMP_DIR/unpacked/Dockerfile" "$TEMP_DIR/unpacked/宿主机更新代理.sh" "$TEMP_DIR/unpacked/package.json" "$TEMP_DIR/unpacked/server.js" "$TEMP_DIR/unpacked/supervisor.js" "$TEMP_DIR/source/"
cp -R "$TEMP_DIR/unpacked/src" "$TEMP_DIR/unpacked/public" "$TEMP_DIR/source/"

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

mkdir -p content update-control
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
NODE_BASE_IMAGE=${NODE_BASE_IMAGE}
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
    -e '/^UPDATE_DEPLOY_DIR=/d' \
    -e '/^DEPLOY_DIR=/d' \
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
  set_env_value NODE_BASE_IMAGE "$NODE_BASE_IMAGE"
  printf '%s\n' 'Existing secrets and settings were preserved; deployment now builds from local source.'
fi

mv "$TEMP_DIR/compose.yaml" "$PWD/compose.yaml"
rm -f "$PWD/update-source.sh"
if [ "$HAD_PREVIOUS_DEPLOYMENT" -eq 1 ]; then
  systemctl stop mantou-toolbox-update-agent.service >/dev/null 2>&1 || true
  mv source "$TEMP_DIR/source.previous"
fi
mv "$TEMP_DIR/source" "$PWD/source"
chown -R 1000:1000 "$PWD/source"
chown -R 1000:1000 "$PWD/update-control"

UNIT_DEPLOY_DIR="$(printf '%s' "$PWD" | sed 's/%/%%/g')"
if [ -f /etc/systemd/system/mantou-toolbox-update-agent.service ]; then
  cp /etc/systemd/system/mantou-toolbox-update-agent.service "$TEMP_DIR/unit.previous"
fi
cat > "$TEMP_DIR/mantou-toolbox-update-agent.service" <<EOF
[Unit]
Description=Mantou Toolbox Docker update agent
After=docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=$UNIT_DEPLOY_DIR
ExecStart=/bin/sh "$UNIT_DEPLOY_DIR/source/宿主机更新代理.sh" --watch "$UNIT_DEPLOY_DIR"
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
systemd-analyze verify "$TEMP_DIR/mantou-toolbox-update-agent.service"
systemctl stop mantou-toolbox-update-agent.service >/dev/null 2>&1 || true
systemctl disable mantou-toolbox-update-agent.service >/dev/null 2>&1 || true
install -m 0644 "$TEMP_DIR/mantou-toolbox-update-agent.service" /etc/systemd/system/mantou-toolbox-update-agent.service
systemctl daemon-reload
systemctl enable --now mantou-toolbox-update-agent.service

printf '%s\n' 'Building and starting the single backend container...'
RUNTIME_FINGERPRINT="$(runtime_fingerprint "$PWD")"
MANTOU_RUNTIME_FINGERPRINT="$RUNTIME_FINGERPRINT" docker compose build \
  --build-arg "MANTOU_RUNTIME_FINGERPRINT=$RUNTIME_FINGERPRINT" backend
docker compose up -d --no-build --remove-orphans

NEW_CONTAINER_ID=''
HEALTHY=0
ATTEMPT=0
while [ "$ATTEMPT" -lt 120 ]; do
  NEW_CONTAINER_ID="$(docker compose ps -q backend 2>/dev/null | head -n 1 || true)"
  if [ -n "$NEW_CONTAINER_ID" ]; then
    CONTAINER_STATE="$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$NEW_CONTAINER_ID" 2>/dev/null || true)"
    case "$CONTAINER_STATE" in
      'running healthy') HEALTHY=1; break ;;
      *unhealthy*|exited\ *|dead\ *) printf 'Backend failed health check: %s\n' "$CONTAINER_STATE" >&2; exit 1 ;;
    esac
  fi
  ATTEMPT=$((ATTEMPT + 1))
  sleep 2
done
if [ "$HEALTHY" -ne 1 ]; then
  printf '%s\n' 'Timed out waiting for backend health check.' >&2
  exit 1
fi

NEW_IMAGE_ID="$(docker inspect --format '{{.Image}}' "$NEW_CONTAINER_ID")"
if [ -n "$PREVIOUS_IMAGE_ID" ] && [ "$PREVIOUS_IMAGE_ID" != "$NEW_IMAGE_ID" ]; then
  docker image rm "$PREVIOUS_IMAGE_ID" >/dev/null 2>&1 || printf '%s\n' 'Previous image is still used by another container; it was retained.' >&2
fi
docker image prune -f --filter 'label=com.timshitpig.mantou-toolbox.managed=true' >/dev/null 2>&1 || printf '%s\n' 'Warning: Mantou Toolbox dangling-image cleanup failed.' >&2
RUNTIME_FINGERPRINT="$(runtime_fingerprint "$PWD")"
RUNTIME_FINGERPRINT_TEMP="$PWD/update-control/runtime-fingerprint.$$"
printf '%s\n%s\n' "$NEW_IMAGE_ID" "$RUNTIME_FINGERPRINT" > "$RUNTIME_FINGERPRINT_TEMP"
mv -f "$RUNTIME_FINGERPRINT_TEMP" "$PWD/update-control/runtime-fingerprint"
DEPLOY_SUCCEEDED=1

SAVED_APP_BASE_URL="$(sed -n 's/^APP_BASE_URL=//p' .env | tail -n 1)"
printf 'Source code: %s/source\n' "$PWD"
printf 'Admin UI: %s/admin\n' "${SAVED_APP_BASE_URL:-http://127.0.0.1:${PUBLIC_PORT}}"
printf '%s\n' 'Deployment complete. Future source updates rebuild and clean the previous local image from the admin Update button.'
