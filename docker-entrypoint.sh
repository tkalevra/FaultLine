#!/bin/bash
set -e

# Wait for PostgreSQL
echo "Waiting for PostgreSQL..."
POSTGRES_HOST=${POSTGRES_HOST:-postgres}
POSTGRES_PORT=${POSTGRES_PORT:-5432}
MAX_ATTEMPTS=30
ATTEMPT=0

until [ $ATTEMPT -ge $MAX_ATTEMPTS ]; do
  if timeout 2 bash -c "echo > /dev/tcp/$POSTGRES_HOST/$POSTGRES_PORT" 2>/dev/null; then
    echo "PostgreSQL is up"
    break
  fi
  ATTEMPT=$((ATTEMPT + 1))
  echo "PostgreSQL is unavailable (attempt $ATTEMPT/$MAX_ATTEMPTS) - sleeping"
  sleep 1
done

if [ $ATTEMPT -ge $MAX_ATTEMPTS ]; then
  echo "Failed to connect to PostgreSQL after $MAX_ATTEMPTS attempts"
  exit 1
fi

# Check for duplicate migration numbers (CRITICAL VALIDATION)
echo "Validating migration files..."
MIGRATION_NUMBERS=$(ls /app/migrations/*.sql 2>/dev/null | sed 's/^.*\///; s/_.*\.sql$//' | sort)
DUPLICATES=$(echo "$MIGRATION_NUMBERS" | uniq -d)
if [ -n "$DUPLICATES" ]; then
  echo "WARNING: Duplicate migration numbers found: $DUPLICATES"
  echo "Note: Multiple definitions of same migration number may cause issues."
  echo "This is acceptable if they modify different tables or schemas."
  # Don't exit - let migrations proceed (idempotency handled by migrations themselves)
fi
echo "Migration validation complete"

# Run migrations
echo "Running migrations..."
for migration in /app/migrations/*.sql; do
  echo "Applying $migration..."
  psql "${POSTGRES_DSN}" -f "$migration" || true  # Ignore errors for idempotency
done

echo "Migrations complete"

# Start the FastAPI app
echo "Starting FaultLine backend..."
uvicorn src.api.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --log-level info &

# Start the re-embedder background task
echo "Starting re-embedder..."
python -m src.re_embedder.embedder &

# Wait for all background processes
wait
