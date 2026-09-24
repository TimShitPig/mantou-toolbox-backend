#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

mkdir -p "$TEMP_ROOT/bin" "$TEMP_ROOT/deploy"

cat > "$TEMP_ROOT/bin/curl" <<'EOF'
#!/usr/bin/env sh
case "$*" in
  *raw.githubusercontent.com*compose.yaml*)
    output=''
    while [ "$#" -gt 0 ]; do
      if [ "$1" = '-o' ]; then
        shift
        output="$1"
      fi
      shift
    done
    cp "$COMPOSE_FIXTURE" "$output"
    ;;
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

assert_private_file() {
  case "${OSTYPE:-}" in
    msys*|cygwin*) return 0 ;;
  esac
  [ "$(stat -c '%a' "$1")" = '600' ]
}

(
  cd "$TEMP_ROOT/deploy"
  PATH="$TEMP_ROOT/bin:$PATH" \
    COMPOSE_FIXTURE="$ROOT/compose.yaml" \
    RAW_BASE=https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main \
    sh "$ROOT/deploy/docker-deploy.sh"
)

ENV_FILE="$TEMP_ROOT/deploy/.env"
[ -s "$TEMP_ROOT/deploy/compose.yaml" ]
[ -d "$TEMP_ROOT/deploy/content" ]
grep -Fqx 'PUBLIC_PORT=8787' "$ENV_FILE"
grep -Fqx 'APP_BASE_URL=http://198.51.100.27:8787' "$ENV_FILE"
grep -Fqx 'BACKEND_IMAGE=ghcr.nju.edu.cn/timshitpig/mantou-toolbox-backend:latest' "$ENV_FILE"
grep -Fqx 'IMAGE_REPOSITORY=ghcr.nju.edu.cn/timshitpig/mantou-toolbox-backend' "$ENV_FILE"
grep -Fqx "DEPLOY_DIR=$TEMP_ROOT/deploy" "$ENV_FILE"
[ "$(sed -n 's/^APP_SECRET=//p' "$ENV_FILE" | wc -c | tr -d ' ')" -eq 65 ]
[ "$(sed -n 's/^ADMIN_PASSWORD=//p' "$ENV_FILE" | wc -c | tr -d ' ')" -eq 65 ]
[ "$(grep -c '^  backend:' "$TEMP_ROOT/deploy/compose.yaml")" -eq 1 ]
! grep -q '^  updater:' "$TEMP_ROOT/deploy/compose.yaml"
! grep -q '/var/run/docker.sock' "$TEMP_ROOT/deploy/compose.yaml"
assert_private_file "$ENV_FILE"

APP_SECRET_BEFORE="$(sed -n 's/^APP_SECRET=//p' "$ENV_FILE")"
ADMIN_PASSWORD_BEFORE="$(sed -n 's/^ADMIN_PASSWORD=//p' "$ENV_FILE")"
sed -i 's#ghcr.nju.edu.cn/timshitpig/mantou-toolbox-backend#ghcr.io/timshitpig/mantou-toolbox-backend#g' "$ENV_FILE"
sed -i 's/^DEPLOY_DIR=/UPDATE_DEPLOY_DIR=/' "$ENV_FILE"
printf '%s\n' \
  'UPDATE_AGENT_SECRET=obsolete-secret' \
  'UPDATE_AGENT_IMAGE=ghcr.io/timshitpig/mantou-toolbox-backend:latest' \
  'UPDATE_IMAGE_REPOSITORY=ghcr.io/timshitpig/mantou-toolbox-backend' >> "$ENV_FILE"
(
  cd "$TEMP_ROOT/deploy"
  PATH="$TEMP_ROOT/bin:$PATH" \
    COMPOSE_FIXTURE="$ROOT/compose.yaml" \
    sh "$ROOT/deploy/docker-deploy.sh"
)
grep -Fqx 'BACKEND_IMAGE=ghcr.nju.edu.cn/timshitpig/mantou-toolbox-backend:latest' "$ENV_FILE"
grep -Fqx 'IMAGE_REPOSITORY=ghcr.nju.edu.cn/timshitpig/mantou-toolbox-backend' "$ENV_FILE"
grep -Fqx "DEPLOY_DIR=$TEMP_ROOT/deploy" "$ENV_FILE"
[ "$(sed -n 's/^APP_SECRET=//p' "$ENV_FILE")" = "$APP_SECRET_BEFORE" ]
[ "$(sed -n 's/^ADMIN_PASSWORD=//p' "$ENV_FILE")" = "$ADMIN_PASSWORD_BEFORE" ]
! grep -q '^UPDATE_AGENT_' "$ENV_FILE"
! grep -q '^UPDATE_IMAGE_REPOSITORY=' "$ENV_FILE"
! grep -q '^UPDATE_DEPLOY_DIR=' "$ENV_FILE"

printf '%s\n' 'single-container deployment preparation passed'
