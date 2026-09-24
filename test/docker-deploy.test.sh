#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

mkdir -p "$TEMP_ROOT/bin" "$TEMP_ROOT/server"

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
    if [ -n "${COMPOSE_FIXTURE:-}" ]; then
      cp "$COMPOSE_FIXTURE" "$output"
    else
      printf '%s\n' 'name: mantou-toolbox' 'services:' '  backend:' '    image: test/image:latest' > "$output"
    fi
    ;;
  *api.github.com*)
    printf '%s\n' "$*" >> "${CURL_CALLS:-/dev/null}"
    printf '%s\n' '{"sha": "0123456789abcdef0123456789abcdef01234567"}'
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
if [ "$1" = 'inspect' ]; then
  case "$*" in
    *'.State.Running'*)
      printf '%s\n' "${MOCK_AGENT_RUNNING:-false}"
      exit 0
      ;;
  esac
  printf '%s\n' "${MOCK_CURRENT_REVISION:-}"
  exit 0
fi
printf '%s\n' "$*" >> "$DOCKER_CALLS"
EOF

chmod +x "$TEMP_ROOT/bin/curl" "$TEMP_ROOT/bin/docker"

assert_private_file() {
  case "${OSTYPE:-}" in
    msys*|cygwin*) return 0 ;;
  esac
  [ "$(stat -c '%a' "$1")" = '600' ]
}

(
  cd "$TEMP_ROOT/server"
  PATH="$TEMP_ROOT/bin:$PATH" \
    DOCKER_CALLS="$TEMP_ROOT/docker-calls" \
    PUBLIC_PORT=6185 \
    sh "$ROOT/docker-run.sh"
)

ENV_FILE="$TEMP_ROOT/server/.env"
grep -Fqx 'APP_BASE_URL=http://198.51.100.27:6185' "$ENV_FILE"
grep -Fqx 'PUBLIC_PORT=6185' "$ENV_FILE"
grep -Fqx 'WECHAT_APP_ID=wx35d2ab50302daa5f' "$ENV_FILE"
grep -Fqx 'WECHAT_APP_SECRET=' "$ENV_FILE"
grep -Fqx 'ALLOW_DEVELOPMENT_LOGIN=false' "$ENV_FILE"
assert_private_file "$ENV_FILE"
SECRET="$(sed -n 's/^APP_SECRET=//p' "$ENV_FILE")"
[ "${#SECRET}" -eq 64 ]
ADMIN_PASSWORD="$(sed -n 's/^ADMIN_PASSWORD=//p' "$ENV_FILE")"
[ "${#ADMIN_PASSWORD}" -eq 64 ]
UPDATE_AGENT_SECRET="$(sed -n 's/^UPDATE_AGENT_SECRET=//p' "$ENV_FILE")"
[ "${#UPDATE_AGENT_SECRET}" -eq 64 ]
grep -Fq -- '-p 6185:8787' "$TEMP_ROOT/docker-calls"
EXPECTED_UID="$(id -u)"
EXPECTED_GID="$(id -g)"
if [ "$EXPECTED_UID" -eq 0 ]; then
  EXPECTED_UID=1000
  EXPECTED_GID=1000
fi
grep -Fq -- "--user ${EXPECTED_UID}:${EXPECTED_GID}" "$TEMP_ROOT/docker-calls"
grep -Fq -- '--env-file '"$ENV_FILE" "$TEMP_ROOT/docker-calls"

BEFORE="$(sha256sum "$ENV_FILE" | cut -d' ' -f1)"
(
  cd "$TEMP_ROOT/server"
    PATH="$TEMP_ROOT/bin:$PATH" \
    DOCKER_CALLS="$TEMP_ROOT/docker-calls-update" \
    MOCK_CURRENT_REVISION=0123456789abcdef0123456789abcdef01234567 \
    MOCK_AGENT_RUNNING=true \
    sh "$ROOT/docker-update.sh"
)
AFTER="$(sha256sum "$ENV_FILE" | cut -d' ' -f1)"
[ "$BEFORE" = "$AFTER" ]
[ ! -s "$TEMP_ROOT/docker-calls-update" ]

(
  cd "$TEMP_ROOT/server"
  PATH="$TEMP_ROOT/bin:$PATH" \
    DOCKER_CALLS="$TEMP_ROOT/docker-calls-forced" \
    MOCK_CURRENT_REVISION=0123456789abcdef0123456789abcdef01234567 \
    FORCE_UPDATE=true \
    sh "$ROOT/docker-update.sh"
)
grep -Fq 'pull ghcr.io/timshitpig/mantou-toolbox-backend:latest' "$TEMP_ROOT/docker-calls-forced"
grep -Fq -- '-p 6185:8787' "$TEMP_ROOT/docker-calls-forced"
grep -Fq -- "--user ${EXPECTED_UID}:${EXPECTED_GID}" "$TEMP_ROOT/docker-calls-forced"
grep -Fq 'network create mantou-toolbox-network' "$TEMP_ROOT/docker-calls-forced"
grep -Fq -- '-v /var/run/docker.sock:/var/run/docker.sock' "$TEMP_ROOT/docker-calls-forced"
grep -Fq -- '--name mantou-toolbox-updater' "$TEMP_ROOT/docker-calls-forced"
grep -Fq -- 'UPDATE_AGENT_URL=http://updater:8787' "$TEMP_ROOT/docker-calls-forced"

(
  cd "$TEMP_ROOT/server"
  PATH="$TEMP_ROOT/bin:$PATH" \
    CURL_CALLS="$TEMP_ROOT/curl-calls-proxy" \
    DOCKER_CALLS="$TEMP_ROOT/docker-calls-proxy" \
    MOCK_CURRENT_REVISION=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    GITHUB_PROXY=https://gh-proxy.com \
    sh "$ROOT/docker-update.sh"
)
grep -Fq 'https://gh-proxy.com/https://api.github.com/repos/TimShitPig/mantou-toolbox-backend/commits/main' "$TEMP_ROOT/curl-calls-proxy"
grep -Fq 'pull ghcr.io/timshitpig/mantou-toolbox-backend:latest' "$TEMP_ROOT/docker-calls-proxy"

if (
  cd "$TEMP_ROOT/server"
  PATH="$TEMP_ROOT/bin:$PATH" \
    DOCKER_CALLS="$TEMP_ROOT/docker-calls-invalid-proxy" \
    GITHUB_PROXY=https://invalid.example \
    sh "$ROOT/docker-update.sh" 2>/dev/null
); then
  printf '%s\n' 'unsupported GitHub proxy was accepted' >&2
  exit 1
fi

mkdir -p "$TEMP_ROOT/legacy"
sed '/^ADMIN_PASSWORD=/d' "$ENV_FILE" > "$TEMP_ROOT/legacy/.env"
OLD_SECRET="$(sed -n 's/^APP_SECRET=//p' "$TEMP_ROOT/legacy/.env")"
(
  cd "$TEMP_ROOT/legacy"
    PATH="$TEMP_ROOT/bin:$PATH" \
    DOCKER_CALLS="$TEMP_ROOT/docker-calls-legacy" \
    MOCK_CURRENT_REVISION= \
    sh "$ROOT/docker-update.sh"
)
MIGRATED_ADMIN_PASSWORD="$(sed -n 's/^ADMIN_PASSWORD=//p' "$TEMP_ROOT/legacy/.env")"
MIGRATED_SECRET="$(sed -n 's/^APP_SECRET=//p' "$TEMP_ROOT/legacy/.env")"
[ "${#MIGRATED_ADMIN_PASSWORD}" -eq 64 ]
[ "$MIGRATED_SECRET" = "$OLD_SECRET" ]
assert_private_file "$TEMP_ROOT/legacy/.env"

mkdir -p "$TEMP_ROOT/prepare"
(
  cd "$TEMP_ROOT/prepare"
  PATH="$TEMP_ROOT/bin:$PATH" \
    DOCKER_CALLS="$TEMP_ROOT/docker-calls-prepare" \
    COMPOSE_FIXTURE="$ROOT/compose.yaml" \
    RAW_BASE=https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main \
    sh "$ROOT/deploy/docker-deploy.sh"
)
PREPARED_ENV="$TEMP_ROOT/prepare/.env"
[ -s "$TEMP_ROOT/prepare/compose.yaml" ]
[ -d "$TEMP_ROOT/prepare/content" ]
grep -Fqx 'PUBLIC_PORT=8787' "$PREPARED_ENV"
grep -Fqx 'APP_BASE_URL=http://198.51.100.27:8787' "$PREPARED_ENV"
grep -Fqx 'BACKEND_IMAGE=ghcr.io/timshitpig/mantou-toolbox-backend:latest' "$PREPARED_ENV"
grep -Fqx 'UPDATE_AGENT_IMAGE=ghcr.io/timshitpig/mantou-toolbox-backend:latest' "$PREPARED_ENV"
grep -Fqx "UPDATE_DEPLOY_DIR=$TEMP_ROOT/prepare" "$PREPARED_ENV"
[ "$(sed -n 's/^APP_SECRET=//p' "$PREPARED_ENV" | wc -c | tr -d ' ')" -eq 65 ]
[ "$(sed -n 's/^ADMIN_PASSWORD=//p' "$PREPARED_ENV" | wc -c | tr -d ' ')" -eq 65 ]
[ "$(sed -n 's/^UPDATE_AGENT_SECRET=//p' "$PREPARED_ENV" | wc -c | tr -d ' ')" -eq 65 ]
assert_private_file "$PREPARED_ENV"
BEFORE_PREPARED_ENV="$(sha256sum "$PREPARED_ENV" | cut -d' ' -f1)"
(
  cd "$TEMP_ROOT/prepare"
  PATH="$TEMP_ROOT/bin:$PATH" \
    DOCKER_CALLS="$TEMP_ROOT/docker-calls-prepare-rerun" \
    COMPOSE_FIXTURE="$ROOT/compose.yaml" \
    RAW_BASE=https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main \
    sh "$ROOT/deploy/docker-deploy.sh"
)
AFTER_PREPARED_ENV="$(sha256sum "$PREPARED_ENV" | cut -d' ' -f1)"
[ "$BEFORE_PREPARED_ENV" = "$AFTER_PREPARED_ENV" ]
grep -Fq 'pull_policy: always' "$TEMP_ROOT/prepare/compose.yaml"

printf '%s\n' 'deployment config generation and update preservation passed'
