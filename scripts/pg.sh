#!/usr/bin/env bash
# Local PostgreSQL cluster for the proof of concept.
# Data lives in ./.pgdata; nothing runs as a background service.
#   scripts/pg.sh init | start | stop | reset | psql
set -euo pipefail
# Postgres on macOS refuses to start without a valid LC_ALL.
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PGBIN="${PGBIN:-$(brew --prefix postgresql@17)/bin}"
DATA="$ROOT/.pgdata"
PORT="${NEREUS_PGPORT:-5439}"
SOCK="$ROOT/.pgsock"

case "${1:-}" in
  init)
    mkdir -p "$SOCK"
    "$PGBIN/initdb" -D "$DATA" -U postgres --auth=trust --encoding=UTF8 --locale=C >/dev/null
    cat >> "$DATA/postgresql.conf" <<EOF
port = $PORT
unix_socket_directories = '$SOCK'
listen_addresses = 'localhost'
shared_buffers = 512MB
work_mem = 64MB
maintenance_work_mem = 512MB
max_wal_size = 4GB
EOF
    "$0" start
    "$PGBIN/createdb" -h "$SOCK" -p "$PORT" -U postgres nereus
    "$PGBIN/psql" -q -h "$SOCK" -p "$PORT" -U postgres -d nereus -v ON_ERROR_STOP=1 \
        -f "$ROOT/sql/001_schema.sql"
    echo "Database ready: postgresql://postgres@localhost:$PORT/nereus"
    ;;
  start)
    if "$PGBIN/pg_ctl" -D "$DATA" status >/dev/null 2>&1; then echo "already running"
    else "$PGBIN/pg_ctl" -D "$DATA" -l "$ROOT/.pg.log" -w start >/dev/null && echo started; fi ;;
  stop)  "$PGBIN/pg_ctl" -D "$DATA" -w stop >/dev/null && echo stopped ;;
  reset)
    "$PGBIN/pg_ctl" -D "$DATA" -w stop >/dev/null 2>&1 || true
    rm -rf "$DATA"
    "$0" init
    ;;
  psql) shift; exec "$PGBIN/psql" -h "$SOCK" -p "$PORT" -U postgres -d nereus "$@" ;;
  *) echo "usage: $0 init|start|stop|reset|psql" >&2; exit 1 ;;
esac
