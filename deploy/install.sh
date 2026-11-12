#!/bin/sh
# Run from any directory. Existing configuration and data are never overwritten.
set -eu
cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
command -v docker >/dev/null 2>&1 || { echo '请先安装 Docker 和 Docker Compose。' >&2; exit 1; }
docker compose version >/dev/null
deploy_uid=$(id -u)
deploy_gid=$(id -g)
if [ "$deploy_uid" -eq 0 ]; then
  deploy_uid=1000
  deploy_gid=1000
fi
for deploy_directory in deploy/data deploy/workspaces; do
  if [ ! -d "$deploy_directory" ]; then
    mkdir -p "$deploy_directory"
    if [ "$(id -u)" -eq 0 ]; then chown "$deploy_uid:$deploy_gid" "$deploy_directory"; fi
  fi
done
if [ ! -f deploy/web-password ]; then
  umask 077
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24 > deploy/web-password
  else
    echo '请先安装 openssl，或手动创建 deploy/web-password（至少 12 个字符）。' >&2
    exit 1
  fi
  if [ "$(id -u)" -eq 0 ]; then chown "$deploy_uid:$deploy_gid" deploy/web-password; fi
fi
if [ ! -f .env ]; then
  umask 077
  cat > .env <<CONFIG
KNOWE_UID=$deploy_uid
KNOWE_GID=$deploy_gid
KNOWE_BIND_HOST=127.0.0.1
KNOWE_PORT=8090
CONFIG
fi
if ! grep -q '^KNOWE_UID=' .env; then printf '\nKNOWE_UID=%s\n' "$deploy_uid" >> .env; fi
if ! grep -q '^KNOWE_GID=' .env; then printf '\nKNOWE_GID=%s\n' "$deploy_gid" >> .env; fi
docker compose up -d --build --wait --wait-timeout 180
printf '%s\n' 'Knowe 已启动：http://127.0.0.1:8090' '访问密码保存在 deploy/web-password；请自行读取并妥善保存。' '远程访问及 HTTPS 配置见 docs/CN/网页版部署.md。'
