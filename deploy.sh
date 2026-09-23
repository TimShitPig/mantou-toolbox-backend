#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.docker.example .env
  printf '%s\n' 'Created .env from .env.docker.example. Review APP_SECRET before production use.'
fi

if grep -Eq '^[[:space:]]*BACKEND_IMAGE[[:space:]]*=' .env; then
  docker compose pull
  docker compose up -d --no-build --remove-orphans
else
  docker compose up -d --build --remove-orphans
fi

docker compose ps
printf '%s\n' 'Backend URL: http://127.0.0.1:8787'
