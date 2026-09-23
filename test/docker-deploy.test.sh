#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

mkdir -p "$TEMP_ROOT/bin" "$TEMP_ROOT/server"

cat > "$TEMP_ROOT/bin/curl" <<'EOF'
#!/usr/bin/env sh
printf '%s\n' '198.51.100.27'
EOF

cat > "$TEMP_ROOT/bin/docker" <<'EOF'
#!/usr/bin/env sh
printf '%s\n' "$*" >> "$DOCKER_CALLS"
EOF

chmod +x "$TEMP_ROOT/bin/curl" "$TEMP_ROOT/bin/docker"

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
[ "$(stat -c '%a' "$ENV_FILE")" = '600' ]
SECRET="$(sed -n 's/^APP_SECRET=//p' "$ENV_FILE")"
[ "${#SECRET}" -eq 64 ]
ADMIN_PASSWORD="$(sed -n 's/^ADMIN_PASSWORD=//p' "$ENV_FILE")"
[ "${#ADMIN_PASSWORD}" -eq 64 ]
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
    sh "$ROOT/docker-update.sh"
)
AFTER="$(sha256sum "$ENV_FILE" | cut -d' ' -f1)"
[ "$BEFORE" = "$AFTER" ]
grep -Fq -- '-p 6185:8787' "$TEMP_ROOT/docker-calls-update"
grep -Fq -- "--user ${EXPECTED_UID}:${EXPECTED_GID}" "$TEMP_ROOT/docker-calls-update"

mkdir -p "$TEMP_ROOT/legacy"
sed '/^ADMIN_PASSWORD=/d' "$ENV_FILE" > "$TEMP_ROOT/legacy/.env"
OLD_SECRET="$(sed -n 's/^APP_SECRET=//p' "$TEMP_ROOT/legacy/.env")"
(
  cd "$TEMP_ROOT/legacy"
  PATH="$TEMP_ROOT/bin:$PATH" \
    DOCKER_CALLS="$TEMP_ROOT/docker-calls-legacy" \
    sh "$ROOT/docker-update.sh"
)
MIGRATED_ADMIN_PASSWORD="$(sed -n 's/^ADMIN_PASSWORD=//p' "$TEMP_ROOT/legacy/.env")"
MIGRATED_SECRET="$(sed -n 's/^APP_SECRET=//p' "$TEMP_ROOT/legacy/.env")"
[ "${#MIGRATED_ADMIN_PASSWORD}" -eq 64 ]
[ "$MIGRATED_SECRET" = "$OLD_SECRET" ]
[ "$(stat -c '%a' "$TEMP_ROOT/legacy/.env")" = '600' ]

printf '%s\n' 'deployment config generation and update preservation passed'
