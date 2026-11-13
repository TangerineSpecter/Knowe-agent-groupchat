#!/bin/sh
# Run on the Docker host. Preserve data, passwords and unrelated .env settings.
set -eu
cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"

fail() { printf '%s\n' "$*" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || fail '请先安装 Docker 和 Docker Compose。'
docker compose version >/dev/null
docker info >/dev/null 2>&1 || fail '无法连接 Docker，请确认 Docker 已启动且当前用户有访问权限。'

deploy_pull_pid=''
deploy_progress_pid=''
deploy_pull_dir=''
deploy_env_tmp=''
cleanup() {
  for deploy_job in "$deploy_pull_pid" "$deploy_progress_pid"; do
    if [ -n "$deploy_job" ]; then
      kill "$deploy_job" 2>/dev/null || :
      wait "$deploy_job" 2>/dev/null || :
    fi
  done
  if [ -n "$deploy_pull_dir" ]; then rm -rf "$deploy_pull_dir"; fi
  if [ -n "$deploy_env_tmp" ]; then rm -f "$deploy_env_tmp"; fi
}
trap cleanup 0
trap 'exit 130' INT
trap 'exit 143' TERM

# Update only installer-owned keys. Never source .env as executable shell code.
save_setting() {
  deploy_env_tmp=$(umask 077; mktemp .env.knowe.XXXXXX)
  awk -v key="$1" -v value="$2" '
    $0 ~ "^[[:space:]]*(export[[:space:]]+)?" key "=" {
      if (!written) print key "=" value
      written=1
      next
    }
    { print }
    END { if (!written) print key "=" value }
  ' .env > "$deploy_env_tmp"
  mv "$deploy_env_tmp" .env
  deploy_env_tmp=''
}

deploy_uid=$(id -u)
deploy_gid=$(id -g)
if [ "$deploy_uid" -eq 0 ]; then deploy_uid=1000; deploy_gid=1000; fi
for deploy_directory in deploy/data deploy/workspaces; do
  if [ ! -d "$deploy_directory" ]; then
    mkdir -p "$deploy_directory"
    if [ "$(id -u)" -eq 0 ]; then chown "$deploy_uid:$deploy_gid" "$deploy_directory"; fi
  fi
done
if [ ! -f deploy/web-password ]; then
  command -v openssl >/dev/null 2>&1 || fail '请先安装 openssl，或手动创建 deploy/web-password（至少 12 个字符）。'
  (umask 077; openssl rand -hex 24 > deploy/web-password)
  if [ "$(id -u)" -eq 0 ]; then chown "$deploy_uid:$deploy_gid" deploy/web-password; fi
fi
if [ ! -f .env ]; then (umask 077; : > .env); fi
if ! grep -Eq '^[[:space:]]*(export[[:space:]]+)?KNOWE_UID=' .env; then save_setting KNOWE_UID "$deploy_uid"; fi
if ! grep -Eq '^[[:space:]]*(export[[:space:]]+)?KNOWE_GID=' .env; then save_setting KNOWE_GID "$deploy_gid"; fi

# Compose resolves quoted values and environment overrides using its own rules.
deploy_config=$(docker compose config --environment)
setting() {
  deploy_value=$(printf '%s\n' "$deploy_config" | awk -v key="$1" '
    index($0, key "=")==1 { print substr($0, length(key)+2); exit }')
  printf '%s\n' "${deploy_value:-$2}"
}
deploy_bind_host=$(setting KNOWE_BIND_HOST 127.0.0.1)
deploy_port=$(setting KNOWE_PORT 18090)
deploy_port=$(printf '%s\n' "$deploy_port" | awk '/^[0-9]+$/ && $0>=1 && $0<=65535 {printf "%.0f", $0}')
[ -n "$deploy_port" ] || fail 'KNOWE_PORT 必须是 1–65535 之间的整数。'
deploy_source=$(setting KNOWE_DOWNLOAD_SOURCE mirror)
case "$deploy_source" in auto|mirror|official) ;; *) fail 'KNOWE_DOWNLOAD_SOURCE 仅支持 auto、mirror、official。';; esac
deploy_pull_timeout=$(setting KNOWE_PULL_TIMEOUT 60)
deploy_pull_timeout=$(printf '%s\n' "$deploy_pull_timeout" | awk '/^[0-9]+$/ && $0>=1 && $0<=3600 {printf "%.0f", $0}')
[ -n "$deploy_pull_timeout" ] || fail 'KNOWE_PULL_TIMEOUT 必须是 1–3600 之间的无进度等待秒数。'

deploy_current_id=$(docker compose ps --all --quiet knowe)
deploy_data_path=$(setting KNOWE_DATA_PATH ./deploy/data)
if [ "$deploy_port" -eq 8090 ] && [ -z "${KNOWE_PORT:-}" ] \
  && [ -z "$deploy_current_id" ] && [ ! -f "$deploy_data_path/web/session.key" ]; then
  printf '%s\n' '检测到尚未启动过的旧版默认配置，宿主机端口从 8090 改为 18090。'
  deploy_port=18090
fi

port_available() {
  # Re-running the installer must retain a port already owned by this service.
  deploy_owned_binding=$(docker compose port knowe 8090 2>/dev/null || :)
  if [ "$deploy_owned_binding" = "$deploy_bind_host:$1" ] \
    || [ "$deploy_owned_binding" = "[$deploy_bind_host]:$1" ]; then return 0; fi
  deploy_published=$(docker ps --format '{{.Ports}}') || fail '无法读取 Docker 已发布端口。'
  if printf '%s\n' "$deploy_published" | awk -v port="$1" '
    {
      count=split($0, entries, ",")
      for (i=1; i<=count; i++) {
        if (!index(entries[i], "->")) continue
        split(entries[i], direction, "->")
        bound=direction[1]; sub(/^.*:/, "", bound)
        if (bound !~ /^[0-9]+(-[0-9]+)?$/) continue
        split(bound, bounds, "-")
        end=(bounds[2]=="" ? bounds[1] : bounds[2])
        if (port>=bounds[1]+0 && port<=end+0) found=1
      }
    }
    END {exit !found}'; then return 1; fi
  if command -v ss >/dev/null 2>&1; then
    deploy_listeners=$(ss -H -ltn) || fail '无法检查监听端口（ss）。'
    ! printf '%s\n' "$deploy_listeners" | awk -v port="$1" '
      $4 ~ ":" port "$" {found=1} END {exit !found}'
  elif command -v lsof >/dev/null 2>&1; then
    ! lsof -nP -iTCP:"$1" -sTCP:LISTEN -t >/dev/null 2>&1
  elif [ -r /proc/net/tcp ]; then
    deploy_port_hex=$(printf '%04X' "$1")
    deploy_tcp_files=/proc/net/tcp
    if [ -r /proc/net/tcp6 ]; then deploy_tcp_files="$deploy_tcp_files /proc/net/tcp6"; fi
    ! awk -v port="$deploy_port_hex" '
      $4=="0A" {split($2, address, ":"); if (address[2]==port) found=1} END {exit !found}
    ' $deploy_tcp_files
  else
    fail '无法检测端口占用，请安装 iproute2（ss）或 lsof 后重试。'
  fi
}

choose_port() {
  deploy_requested_port=$deploy_port
  deploy_attempt=0
  while ! port_available "$deploy_port"; do
    deploy_attempt=$((deploy_attempt+1))
    [ "$deploy_attempt" -lt 100 ] && [ "$deploy_port" -lt 65535 ] \
      || fail '连续 100 个端口不可用，或已到端口上限；请设置 KNOWE_PORT 后重试。'
    deploy_port=$((deploy_port+1))
  done
  if [ "$deploy_port" -ne "$deploy_requested_port" ]; then
    printf '端口 %s 已被占用，自动改用 %s。\n' "$deploy_requested_port" "$deploy_port"
  fi
  save_setting KNOWE_PORT "$deploy_port"
  save_setting KNOWE_BIND_HOST "$deploy_bind_host"
  KNOWE_PORT=$deploy_port
  KNOWE_BIND_HOST=$deploy_bind_host
  export KNOWE_PORT KNOWE_BIND_HOST
}
choose_port

pull_image() {
  if docker image inspect "$1" >/dev/null 2>&1; then
    printf '复用本地基础镜像：%s\n' "$1"
    return 0
  fi
  printf '拉取基础镜像（连续 %s 秒无进度才换源，总时长不限）：%s\n' "$deploy_pull_timeout" "$1"
  deploy_pull_dir=$(umask 077; mktemp -d "${TMPDIR:-/tmp}/knowe-pull.XXXXXX") || fail '无法创建拉取进度目录。'
  # Plain Compose progress includes layer byte counts even when piped. Ordinary
  # `docker pull` hides those updates without a TTY, making idle detection unsafe.
  cat > "$deploy_pull_dir/compose.yaml" <<'PULL_CONFIG'
services:
  base-image:
    image: ${KNOWE_INSTALL_PULL_IMAGE:?}
PULL_CONFIG
  mkfifo "$deploy_pull_dir/output.pipe" || fail '无法创建拉取进度管道。'
  : > "$deploy_pull_dir/progress.log"
  tee "$deploy_pull_dir/progress.log" < "$deploy_pull_dir/output.pipe" &
  deploy_progress_pid=$!
  KNOWE_INSTALL_PULL_IMAGE="$1" docker compose --progress plain \
    --project-name knowe-installer-pull --file "$deploy_pull_dir/compose.yaml" \
    pull base-image > "$deploy_pull_dir/output.pipe" 2>&1 &
  deploy_pull_pid=$!
  deploy_pull_status=0
  deploy_progress_size=0
  deploy_idle_seconds=0
  while kill -0 "$deploy_pull_pid" 2>/dev/null; do
    sleep 1
    deploy_next_size=$(wc -c < "$deploy_pull_dir/progress.log")
    if [ "$deploy_next_size" -ne "$deploy_progress_size" ]; then
      deploy_progress_size=$deploy_next_size
      deploy_idle_seconds=0
    else
      deploy_idle_seconds=$((deploy_idle_seconds+1))
    fi
    if [ "$deploy_idle_seconds" -ge "$deploy_pull_timeout" ] \
      && kill -0 "$deploy_pull_pid" 2>/dev/null; then
      printf '连续 %s 秒没有新的镜像拉取进度，停止本次拉取。\n' "$deploy_pull_timeout" >&2
      kill "$deploy_pull_pid" 2>/dev/null || :
      deploy_pull_status=1
      break
    fi
  done
  if ! wait "$deploy_pull_pid"; then deploy_pull_status=1; fi
  wait "$deploy_progress_pid" || deploy_pull_status=1
  deploy_pull_pid=''
  deploy_progress_pid=''
  rm -rf "$deploy_pull_dir"
  deploy_pull_dir=''
  return "$deploy_pull_status"
}

deploy_used_mirror=0
prepare_image() {
  deploy_image_key=$1
  deploy_official_image=$2
  deploy_mirror_image="m.daocloud.io/docker.io/library/$2"
  deploy_image=$(setting "$deploy_image_key" '')
  deploy_alternative=''
  case "$deploy_image" in
    ''|"$deploy_official_image"|"$deploy_mirror_image")
      if [ "$deploy_source" = mirror ] \
        || { [ "$deploy_source" = auto ] && [ "$deploy_image" = "$deploy_mirror_image" ]; }; then
        deploy_image=$deploy_mirror_image
        deploy_alternative=$deploy_official_image
      else
        deploy_image=$deploy_official_image
        if [ "$deploy_source" = auto ]; then deploy_alternative=$deploy_mirror_image; fi
      fi
      ;;
  esac
  if ! pull_image "$deploy_image"; then
    [ -n "$deploy_alternative" ] || fail "无法拉取基础镜像：${deploy_image}；请检查仓库地址、登录状态和 Docker 网络。"
    printf '当前源不可用，切换到：%s\n' "$deploy_alternative"
    deploy_image=$deploy_alternative
    pull_image "$deploy_image" || fail '官方源和镜像源均下载失败，请检查 Docker 网络或在 .env 中配置可用的基础镜像地址。'
  fi
  if [ "$deploy_image" = "$deploy_mirror_image" ]; then deploy_used_mirror=1; fi
  save_setting "$deploy_image_key" "$deploy_image"
}

printf '%s\n' '阶段 1/3：准备基础镜像；下载不通时自动切换源。'
prepare_image KNOWE_NODE_IMAGE node:22-bookworm-slim
KNOWE_NODE_IMAGE=$deploy_image
prepare_image KNOWE_PYTHON_IMAGE python:3.11-slim-bookworm
KNOWE_PYTHON_IMAGE=$deploy_image
deploy_npm_registry=$(setting KNOWE_NPM_REGISTRY '')
if [ -z "$deploy_npm_registry" ]; then
  deploy_npm_registry=https://registry.npmjs.org
  if [ "$deploy_used_mirror" -eq 1 ]; then deploy_npm_registry=https://registry.npmmirror.com; fi
  save_setting KNOWE_NPM_REGISTRY "$deploy_npm_registry"
fi
KNOWE_NPM_REGISTRY=$deploy_npm_registry
export KNOWE_NODE_IMAGE KNOWE_PYTHON_IMAGE KNOWE_NPM_REGISTRY

# Each package manager uses a separate repository. A Docker image mirror alone
# does not speed up apt, pip or the browser binary downloaded during the build.
deploy_pip_default=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
deploy_apt_default=https://mirrors.tuna.tsinghua.edu.cn
deploy_browser_default=https://cdn.npmmirror.com/binaries/playwright
if [ "$deploy_source" = official ]; then
  deploy_pip_default=https://pypi.org/simple
  deploy_apt_default=https://deb.debian.org
  deploy_browser_default=''
fi
KNOWE_PIP_INDEX_URL=$(setting KNOWE_PIP_INDEX_URL "$deploy_pip_default")
KNOWE_APT_MIRROR=$(setting KNOWE_APT_MIRROR "$deploy_apt_default")
# An explicitly empty host selects Playwright's official CDN.
if printf '%s\n' "$deploy_config" | grep -q '^KNOWE_PLAYWRIGHT_DOWNLOAD_HOST='; then
  KNOWE_PLAYWRIGHT_DOWNLOAD_HOST=$(printf '%s\n' "$deploy_config" | sed -n 's/^KNOWE_PLAYWRIGHT_DOWNLOAD_HOST=//p')
else
  KNOWE_PLAYWRIGHT_DOWNLOAD_HOST=$deploy_browser_default
fi
save_setting KNOWE_PIP_INDEX_URL "$KNOWE_PIP_INDEX_URL"
save_setting KNOWE_APT_MIRROR "$KNOWE_APT_MIRROR"
save_setting KNOWE_PLAYWRIGHT_DOWNLOAD_HOST "$KNOWE_PLAYWRIGHT_DOWNLOAD_HOST"
export KNOWE_PIP_INDEX_URL KNOWE_APT_MIRROR KNOWE_PLAYWRIGHT_DOWNLOAD_HOST
printf '%s\n' '已配置 Python、Debian 和浏览器下载源，构建将保留下载缓存。'

printf '%s\n' '阶段 2/3：构建应用镜像（安装依赖、编译前端、安装 Chromium）；首次耗时较长。'
docker compose --progress plain build knowe
# Dependencies may take minutes to build. Check again immediately before publishing.
choose_port
printf '%s\n' '阶段 3/3：启动并等待健康检查。'
docker compose up -d --no-build --wait --wait-timeout 180
case "$deploy_bind_host" in
  0.0.0.0|::) printf 'Knowe 已启动：宿主机端口 %s，请通过服务器地址或反向代理访问。\n' "$deploy_port";;
  *:*) printf 'Knowe 已启动：http://[%s]:%s\n' "$deploy_bind_host" "$deploy_port";;
  *) printf 'Knowe 已启动：http://%s:%s\n' "$deploy_bind_host" "$deploy_port";;
esac
printf '%s\n' '选用的端口和镜像源已保存至 .env，后续运行会复用。' \
  '访问密码保存在 deploy/web-password；请自行读取并妥善保存。' \
  '远程访问及 HTTPS 配置见 docs/CN/网页版部署.md。'
