#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
mkdir -p "$TEMP_ROOT/bin" "$TEMP_ROOT/deploy"

cat > "$TEMP_ROOT/bin/curl" <<'EOF'
#!/usr/bin/env sh
request="$*"
archive_source() {
  fixture="$1"
  output="$2"
  archive_root="$output.root"
  mkdir -p "$archive_root/source-root"
  cp "$fixture/Dockerfile" "$fixture/package.json" "$fixture/server.js" "$archive_root/source-root/"
  cp -R "$fixture/src" "$fixture/public" "$archive_root/source-root/"
  tar -czf "$output" -C "$archive_root" source-root
  rm -rf "$archive_root"
}

output=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-o' ]; then shift; output="$1"; fi
  shift
done
case "$request" in
  *compose.yaml*) cp "$COMPOSE_FIXTURE" "$output" ;;
  *deploy/update-source.sh*) cp "$UPDATE_FIXTURE" "$output" ;;
  *archive/refs/heads/main.tar.gz*) archive_source "$SOURCE_FIXTURE" "$output" ;;
  *archive/refs/tags/v0.0.3.tar.gz*) archive_source "$SOURCE_FIXTURE_V2" "$output" ;;
  *) printf '%s\n' '198.51.100.27' ;;
esac
EOF

cat > "$TEMP_ROOT/bin/docker" <<'EOF'
#!/usr/bin/env sh
if [ "$1" = 'compose' ] && [ "$2" = 'version' ]; then
  printf '%s\n' 'Docker Compose version v2.30.0'
  exit 0
fi
if [ "$1" = 'inspect' ]; then
  printf '%s\n' 'sha256:old-image'
  exit 0
fi
if [ "$1" = 'compose' ] && [ "$2" = 'up' ]; then
  calls="${COMPOSE_CALLS:-/dev/null}"
  count=0
  if [ -f "$calls" ]; then count="$(wc -l < "$calls")"; fi
  printf '%s\n' "$*" >> "$calls"
  if [ "${MOCK_FAIL_FIRST:-false}" = 'true' ] && [ "$count" -eq 0 ]; then exit 1; fi
  exit 0
fi
if [ "$1" = 'image' ] && [ "$2" = 'rm' ]; then exit 0; fi
exit 1
EOF

chmod +x "$TEMP_ROOT/bin/curl" "$TEMP_ROOT/bin/docker"

assert_private_file() {
  case "${OSTYPE:-}" in
    msys*|cygwin*) return 0 ;;
  esac
  [ "$(stat -c '%a' "$1")" = '600' ]
}

run_prepare() {
  (
    cd "$TEMP_ROOT/deploy"
    PATH="$TEMP_ROOT/bin:$PATH" \
      COMPOSE_FIXTURE="$ROOT/compose.yaml" \
      SOURCE_FIXTURE="$ROOT" \
      UPDATE_FIXTURE="$ROOT/deploy/update-source.sh" \
      RAW_BASE=https://gh-proxy.com/https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main \
      SOURCE_ARCHIVE_URL=https://gh-proxy.com/https://github.com/TimShitPig/mantou-toolbox-backend/archive/refs/heads/main.tar.gz \
      sh "$ROOT/deploy/docker-deploy.sh"
  )
}

run_prepare
ENV_FILE="$TEMP_ROOT/deploy/.env"
[ -s "$TEMP_ROOT/deploy/compose.yaml" ]
[ -x "$TEMP_ROOT/deploy/update-source.sh" ]
[ -f "$TEMP_ROOT/deploy/source/src/app.js" ]
[ -f "$TEMP_ROOT/deploy/source/public/admin.html" ]
[ ! -e "$TEMP_ROOT/deploy/source/test" ]
grep -Fqx 'PUBLIC_PORT=8787' "$ENV_FILE"
grep -Fqx 'APP_BASE_URL=http://198.51.100.27:8787' "$ENV_FILE"
grep -Fqx 'APP_BUILD_VERSION=v0.0.2' "$ENV_FILE"
grep -Fqx 'NODE_BASE_IMAGE=m.daocloud.io/docker.io/library/node:24-bookworm-slim' "$ENV_FILE"
grep -Fqx "DEPLOY_DIR=$TEMP_ROOT/deploy" "$ENV_FILE"
[ "$(sed -n 's/^APP_SECRET=//p' "$ENV_FILE" | wc -c | tr -d ' ')" -eq 65 ]
[ "$(sed -n 's/^ADMIN_PASSWORD=//p' "$ENV_FILE" | wc -c | tr -d ' ')" -eq 65 ]
[ "$(grep -c '^  backend:' "$TEMP_ROOT/deploy/compose.yaml")" -eq 1 ]
grep -Fq 'context: ./source' "$TEMP_ROOT/deploy/compose.yaml"
grep -Fq 'pull_policy: build' "$TEMP_ROOT/deploy/compose.yaml"
! grep -q '^  updater:' "$TEMP_ROOT/deploy/compose.yaml"
! grep -q '/var/run/docker.sock' "$TEMP_ROOT/deploy/compose.yaml"
assert_private_file "$ENV_FILE"

APP_SECRET_BEFORE="$(sed -n 's/^APP_SECRET=//p' "$ENV_FILE")"
ADMIN_PASSWORD_BEFORE="$(sed -n 's/^ADMIN_PASSWORD=//p' "$ENV_FILE")"
sed -i 's/^DEPLOY_DIR=/UPDATE_DEPLOY_DIR=/' "$ENV_FILE"
printf '%s\n' \
  'BACKEND_IMAGE=ghcr.io/timshitpig/mantou-toolbox-backend:latest' \
  'IMAGE_REPOSITORY=ghcr.io/timshitpig/mantou-toolbox-backend' \
  'UPDATE_AGENT_SECRET=obsolete-secret' \
  'UPDATE_AGENT_IMAGE=ghcr.io/timshitpig/mantou-toolbox-backend:latest' \
  'UPDATE_IMAGE_REPOSITORY=ghcr.io/timshitpig/mantou-toolbox-backend' >> "$ENV_FILE"
run_prepare

grep -Fqx 'APP_BUILD_VERSION=v0.0.2' "$ENV_FILE"
grep -Fqx "DEPLOY_DIR=$TEMP_ROOT/deploy" "$ENV_FILE"
[ "$(sed -n 's/^APP_SECRET=//p' "$ENV_FILE")" = "$APP_SECRET_BEFORE" ]
[ "$(sed -n 's/^ADMIN_PASSWORD=//p' "$ENV_FILE")" = "$ADMIN_PASSWORD_BEFORE" ]
! grep -q '^BACKEND_IMAGE=' "$ENV_FILE"
! grep -q '^IMAGE_REPOSITORY=' "$ENV_FILE"
! grep -q '^UPDATE_AGENT_' "$ENV_FILE"
! grep -q '^UPDATE_IMAGE_REPOSITORY=' "$ENV_FILE"
! grep -q '^UPDATE_DEPLOY_DIR=' "$ENV_FILE"

cp -R "$TEMP_ROOT/deploy/source" "$TEMP_ROOT/source-v2"
sed -i 's/"version": "0.0.2"/"version": "0.0.3"/' "$TEMP_ROOT/source-v2/package.json"
if (
  cd "$TEMP_ROOT/deploy"
  PATH="$TEMP_ROOT/bin:$PATH" \
    SOURCE_FIXTURE_V2="$TEMP_ROOT/source-v2" \
    COMPOSE_CALLS="$TEMP_ROOT/compose-calls" \
    MOCK_FAIL_FIRST=true \
    sh ./update-source.sh v0.0.3
); then
  printf '%s\n' 'failed update unexpectedly reported success' >&2
  exit 1
fi
grep -Fqx 'APP_BUILD_VERSION=v0.0.2' "$ENV_FILE"
[ -f "$TEMP_ROOT/deploy/source/Dockerfile" ]
grep -Fq 'version": "0.0.2"' "$TEMP_ROOT/deploy/source/package.json"
[ "$(wc -l < "$TEMP_ROOT/compose-calls")" -eq 2 ]

printf '%s\n' 'source-based single-container deployment preparation passed'
