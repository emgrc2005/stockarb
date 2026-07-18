#!/bin/sh
# Container entrypoint: validate configuration, initialize the SQLite schema,
# then hand off to supercronic (PID 1 replacement via exec so signals/SIGTERM
# propagate correctly for graceful `docker stop`).
set -eu

log() {
    printf '%s STARTUP  [entrypoint] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")" "$1"
}

log "StockArb starting up..."

required_vars="ALPACA_API_KEY ALPACA_SECRET_KEY"
missing=""
for var in $required_vars; do
    eval "value=\${$var:-}"
    if [ -z "$value" ]; then
        missing="$missing $var"
    fi
done

if [ -n "$missing" ]; then
    log "FATAL: missing required environment variable(s):$missing"
    log "Copy .env.example to .env and fill in your Alpaca paper-trading credentials."
    exit 1
fi

log "Config OK. DB_PATH=${DB_PATH:-/app/data/stat_arb.db} LOG_LEVEL=${LOG_LEVEL:-INFO} TIMEZONE=${TIMEZONE:-America/New_York}"
log "Initializing SQLite schema (idempotent)..."
python -c "from app.config import get_config; from app.db import init_db; c = get_config(); init_db(c.db_path); print(f'DB ready at {c.db_path}')"

log "Universe: $(python -c 'from app.config import get_config; print(",".join(get_config().universe_tickers))')"
log "Handing off to supercronic with schedule from /app/crontab"

exec supercronic -passthrough-logs /app/crontab
