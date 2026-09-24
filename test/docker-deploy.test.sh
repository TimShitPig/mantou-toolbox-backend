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
  cp "$fixture/Dockerfile" "$fixture/package.json" "$fixture/server.js" "$fixture/supervisor.js" "$archive_root/source-root/"
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
  *archive/refs/heads/main.tar.gz*) archive_source "$SOURCE_FIXTURE" "$output" ;;
  *) printf '%s\n' '198.51.100.27' ;;
esac
EOF

cat > "$TEMP_ROOT/bin/docker" <<'EOF'
#!/usr/bin/env sh
if [ "$1" = 'compose' ] && [ "$2" = 'version' ]; then
  printf '%s\n' 'Docker Compose version v2.30.0'
  exit 0
fi
exit 1
EOF

chmod +x "$TEMP_ROOT/bin/curl" "$TEMP_ROOT/bin/docker"
cat > "$TEMP_ROOT/bin/chown" <<'EOF'
#!/usr/bin/env sh
exit 0
EOF
chmod +x "$TEMP_ROOT/bin/chown"

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
      RAW_BASE=https://gh-proxy.com/https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main \
      SOURCE_ARCHIVE_URL=https://gh-proxy.com/https://github.com/TimShitPig/mantou-toolbox-backend/archive/refs/heads/main.tar.gz \
      sh "$ROOT/deploy/docker-deploy.sh"
  )
}

run_prepare
ENV_FILE="$TEMP_ROOT/deploy/.env"
[ -s "$TEMP_ROOT/deploy/compose.yaml" ]
[ -f "$TEMP_ROOT/deploy/source/src/app.js" ]
[ -f "$TEMP_ROOT/deploy/source/public/admin.html" ]
[ -f "$TEMP_ROOT/deploy/source/supervisor.js" ]
[ ! -e "$TEMP_ROOT/deploy/update-source.sh" ]
[ ! -e "$TEMP_ROOT/deploy/source/test" ]
grep -Fqx 'PUBLIC_PORT=8787' "$ENV_FILE"
grep -Fqx 'APP_BASE_URL=http://198.51.100.27:8787' "$ENV_FILE"
grep -Fqx 'NODE_BASE_IMAGE=m.daocloud.io/docker.io/library/node:24-bookworm-slim' "$ENV_FILE"
! grep -q '^DEPLOY_DIR=' "$ENV_FILE"
! grep -q '^APP_BUILD_VERSION=' "$ENV_FILE"
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
printf '%s\n' \
  'DEPLOY_DIR=/root/mantou-toolbox-deploy' \
  'UPDATE_DEPLOY_DIR=/root/mantou-toolbox-deploy' \
  'BACKEND_IMAGE=ghcr.io/timshitpig/mantou-toolbox-backend:latest' \
  'IMAGE_REPOSITORY=ghcr.io/timshitpig/mantou-toolbox-backend' \
  'UPDATE_AGENT_SECRET=obsolete-secret' \
  'UPDATE_AGENT_IMAGE=ghcr.io/timshitpig/mantou-toolbox-backend:latest' \
  'UPDATE_IMAGE_REPOSITORY=ghcr.io/timshitpig/mantou-toolbox-backend' >> "$ENV_FILE"
run_prepare

! grep -q '^DEPLOY_DIR=' "$ENV_FILE"
[ "$(sed -n 's/^APP_SECRET=//p' "$ENV_FILE")" = "$APP_SECRET_BEFORE" ]
[ "$(sed -n 's/^ADMIN_PASSWORD=//p' "$ENV_FILE")" = "$ADMIN_PASSWORD_BEFORE" ]
! grep -q '^BACKEND_IMAGE=' "$ENV_FILE"
! grep -q '^IMAGE_REPOSITORY=' "$ENV_FILE"
! grep -q '^UPDATE_AGENT_' "$ENV_FILE"
! grep -q '^UPDATE_IMAGE_REPOSITORY=' "$ENV_FILE"
! grep -q '^UPDATE_DEPLOY_DIR=' "$ENV_FILE"

printf '%s\n' 'source-based single-container deployment preparation passed'
