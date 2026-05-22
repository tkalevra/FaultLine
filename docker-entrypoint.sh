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
# Export environment variables for subprocess (subshell inherits parent's env but explicit export ensures propagation)
export POSTGRES_DSN QDRANT_URL QDRANT_COLLECTION QWEN_API_URL OPENWEBUI_URL REEMBED_INTERVAL QDRANT_SYNC_CONFIDENCE_THRESHOLD
export PYTHONPATH=/app PYTHONUNBUFFERED=1

# Launch re_embedder with redirected output to aide debugging
python -m src.re_embedder.embedder > /tmp/re_embedder.log 2>&1 &
REEMBED_PID=$!
echo "[entrypoint] Re-embedder PID: $REEMBED_PID"
sleep 2
if ! kill -0 $REEMBED_PID 2>/dev/null; then
    echo "[entrypoint] ERROR: Re-embedder process died immediately. Check /tmp/re_embedder.log:"
    head -30 /tmp/re_embedder.log || echo "Log file not readable"
    exit 1
fi

echo "[entrypoint] Starting FaultLine WGM service (foreground)..."
exec uvicorn src.api.main:app --host 0.0.0.0 --port 8000
