#!/usr/bin/env bash
# Start AutoSelf local agent (Douyin/Kuaishou jobs from cloud).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SERVER="${AUTOSELF_SERVER:-https://autopost.com.cn}"
TOKEN_FILE="${AUTOSELF_TOKEN_FILE:-$HOME/.autoself/token}"

if [[ -z "${AUTOSELF_TOKEN:-}" && -f "$TOKEN_FILE" ]]; then
  AUTOSELF_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
  export AUTOSELF_TOKEN
fi

if [[ -z "${AUTOSELF_TOKEN:-}" ]]; then
  echo "缺少 token。任选其一：" >&2
  echo "  1) 网页点「复制启动命令」后粘贴执行" >&2
  echo "  2) mkdir -p ~/.autoself && echo '你的token' > ~/.autoself/token" >&2
  echo "  3) export AUTOSELF_TOKEN=..." >&2
  exit 1
fi

exec python -m web_mvp.local_agent --server "$SERVER" --token "$AUTOSELF_TOKEN"
