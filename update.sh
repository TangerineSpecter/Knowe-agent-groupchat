#!/bin/sh
# Run on the Docker host; install.sh owns configuration, data and container updates.
set -eu

fail() { printf '%s\n' "$*" >&2; exit 1; }

# Parse the complete function before pulling: Git may replace this script in place.
main() {
  cd "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
  for update_argument in "$@"; do
    case "$update_argument" in
      --lan|--local) ;;
      -h|--help)
        printf '%s\n' '用法：./update.sh [--lan|--local]' \
          '拉取当前分支的远端更新，复用缓存构建镜像，然后重建服务并检查健康状态。' \
          '保留已有配置、端口、密码和持久数据。更新会重启服务，请先等活动任务结束。' \
          '--lan 允许内网访问；--local 仅限本机；默认保留已有监听配置。'
        return 0;;
      --no-build) fail '更新代码需要构建镜像；仅调整运行配置请使用 ./deploy/install.sh --no-build。';;
      *) fail "未知参数：${update_argument}；用法：./update.sh [--lan|--local]";;
    esac
  done

  command -v git >/dev/null 2>&1 || fail '请先安装 Git。'
  update_root=$(git rev-parse --show-toplevel 2>/dev/null) \
    || fail '未找到 Git 仓库，请在完整克隆的项目中运行 update.sh。'
  [ "$update_root" = "$(pwd -P)" ] || fail 'update.sh 必须位于项目的 Git 仓库根目录。'
  update_branch=$(git symbolic-ref --quiet --short HEAD) \
    || fail '当前处于 detached HEAD，请先切换到要更新的分支。'
  update_upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null) \
    || fail '当前分支没有配置远端跟踪分支，请先配置后再更新。'
  update_changes=$(git status --porcelain --untracked-files=no)
  [ -z "$update_changes" ] \
    || fail '源码有未提交的修改，请先用 git status 查看并处理；更新已停止。'

  printf '阶段 1/2：更新源码（%s ← %s）。\n' "$update_branch" "$update_upstream"
  git pull --ff-only || fail '源码拉取失败，未执行构建或重启；请处理上方 Git 错误后重试。'
  printf '%s\n' '阶段 2/2：构建并更新服务。'
  exec sh ./deploy/install.sh "$@"
}

main "$@"
