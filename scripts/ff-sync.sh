#!/usr/bin/env bash
# ff-sync — 把 weborg 的 org/ff.org 同步到 feedfinder 仓库并推送到 GitHub
#
#   ./scripts/ff-sync.sh              检查源清单：变了才复制、提交、推送
#   ./scripts/ff-sync.sh --force      内容没变也提交推送（首次建仓库用）
#   ./scripts/ff-sync.sh --dry-run    只检查并复制，不提交不推送
#   ./scripts/ff-sync.sh -m "说明"     指定提交说明
#
# 设计：
#   * 唯一编辑处是 weborg/org/ff.org；本仓库根目录的 ff.org 是镜像副本，
#     由本脚本复制生成，CI 里读的就是这份副本。
#   * 用 SHA256 内容指纹判断变化，不靠 mtime（git 不存 mtime）。
#   * 本机 git 走 SSH 有权限问题，推送统一用 HTTPS + PAT（GH_TOKEN 环境变量）。
#
# 环境变量：
#   GH_TOKEN      GitHub PAT（Contents: Read and write）。没有则用 git 已配置的凭证。
#   SYNC_REMOTE   推送目标（默认用本仓库已配置的 origin）
#   SYNC_BRANCH   目标分支（默认 main）
#   SOURCE_ORG    weborg 的 ff.org 路径（默认 ../weborg/org/ff.org）

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SOURCE_ORG="${SOURCE_ORG:-$ROOT/../weborg/org/ff.org}"
MIRROR="$ROOT/ff.org"
HASH_FILE="$ROOT/source-hash"   # 放根目录（data/ 被 gitignore，进不了库）
REMOTE="${SYNC_REMOTE:-$(git remote get-url origin 2>/dev/null || true)}"
BRANCH="${SYNC_BRANCH:-main}"

FORCE=0
DRY=0
MSG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --dry-run) DRY=1 ;;
    -m|--message) MSG="${2:-}"; [ -n "$MSG" ] || { echo "-m 后要跟说明" >&2; exit 1; }; shift ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,"");print;next} NR>1{exit}' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
  esac
  shift
done

[ -f "$SOURCE_ORG" ] || { echo "找不到源清单：$SOURCE_ORG" >&2; exit 1; }
mkdir -p "$ROOT/data"

new_hash="$(sha256sum "$SOURCE_ORG" | cut -d' ' -f1)"
old_hash="$(cat "$HASH_FILE" 2>/dev/null || true)"

if [ "$FORCE" -eq 0 ] && [ "$new_hash" = "$old_hash" ] && [ -f "$MIRROR" ]; then
  echo "==> ff.org 无变化（${new_hash:0:12}），跳过同步"
  exit 0
fi

short_old="$old_hash"
[ -n "$short_old" ] && short_old="${short_old:0:12}" || short_old="none"
echo "==> ff.org 有变化：${short_old} → ${new_hash:0:12}"
cp "$SOURCE_ORG" "$MIRROR"
printf '%s\n' "$new_hash" > "$HASH_FILE"

if [ "$DRY" -eq 1 ]; then
  echo "==> dry-run：已复制到 $MIRROR，未提交未推送"
  exit 0
fi

# 确保远端存在（首次运行）
if ! git remote get-url origin >/dev/null 2>&1; then
  [ -n "$REMOTE" ] || { echo "未配置远端：请设置 SYNC_REMOTE 或先添加 git origin" >&2; exit 1; }
  git remote add origin "$REMOTE"
fi
git add ff.org source-hash

if git diff --cached --quiet; then
  echo "==> 仓库内容无差异，跳过提交"
  exit 0
fi

[ -n "$MSG" ] || MSG="同步 ff.org（$(date '+%Y-%m-%d %H:%M')）"
git commit -q -m "$MSG"

if [ -n "${GH_TOKEN:-}" ] && [ -n "$REMOTE" ]; then
  # 把 https://host/path 改成 https://TOKEN@host/path，不写死任何账号名
  push_url="${REMOTE/https:\/\//https:\/\/${GH_TOKEN}@}"
else
  push_url="origin"
fi
echo "==> 推送到 GitHub（$BRANCH）"
git push "$push_url" "HEAD:$BRANCH"
echo "==> 同步完成，Actions 将被触发"
