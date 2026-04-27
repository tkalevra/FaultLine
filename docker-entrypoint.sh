#!/usr/bin/env sh
set -e

echo "[entrypoint] Waiting for PostgreSQL..."
until pg_isready -d "$POSTGRES_DSN" -q; do
  sleep 1
done

echo "[entrypoint] Running migrations..."
psql "$POSTGRES_DSN" -f /app/migrations/001_create_facts.sql

echo "[entrypoint] Starting re_embedder service (background)..."
python -m src.re_embedder.embedder &
REEMBED_PID=$!

echo "[entrypoint] Starting FaultLine WGM service (foreground)..."
exec uvicorn src.api.main:app --host 0.0.0.0 --port 8000
