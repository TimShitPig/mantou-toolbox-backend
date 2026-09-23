#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"
git pull --ff-only

if grep -Eq '^[[:space:]]*BACKEND_IMAGE[[:space:]]*=' .env; then
  docker compose pull
  docker compose up -d --no-build --remove-orphans
else
  docker compose up -d --build --remove-orphans
fi

docker compose ps
printf '%s\n' 'Backend updated.'
