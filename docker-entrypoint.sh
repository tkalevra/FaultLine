#!/usr/bin/env sh
set -e

echo "[entrypoint] Waiting for PostgreSQL..."
until pg_isready -d "$POSTGRES_DSN" -q; do
  sleep 1
done

echo "[entrypoint] Running migrations..."
for migration in $(ls /app/migrations/*.sql | sort); do
    echo "[entrypoint] Applying $migration..."
    if ! psql "$POSTGRES_DSN" --set ON_ERROR_STOP=on -f "$migration"; then
        echo "[entrypoint] WARNING: $migration had errors (may already be applied) — continuing"
    fi
done
echo "[entrypoint] Migrations complete."

echo "[entrypoint] Starting re_embedder service (background)..."
python -m src.re_embedder.embedder &
REEMBED_PID=$!
echo "[entrypoint] Re-embedder PID: $REEMBED_PID"
sleep 2
if ! kill -0 $REEMBED_PID 2>/dev/null; then
    echo "[entrypoint] ERROR: Re-embedder process died immediately. Check logs above."
    exit 1
fi

echo "[entrypoint] Starting FaultLine WGM service (foreground)..."
exec uvicorn src.api.main:app --host 0.0.0.0 --port 8000
