#!/usr/bin/env bash
# ff 一条龙：解析 ff.org -> 抓取 -> 生成静态站
#
#   ./scripts/ff_run.sh               常规运行（只抓到期的源）
#   ./scripts/ff_run.sh --force       强制抓全部源
#   ./scripts/ff_run.sh --serve       跑完后起本地预览（默认 1314 端口）
#
# 环境变量：
#   FF_PYTHON      Python 解释器（默认项目内 .venv，否则系统 python3）
#   FF_SITE_URL    站点地址（默认 https://ff.luwq.cn，CI 里由 workflow 覆盖）
#   FF_SOURCE      ff.org 路径（默认 ../weborg/org/ff.org，即 weborg 仓库的源清单）
#                  GitHub Actions 里这份源会被同步到本仓库根目录的 ff.org，脚本自动回退到它

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 默认依次尝试：环境变量、venv（项目内 .venv）、系统 python3；本地与 CI 都能跑
if [ -n "${FF_PYTHON:-}" ]; then
  PY="$FF_PYTHON"
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi
# 本地开发读 weborg 里的源清单；CI 里没有 weborg，回退到本仓库同步过来的副本
LOCAL_SRC="$ROOT/../weborg/org/ff.org"
MIRROR_SRC="$ROOT/ff.org"
if [ -n "${FF_SOURCE:-}" ]; then
  SRC="$FF_SOURCE"
elif [ -f "$LOCAL_SRC" ]; then
  SRC="$LOCAL_SRC"
else
  SRC="$MIRROR_SRC"
fi
PORT=1314
FORCE=0
SERVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --serve) SERVE=1 ;;
    --port) PORT="$2"; shift ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
  esac
  shift
done

[ -f "$SRC" ] || { echo "找不到源清单：$SRC" >&2; exit 1; }
mkdir -p "$ROOT/data" "$ROOT/dist"

echo "==> [1/3] 解析 $SRC"
FF_SOURCE="$SRC" FF_OUT="$ROOT/data/feeds.json" \
  "$PY" "$ROOT/scripts/ff_parse.py"

echo "==> [2/3] 抓取订阅源"
FETCH_ARGS=(--root "$ROOT")
[ "$FORCE" -eq 1 ] && FETCH_ARGS+=(--force)
"$PY" "$ROOT/scripts/ff_fetch.py" "${FETCH_ARGS[@]}"

echo "==> [3/3] 生成站点与订阅"
"$PY" "$ROOT/scripts/ff_generate.py" --root "$ROOT" | tee "$ROOT/data/last-run.log"

# 供 CI 判断：把「产物是否变化」写到 GitHub Actions 的 output / 本地标志文件
if grep -q "产物变化: 是" "$ROOT/data/last-run.log"; then
  CHANGED=1
else
  CHANGED=0
fi
printf '%s\n' "$CHANGED" > "$ROOT/data/changed"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "changed=$CHANGED" >> "$GITHUB_OUTPUT"
fi

echo "==> 产物是否变化：$CHANGED（1=有变化可发布 / 0=无变化跳过）"
if [ "$SERVE" -eq 1 ]; then
  echo "==> 预览：http://127.0.0.1:$PORT/"
  echo "    Ctrl-C 结束"
  cd "$ROOT/dist"
  exec "$PY" -m http.server "$PORT" --bind 127.0.0.1
fi
