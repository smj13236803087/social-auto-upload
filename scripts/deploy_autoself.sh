#!/usr/bin/env bash
# Deploy AutoSelf with zero-downtime rolling reload.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${AUTOSELF_SSH_HOST:-autoself}"
REMOTE="${AUTOSELF_REMOTE_DIR:-/home/ubuntu/apps/autoself}"

py_changed=0

for rel in "$@"; do
  src="$ROOT/$rel"
  if [[ ! -e "$src" ]]; then
    echo "skip missing: $rel" >&2
    continue
  fi
  # ensure remote parent dir exists
  ssh "$HOST" "mkdir -p \"$(dirname "$REMOTE/$rel")\""
  scp -q "$src" "$HOST:$REMOTE/$rel"
  echo "synced $rel"
  if [[ "$rel" == *.py ]]; then
    py_changed=1
  fi
done

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 <relative-path> [more paths...]" >&2
  exit 1
fi

wait_health() {
  local port="$1"
  for _ in $(seq 1 30); do
    if ssh "$HOST" "curl -fsS -o /dev/null http://127.0.0.1:${port}/api/health"; then
      return 0
    fi
    sleep 0.2
  done
  echo "health check failed on :$port" >&2
  return 1
}

if [[ "$py_changed" -eq 1 ]]; then
  echo "python changed -> rolling reload (5410 then 5411)"
  ssh "$HOST" 'sudo systemctl reload autoself@5410'
  wait_health 5410
  # give nginx a beat so in-flight requests drain before bouncing the peer
  sleep 1
  ssh "$HOST" 'sudo systemctl reload autoself@5411'
  wait_health 5411
else
  echo "static/config only -> no service bounce (files served from disk)"
fi

ssh "$HOST" 'curl -fsS -o /dev/null -w "nginx %{http_code}\n" -k https://127.0.0.1/api/health -H "Host: autopost.com.cn"'
