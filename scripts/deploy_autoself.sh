#!/usr/bin/env bash
# AutoSelf zero-downtime deploy (nginx → 5410 + 5411 rolling bounce).
#
# Every deploy:
#   1) sync code (keeps remote data/cookies/.venv)
#   2) sync .env.prod → /home/ubuntu/autoself/.env
#   3) apply pending migrations/*.sql on server MySQL
#   4) rolling-restart so systemd reloads EnvironmentFile
#
# Usage:
#   ./scripts/deploy_autoself.sh                  # full sync + env + migrate + restart
#   ./scripts/deploy_autoself.sh path [path...]   # listed paths + env + migrate + restart
#   ./scripts/deploy_autoself.sh --env-only       # only sync .env.prod + restart (no migrate)
#   ./scripts/deploy_autoself.sh --skip-env       # code only; reload (no env restart)
#   ./scripts/deploy_autoself.sh --skip-migrate   # do not run SQL migrations
#   ./scripts/deploy_autoself.sh --dry-run
#   ./scripts/deploy_autoself.sh --no-reload      # sync only, leave services alone
#
# Env overrides:
#   AUTOSELF_SSH_HOST=autoself
#   AUTOSELF_REMOTE_DIR=/home/ubuntu/apps/autoself
#   AUTOSELF_REMOTE_ENV=/home/ubuntu/autoself/.env
#   AUTOSELF_LOCAL_PROD_ENV=<repo>/.env.prod
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${AUTOSELF_SSH_HOST:-autoself}"
REMOTE="${AUTOSELF_REMOTE_DIR:-/home/ubuntu/apps/autoself}"
REMOTE_ENV="${AUTOSELF_REMOTE_ENV:-/home/ubuntu/autoself/.env}"
LOCAL_PROD_ENV="${AUTOSELF_LOCAL_PROD_ENV:-$ROOT/.env.prod}"

DRY_RUN=0
SKIP_ENV=0
SKIP_MIGRATE=0
ENV_ONLY=0
NO_RELOAD=0
PATHS=()

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-env) SKIP_ENV=1; shift ;;
    --skip-migrate) SKIP_MIGRATE=1; shift ;;
    --with-env) shift ;; # default; kept for backwards compatibility
    --env-only) ENV_ONLY=1; shift ;;
    --no-reload) NO_RELOAD=1; shift ;;
    --) shift; PATHS+=("$@"); break ;;
    -*)
      echo "unknown flag: $1" >&2
      usage 1
      ;;
    *) PATHS+=("$1"); shift ;;
  esac
done

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

remote() {
  run ssh "$HOST" "$@"
}

wait_health() {
  local port="$1"
  local i ok
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] health check :${port}"
    return 0
  fi
  ok=0
  for i in $(seq 1 60); do
    if ssh "$HOST" "curl -fsS -o /dev/null --connect-timeout 1 http://127.0.0.1:${port}/api/health"; then
      ok=$((ok + 1))
      # require 3 consecutive OK so we don't race a half-booted worker
      if [[ "$ok" -ge 3 ]]; then
        echo "  healthy :${port}"
        return 0
      fi
    else
      ok=0
    fi
    sleep 0.3
  done
  echo "health check failed on :${port}" >&2
  return 1
}

rolling_reload() {
  echo "rolling reload: autoself@5410 → health → autoself@5411 → health"
  remote "sudo systemctl reload autoself@5410"
  wait_health 5410
  sleep 2
  remote "sudo systemctl reload autoself@5411"
  wait_health 5411
}

# EnvFile is only read at process start; HUP is not enough after .env change.
rolling_restart() {
  echo "rolling restart (one at a time): autoself@5410 → health → autoself@5411 → health"
  # NEVER restart both in one systemctl command — that causes ERR_EMPTY_RESPONSE.
  remote "sudo systemctl restart autoself@5410"
  wait_health 5410
  sleep 3
  remote "sudo systemctl restart autoself@5411"
  wait_health 5411
}

# Returns ENV_CHANGED=1 if remote file content differed (or was missing).
ENV_CHANGED=0
sync_env() {
  if [[ ! -f "$LOCAL_PROD_ENV" ]]; then
    echo "missing production env: $LOCAL_PROD_ENV" >&2
    echo "create .env.prod in the repo root (same style as mini-program-backstage)" >&2
    exit 1
  fi
  echo "sync .env.prod → ${HOST}:${REMOTE_ENV}"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] scp $LOCAL_PROD_ENV → $REMOTE_ENV (chmod 600)"
    ENV_CHANGED=1
    return 0
  fi
  local env_dir local_hash remote_hash
  env_dir="$(dirname "$REMOTE_ENV")"
  ssh "$HOST" "mkdir -p '${env_dir}'"
  local_hash="$(shasum -a 256 "$LOCAL_PROD_ENV" | awk '{print $1}')"
  remote_hash="$(ssh "$HOST" "sha256sum '${REMOTE_ENV}' 2>/dev/null | awk '{print \$1}'" || true)"
  if [[ "$local_hash" == "$remote_hash" && -n "$remote_hash" ]]; then
    echo "  env unchanged (sha256=${local_hash:0:12}…)"
    ENV_CHANGED=0
    return 0
  fi
  scp -q "$LOCAL_PROD_ENV" "$HOST:$REMOTE_ENV"
  ssh "$HOST" "chmod 600 '${REMOTE_ENV}'"
  echo "  env updated"
  ENV_CHANGED=1
}

sync_paths() {
  local rel src parent
  PATHS_PY_CHANGED=0
  for rel in "${PATHS[@]}"; do
    src="$ROOT/$rel"
    if [[ ! -e "$src" ]]; then
      echo "skip missing: $rel" >&2
      continue
    fi
    parent="$(dirname "$REMOTE/$rel")"
    remote "mkdir -p '${parent}'"
    run scp -q "$src" "$HOST:$REMOTE/$rel"
    echo "synced $rel"
    if [[ "$rel" == *.py ]]; then
      PATHS_PY_CHANGED=1
    fi
  done
}

sync_full() {
  echo "full rsync → ${HOST}:${REMOTE}"
  # Preserve runtime state. Never push local secrets conf.py / cookies / data.
  local -a excludes=(
    "--exclude=.git/"
    "--exclude=.venv/"
    "--exclude=.venv"
    "--exclude=__pycache__/"
    "--exclude=*.pyc"
    "--exclude=.pytest_cache/"
    "--exclude=.mypy_cache/"
    "--exclude=.DS_Store"
    "--exclude=cookies/"
    "--exclude=data/"
    "--exclude=logs/"
    "--exclude=tmp/"
    "--exclude=videos/"
    "--exclude=media/"
    "--exclude=dist/"
    "--exclude=node_modules/"
    "--exclude=.env"
    "--exclude=.env.*"
    "--exclude=.env.prod"
    "--exclude=*.egg-info/"
    "--exclude=conf.py"
    "--exclude=*.pem"
    "--exclude=qrcode.png"
    "--exclude=uploader/*/qrcode.png"
  )

  if [[ "$DRY_RUN" -eq 1 ]]; then
    rsync -avzn --delete \
      "${excludes[@]}" \
      "$ROOT/" "$HOST:$REMOTE/"
    return 0
  fi

  rsync -az --delete \
    "${excludes[@]}" \
    "$ROOT/" "$HOST:$REMOTE/"
  echo "rsync done"
}

nginx_probe() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] nginx health probe"
    return 0
  fi
  ssh "$HOST" 'curl -fsS -o /dev/null -w "nginx %{http_code}\n" -k https://127.0.0.1/api/health -H "Host: autopost.com.cn"'
}

run_remote_migrations() {
  if [[ "$SKIP_MIGRATE" -eq 1 ]]; then
    echo "--skip-migrate: leave database alone"
    return 0
  fi
  if [[ "$ENV_ONLY" -eq 1 ]]; then
    echo "env-only: skip migrations"
    return 0
  fi
  echo "apply pending migrations on ${HOST}"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] ssh ${HOST} apply_migrations.sh"
    # Show what would run against local migration files (server state unknown).
    if [[ -x "$ROOT/scripts/apply_migrations.sh" ]]; then
      echo "[dry-run] local migrations/*.sql:"
      ls -1 "$ROOT/migrations"/*.sql 2>/dev/null || echo "  (none yet)"
    fi
    return 0
  fi
  # Ensure runner exists even on path-only deploys.
  ssh "$HOST" "mkdir -p '${REMOTE}/scripts' '${REMOTE}/migrations'"
  scp -q "$ROOT/scripts/apply_migrations.sh" "$HOST:$REMOTE/scripts/apply_migrations.sh"
  ssh "$HOST" "chmod +x '${REMOTE}/scripts/apply_migrations.sh'"
  if [[ -d "$ROOT/migrations" ]]; then
    # Sync migration SQL only (do not delete remote applied history files carelessly:
    # rsync without --delete so old applied files remain for audit).
    rsync -az "$ROOT/migrations/" "$HOST:$REMOTE/migrations/"
  fi
  ssh "$HOST" "set -euo pipefail
    set -a
    # shellcheck disable=SC1090
    source '${REMOTE_ENV}'
    set +a
    cd '${REMOTE}'
    bash scripts/apply_migrations.sh
  "
}

# --- main ---
need_reload=0
need_restart=0
ENV_CHANGED=0

if [[ "$ENV_ONLY" -eq 1 ]]; then
  sync_env
  if [[ "$ENV_CHANGED" -eq 1 ]]; then
    need_restart=1
  else
    echo "env unchanged → no service bounce"
  fi
elif [[ ${#PATHS[@]} -gt 0 ]]; then
  PATHS_PY_CHANGED=0
  sync_paths
  if [[ "$SKIP_ENV" -eq 0 ]]; then
    sync_env
  fi
  if [[ "$ENV_CHANGED" -eq 1 ]]; then
    need_restart=1
  elif [[ "$PATHS_PY_CHANGED" -eq 1 ]]; then
    need_reload=1
  else
    echo "static/config only → services stay up (files read from disk)"
  fi
else
  sync_full
  if [[ "$SKIP_ENV" -eq 0 ]]; then
    sync_env
  fi
  if [[ "$ENV_CHANGED" -eq 1 ]]; then
    need_restart=1
  else
    need_reload=1
  fi
fi

run_remote_migrations

if [[ "$NO_RELOAD" -eq 1 ]]; then
  echo "--no-reload: skip service bounce"
elif [[ "$need_restart" -eq 1 ]]; then
  rolling_restart
elif [[ "$need_reload" -eq 1 ]]; then
  rolling_reload
fi

nginx_probe
echo "deploy ok"
