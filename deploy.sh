#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"
UPDATE_DEPLOY_DIR="${UPDATE_DEPLOY_DIR:-$PWD}"
export UPDATE_DEPLOY_DIR

if command -v git >/dev/null 2>&1 \
  && [ "$(git status --porcelain 2>/dev/null || printf 'unknown')" = '' ]; then
  APP_BUILD_REVISION="$(git rev-parse HEAD 2>/dev/null || printf 'unknown')"
  export APP_BUILD_REVISION
fi

if [ ! -f .env ]; then
  cp .env.docker.example .env
  printf '%s\n' 'Created .env from .env.docker.example. Review APP_SECRET before production use.'
fi

SAVED_UPDATE_AGENT_SECRET="$(sed -n 's/^UPDATE_AGENT_SECRET=//p' .env | tail -n 1)"
if [ -z "$SAVED_UPDATE_AGENT_SECRET" ]; then
  if command -v openssl >/dev/null 2>&1; then
    UPDATE_AGENT_SECRET="$(openssl rand -hex 32)"
  else
    UPDATE_AGENT_SECRET="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
  fi
  if grep -q '^UPDATE_AGENT_SECRET=' .env; then
    TEMP_ENV="$(mktemp)"
    sed "s/^UPDATE_AGENT_SECRET=.*/UPDATE_AGENT_SECRET=${UPDATE_AGENT_SECRET}/" .env > "$TEMP_ENV"
    cat "$TEMP_ENV" > .env
    rm -f "$TEMP_ENV"
  else
    printf '\nUPDATE_AGENT_SECRET=%s\n' "$UPDATE_AGENT_SECRET" >> .env
  fi
  chmod 600 .env
fi

APP_BUILD_VERSION="v$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' package.json | head -n 1)"
export APP_BUILD_VERSION

if grep -Eq '^[[:space:]]*BACKEND_IMAGE[[:space:]]*=' .env; then
  docker compose pull
  docker compose up -d --no-build --remove-orphans
else
  docker compose up -d --build --remove-orphans
fi

docker compose ps
printf '%s\n' 'Backend URL: http://127.0.0.1:8787'
