#!/usr/bin/env sh
set -eu

TARGET_VERSION="${1:-}"
if ! printf '%s' "$TARGET_VERSION" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
  printf 'Usage: %s vX.Y.Z\n' "$0" >&2
  exit 2
fi

SOURCE_ARCHIVE_BASE="${SOURCE_ARCHIVE_BASE:-https://gh-proxy.com/https://github.com/TimShitPig/mantou-toolbox-backend/archive/refs/tags}"
DEPLOY_DIR="${DEPLOY_DIR:-$PWD}"
cd "$DEPLOY_DIR"

if [ ! -f compose.yaml ] || [ ! -f .env ] || [ ! -d source ]; then
  printf '%s\n' 'Run this script from the prepared deployment directory.' >&2
  exit 1
fi

WORK_DIR="$(mktemp -d "$PWD/.source-update.XXXXXX")"
SOURCE_BACKUP="$PWD/.source.previous.$$"
OLD_VERSION="$(sed -n 's/^APP_BUILD_VERSION=//p' .env | tail -n 1)"
OLD_IMAGE_ID="$(docker inspect --format '{{.Image}}' mantou-toolbox 2>/dev/null || true)"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT HUP INT TERM

curl -fsSL --connect-timeout 5 --max-time 120 \
  "${SOURCE_ARCHIVE_BASE%/}/${TARGET_VERSION}.tar.gz" \
  -o "$WORK_DIR/source.tar.gz"
mkdir -p "$WORK_DIR/unpacked" "$WORK_DIR/source"
tar -xzf "$WORK_DIR/source.tar.gz" --strip-components=1 -C "$WORK_DIR/unpacked"

SOURCE_PACKAGE_VERSION="v$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$WORK_DIR/unpacked/package.json" | head -n 1)"
if [ "$SOURCE_PACKAGE_VERSION" != "$TARGET_VERSION" ]; then
  printf 'Tag version %s does not match package version %s.\n' "$TARGET_VERSION" "$SOURCE_PACKAGE_VERSION" >&2
  exit 1
fi

for file in Dockerfile package.json server.js; do
  if [ ! -f "$WORK_DIR/unpacked/$file" ]; then
    printf 'Source archive is missing %s.\n' "$file" >&2
    exit 1
  fi
done
cp "$WORK_DIR/unpacked/Dockerfile" "$WORK_DIR/unpacked/package.json" "$WORK_DIR/unpacked/server.js" "$WORK_DIR/source/"
cp -R "$WORK_DIR/unpacked/src" "$WORK_DIR/unpacked/public" "$WORK_DIR/source/"

set_app_version() {
  if grep -q '^APP_BUILD_VERSION=' .env; then
    sed "s|^APP_BUILD_VERSION=.*|APP_BUILD_VERSION=$1|" .env > "$WORK_DIR/.env"
    chmod 600 "$WORK_DIR/.env"
    mv "$WORK_DIR/.env" .env
  else
    printf 'APP_BUILD_VERSION=%s\n' "$1" >> .env
  fi
}

mv source "$SOURCE_BACKUP"
mv "$WORK_DIR/source" source
set_app_version "$TARGET_VERSION"

if docker compose up -d --no-deps --build --wait backend; then
  NEW_IMAGE_ID="$(docker inspect --format '{{.Image}}' mantou-toolbox 2>/dev/null || true)"
  if [ -n "$OLD_IMAGE_ID" ] && [ "$OLD_IMAGE_ID" != "$NEW_IMAGE_ID" ]; then
    docker image rm "$OLD_IMAGE_ID" >/dev/null 2>&1 || true
  fi
  rm -rf "$SOURCE_BACKUP"
  printf 'Updated to %s and passed the health check.\n' "$TARGET_VERSION"
  exit 0
fi

printf 'Version %s failed its health check; restoring %s.\n' "$TARGET_VERSION" "$OLD_VERSION" >&2
FAILED_IMAGE_ID="$(docker inspect --format '{{.Image}}' mantou-toolbox 2>/dev/null || true)"
rm -rf source
mv "$SOURCE_BACKUP" source
if [ -n "$OLD_VERSION" ]; then set_app_version "$OLD_VERSION"; fi
if docker compose up -d --no-deps --build --wait backend; then
  RESTORED_IMAGE_ID="$(docker inspect --format '{{.Image}}' mantou-toolbox 2>/dev/null || true)"
  if [ -n "$FAILED_IMAGE_ID" ] && [ "$FAILED_IMAGE_ID" != "$RESTORED_IMAGE_ID" ]; then
    docker image rm "$FAILED_IMAGE_ID" >/dev/null 2>&1 || true
  fi
  printf 'Restored %s.\n' "$OLD_VERSION" >&2
  exit 1
fi

printf '%s\n' 'The previous source is restored in source/, but its container did not pass the health check.' >&2
exit 2
