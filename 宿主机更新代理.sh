#!/usr/bin/env sh
set -eu

DEPLOY_DIR="${2:-${DEPLOY_DIR:-$(pwd -P)}}"
CONTROL_DIR="$DEPLOY_DIR/update-control"
COMPOSE_FILE="$DEPLOY_DIR/compose.yaml"
SERVICE=backend
IMAGE_TAG=mantou-toolbox-backend:local
REQUEST_FILE="$CONTROL_DIR/request"
PROCESSING_FILE="$CONTROL_DIR/request.processing"
STATUS_FILE="$CONTROL_DIR/status"
BUILD_LOG="$CONTROL_DIR/build.log"

write_status() {
  operation_id="$1"
  state="$2"
  message="$3"
  STATUS_OPERATION_ID="$operation_id"
  STATUS_STATE="$state"
  STATUS_MESSAGE="$message"
  temporary_file="$STATUS_FILE.$$.tmp"
  printf '%s\n%s\n%s\n%s\n' "$operation_id" "$state" "$message" "$(date +%s)" > "$temporary_file"
  mv -f "$temporary_file" "$STATUS_FILE"
}

valid_operation_id() {
  [ "${#1}" -eq 36 ] || return 1
  case "$1" in
    *[!a-f0-9-]*) return 1 ;;
  esac
}

restore_previous_image() {
  operation_id="$1"
  previous_image="$2"
  write_status "$operation_id" failed '新镜像启动检查失败，正在恢复旧镜像和源码'
  if [ -n "$previous_image" ]; then
    docker image tag "$previous_image" "$IMAGE_TAG" >> "$BUILD_LOG" 2>&1 || return 1
  fi
  docker compose --project-directory "$DEPLOY_DIR" -f "$COMPOSE_FILE" up -d --no-deps --no-build --force-recreate "$SERVICE" >> "$BUILD_LOG" 2>&1
}

wait_for_backend() {
  operation_id="$1"
  expected_version="$2"
  attempt=0
  while [ "$attempt" -lt 120 ]; do
    container_id="$(docker compose --project-directory "$DEPLOY_DIR" -f "$COMPOSE_FILE" ps -q "$SERVICE" 2>/dev/null | head -n 1 || true)"
    if [ -n "$container_id" ]; then
      state="$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)"
      case "$state" in
        'running healthy')
          version="$(docker exec -w /app "$container_id" node -e 'process.stdout.write("v" + require("./package.json").version)' 2>/dev/null || true)"
          if [ "$version" = "$expected_version" ]; then
            return 0
          fi
          ;;
        *unhealthy*|exited\ *|dead\ *) return 1 ;;
      esac
      if [ "$state" = 'running healthy' ]; then return 1; fi
    fi
    write_status "$operation_id" checking '容器正在启动，等待健康检查'
    attempt=$((attempt + 1))
    sleep 2
  done
  return 1
}

process_request() {
  operation_id="$(sed -n '1p' "$PROCESSING_FILE" 2>/dev/null || true)"
  version="$(sed -n '2p' "$PROCESSING_FILE" 2>/dev/null || true)"
  rm -f "$PROCESSING_FILE"
  if ! valid_operation_id "$operation_id" || ! printf '%s' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
    write_status "$operation_id" failed '更新请求格式无效'
    return
  fi

  old_container="$(docker compose --project-directory "$DEPLOY_DIR" -f "$COMPOSE_FILE" ps -q "$SERVICE" 2>/dev/null | head -n 1 || true)"
  if [ -z "$old_container" ]; then
    write_status "$operation_id" failed '找不到正在运行的后端容器'
    return
  fi
  old_image="$(docker inspect --format '{{.Image}}' "$old_container" 2>/dev/null || true)"
  if [ -z "$old_image" ]; then
    write_status "$operation_id" failed '无法读取当前后端镜像'
    return
  fi

  write_status "$operation_id" building '正在根据新源码构建 Docker 镜像'
  : > "$BUILD_LOG"
  docker compose --project-directory "$DEPLOY_DIR" -f "$COMPOSE_FILE" build --progress plain "$SERVICE" >> "$BUILD_LOG" 2>&1 &
  build_pid=$!
  while kill -0 "$build_pid" 2>/dev/null; do
    build_detail="$(tail -n 1 "$BUILD_LOG" 2>/dev/null | tr -cd '[:print:]' | cut -c 1-180 || true)"
    if [ -n "$build_detail" ]; then
      write_status "$operation_id" building "正在构建镜像：$build_detail"
    else
      write_status "$operation_id" building '正在构建 Docker 镜像，保留旧镜像用于回退'
    fi
    sleep 3
  done
  if ! wait "$build_pid"; then
    write_status "$operation_id" failed 'Docker 镜像构建失败，原容器和镜像保持不变'
    return
  fi

  new_image="$(docker image inspect --format '{{.Id}}' "$IMAGE_TAG" 2>/dev/null || true)"
  if [ -z "$new_image" ]; then
    write_status "$operation_id" failed '新镜像构建完成但无法读取镜像 ID'
    return
  fi
  write_status "$operation_id" recreating '新镜像已构建，正在重建后端容器'
  if ! docker compose --project-directory "$DEPLOY_DIR" -f "$COMPOSE_FILE" up -d --no-deps --no-build --force-recreate "$SERVICE" >> "$BUILD_LOG" 2>&1; then
    restore_previous_image "$operation_id" "$old_image" || true
    return
  fi

  write_status "$operation_id" checking '容器正在启动，等待健康检查'
  if ! wait_for_backend "$operation_id" "$version"; then
    restore_previous_image "$operation_id" "$old_image" || true
    return
  fi

  write_status "$operation_id" cleaning '新版本已通过健康检查，正在删除旧本地镜像'
  cleanup_message='更新完成，旧本地镜像已删除'
  if [ "$old_image" != "$new_image" ] && ! docker image rm "$old_image" >> "$BUILD_LOG" 2>&1; then
    cleanup_message='更新完成；旧镜像仍被其他容器使用，因此予以保留'
  fi
  if ! docker image prune -f --filter 'label=com.timshitpig.mantou-toolbox.managed=true' >> "$BUILD_LOG" 2>&1; then
    cleanup_message="$cleanup_message；馒头工具箱悬空镜像清理失败"
  fi
  write_status "$operation_id" complete "$cleanup_message"
}

watch_requests() {
  mkdir -p "$CONTROL_DIR"
  if [ -f "$PROCESSING_FILE" ] && [ ! -f "$REQUEST_FILE" ]; then
    mv "$PROCESSING_FILE" "$REQUEST_FILE"
  fi
  previous_operation="$(sed -n '1p' "$STATUS_FILE" 2>/dev/null || true)"
  previous_state="$(sed -n '2p' "$STATUS_FILE" 2>/dev/null || true)"
  previous_message="$(sed -n '3p' "$STATUS_FILE" 2>/dev/null || true)"
  previous_updated="$(sed -n '4p' "$STATUS_FILE" 2>/dev/null || true)"
  case "$previous_updated" in *[!0-9]*|'') previous_updated='' ;; esac
  if [ "$previous_state" = complete ] || [ "$previous_state" = failed ]; then
    if [ -n "$previous_updated" ] && [ $(( $(date +%s) - previous_updated )) -lt 120 ]; then
      write_status "$previous_operation" "$previous_state" "$previous_message"
    else
      write_status '' ready '宿主机 Docker 更新代理已就绪'
    fi
  else
    write_status '' ready '宿主机 Docker 更新代理已就绪'
  fi
  last_heartbeat="$(date +%s)"
  while :; do
    if [ -f "$REQUEST_FILE" ] && mv "$REQUEST_FILE" "$PROCESSING_FILE" 2>/dev/null; then
      process_request || write_status '' failed '宿主机更新代理执行失败'
      exit 0
    else
      now="$(date +%s)"
      if [ $((now - last_heartbeat)) -ge 5 ]; then
        write_status "$STATUS_OPERATION_ID" "$STATUS_STATE" "$STATUS_MESSAGE"
        last_heartbeat="$now"
      fi
    fi
    sleep 1
  done
}

case "${1:-}" in
  --watch) watch_requests ;;
  *) printf '%s\n' 'Usage: 宿主机更新代理.sh --watch [deploy-dir]' >&2; exit 2 ;;
esac
