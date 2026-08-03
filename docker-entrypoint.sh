#!/bin/bash
set -e

# If a custom command is passed as args (e.g., MCP server), exec it directly
# without running migrations or starting the main backend.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

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

# ── DATABASE LOCALE GUARD ─────────────────────────────────────────────────────
# A database's COLLATION is fixed at initdb and CANNOT be changed afterwards without a
# dump/restore. So a non-English install that lands on a database created with the English
# default silently carries the wrong sort order forever — the exact "bites you later" case.
#
# The language chosen at install time sets FAULTLINE_DB_ICU_LOCALE (e.g. es-ES) and the
# matching POSTGRES_INITDB_ARGS, so a FRESH database is created correctly. This guard covers
# the other half: an EXISTING database (a reused Postgres volume, or a language switch on a
# stack that already ran) whose collation does not match. It refuses to start rather than
# quietly accept it.
#
# ENGLISH IS A NO-OP: FAULTLINE_DB_ICU_LOCALE is empty for English, so this whole block is
# skipped — not a single query is issued and startup is byte-for-byte what it always was.
if [ -n "${FAULTLINE_DB_ICU_LOCALE:-}" ]; then
  # daticulocale (PG15/16) was renamed datlocale (PG17+) — try both, tolerate neither.
  DB_LOCALE_ROW=$(psql "${POSTGRES_DSN}" -tAc \
      "SELECT datlocprovider::text || '|' || coalesce(daticulocale, '') FROM pg_database WHERE datname = current_database()" 2>/dev/null) \
    || DB_LOCALE_ROW=""
  if [ -z "$DB_LOCALE_ROW" ]; then
    DB_LOCALE_ROW=$(psql "${POSTGRES_DSN}" -tAc \
        "SELECT datlocprovider::text || '|' || coalesce(datlocale, '') FROM pg_database WHERE datname = current_database()" 2>/dev/null) \
      || DB_LOCALE_ROW=""
  fi

  if [ -z "$DB_LOCALE_ROW" ]; then
    # Could not read the catalog (permissions / unexpected server version). Say so LOUDLY,
    # but do not block startup on a check that could not run.
    echo "WARNING: could not read this database's locale provider — expected ICU '${FAULTLINE_DB_ICU_LOCALE}'."
    echo "         Verify manually:  SELECT datlocprovider, daticulocale FROM pg_database;"
  else
    DB_PROVIDER=${DB_LOCALE_ROW%%|*}
    DB_ICU=${DB_LOCALE_ROW#*|}
    # Normalize both sides: ICU accepts es-ES / es_ES / ES-es as the same locale.
    _norm() { echo "$1" | tr 'A-Z_' 'a-z-'; }
    WANT=$(_norm "${FAULTLINE_DB_ICU_LOCALE}")
    GOT=$(_norm "${DB_ICU}")

    if [ "$DB_PROVIDER" = "i" ] && [ "$WANT" = "$GOT" ]; then
      echo "Database locale OK — ICU '${DB_ICU}' (language: ${FAULTLINE_LANGUAGE:-?})"
    elif [ "${FAULTLINE_ALLOW_DB_LOCALE_MISMATCH:-}" = "true" ]; then
      echo "=================================================================="
      echo "WARNING: DATABASE COLLATION MISMATCH — running anyway by request."
      echo "  expected: ICU '${FAULTLINE_DB_ICU_LOCALE}'   actual: provider='${DB_PROVIDER}' locale='${DB_ICU}'"
      echo "  FAULTLINE_ALLOW_DB_LOCALE_MISMATCH=true is set, so startup continues."
      echo "  Text sorts (ORDER BY, range scans, unique-index ordering) will use the"
      echo "  WRONG language's rules. Storage and case-folding are unaffected."
      echo "=================================================================="
    else
      echo "=================================================================="
      echo "FATAL: DATABASE COLLATION DOES NOT MATCH THE INSTALL LANGUAGE."
      echo ""
      echo "  install language : ${FAULTLINE_LANGUAGE:-?}"
      echo "  expected collation: ICU '${FAULTLINE_DB_ICU_LOCALE}'"
      echo "  actual collation  : provider='${DB_PROVIDER}' locale='${DB_ICU:-<none>}'"
      echo ""
      echo "  This database was created with a different (probably the default English)"
      echo "  collation. A collation CANNOT be changed after the database is created —"
      echo "  it is fixed by initdb. Continuing would give you permanently wrong text"
      echo "  ordering for this language."
      echo ""
      echo "  Your options:"
      echo ""
      echo "   1. RECREATE the database volume (DESTROYS all stored memory):"
      echo "        docker compose down"
      echo "        docker volume rm \$(docker volume ls -q | grep postgres_data)"
      echo "        docker compose up -d"
      echo ""
      echo "   2. DUMP and RESTORE into a correctly-collated database (keeps your data):"
      echo "        docker compose exec postgres pg_dumpall -U \$POSTGRES_USER > backup.sql"
      echo "        ...recreate the volume as in (1)..."
      echo "        docker compose exec -T postgres psql -U \$POSTGRES_USER < backup.sql"
      echo ""
      echo "   3. ACCEPT the wrong collation (not recommended) — set in your .env:"
      echo "        FAULTLINE_ALLOW_DB_LOCALE_MISMATCH=true"
      echo ""
      echo "  Nothing has been changed or deleted. Startup is stopping here on purpose."
      echo "=================================================================="
      exit 1
    fi
  fi
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
# NOTE: execution semantics are unchanged (every file runs fully, errors do not
# stop startup — legacy migrations rely on continue-past-error idempotency), but
# ERROR lines are now surfaced and summarized instead of silently swallowed.
# A silently-failed ADD CONSTRAINT here is how dBug-074 happened.
echo "Running migrations..."
MIGRATION_ERRORS=""
for migration in /app/migrations/*.sql; do
  echo "Applying $migration..."
  MIG_OUT=$(psql "${POSTGRES_DSN}" -f "$migration" 2>&1) || true
  echo "$MIG_OUT"
  ERR_COUNT=$(echo "$MIG_OUT" | grep -c 'ERROR:' || true)
  if [ "$ERR_COUNT" -gt 0 ]; then
    echo ">>> $migration produced $ERR_COUNT ERROR line(s) (execution continued)"
    MIGRATION_ERRORS="${MIGRATION_ERRORS}  - ${migration} (${ERR_COUNT} error(s))\n"
  fi
done

if [ -n "$MIGRATION_ERRORS" ]; then
  echo "=================================================================="
  echo "WARNING: migrations produced ERROR lines (startup continues):"
  printf "%b" "$MIGRATION_ERRORS"
  echo "Some errors are expected re-run noise (e.g. duplicate_object on"
  echo "unguarded ADD CONSTRAINT), but a NEW migration appearing here means"
  echo "its schema change may NOT have applied. Inspect before trusting."
  echo "=================================================================="
fi

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
