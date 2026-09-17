#!/usr/bin/env bash
# Apply pending SQL files under migrations/ (idempotent).
#
# Requires AUTOSELF_MYSQL_* in the environment (from .env / .env.prod / systemd).
#
# Local:
#   set -a && source .env && set +a && ./scripts/apply_migrations.sh
#
# Files: migrations/*.sql  (sorted by name). Already-applied names are skipped
# via table schema_migrations.
#
#   --dry-run   print pending only
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MIG_DIR="${AUTOSELF_MIGRATIONS_DIR:-$ROOT/migrations}"
DRY_RUN=0

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

HOST="${AUTOSELF_MYSQL_HOST:-127.0.0.1}"
PORT="${AUTOSELF_MYSQL_PORT:-3306}"
USER="${AUTOSELF_MYSQL_USER:-root}"
PASS="${AUTOSELF_MYSQL_PASSWORD:-}"
DB="${AUTOSELF_MYSQL_DB:-autoself}"

if [[ ! -d "$MIG_DIR" ]]; then
  echo "no migrations dir: $MIG_DIR (nothing to do)"
  exit 0
fi

mysql_cmd() {
  local cnf rc
  cnf="$(mktemp)"
  chmod 600 "$cnf"
  cat >"$cnf" <<EOF
[client]
host=${HOST}
port=${PORT}
user=${USER}
password=${PASS}
database=${DB}
EOF
  set +e
  mysql --defaults-extra-file="$cnf" "$@"
  rc=$?
  set -e
  rm -f "$cnf"
  return "$rc"
}

sql_quote() {
  # escape single quotes for SQL string literals
  printf "%s" "$1" | sed "s/'/''/g"
}

echo "migrations → ${USER}@${HOST}:${PORT}/${DB}"

mysql_cmd -e "
CREATE TABLE IF NOT EXISTS schema_migrations (
  id VARCHAR(255) NOT NULL PRIMARY KEY,
  applied_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"

applied="$(mysql_cmd -N -e "SELECT id FROM schema_migrations" | tr '\n' ' ')"
pending=0

shopt -s nullglob
files=("$MIG_DIR"/*.sql)
if [[ ${#files[@]} -eq 0 ]]; then
  echo "no *.sql in $MIG_DIR"
  exit 0
fi

IFS=$'\n' sorted=($(printf '%s\n' "${files[@]}" | sort)); unset IFS

for f in "${sorted[@]}"; do
  name="$(basename "$f")"
  if [[ " $applied " == *" $name "* ]]; then
    echo "  skip  $name"
    continue
  fi
  pending=$((pending + 1))
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  pending  $name"
    continue
  fi
  echo "  apply $name"
  mysql_cmd <"$f"
  mysql_cmd -e "INSERT INTO schema_migrations (id) VALUES ('$(sql_quote "$name")')"
done

if [[ "$pending" -eq 0 ]]; then
  echo "migrations up to date"
elif [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: $pending pending"
else
  echo "applied $pending migration(s)"
fi
