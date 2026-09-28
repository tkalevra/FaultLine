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
# Signal preservation (2026-08-19): a warning that fires on EVERY boot teaches
# everyone to ignore it. Twelve numbers are duplicated across the backlog; those
# are disjoint-table idempotent pairs that execute safely by glob order (no
# applied-migrations ledger; every file runs). The case this check exists to
# catch is two migrations modifying the SAME table. So: count benign
# number-collisions informationally, WARN only on same-table collisions.
echo "Validating migration files..."
MIGRATION_NUMBERS=$(ls /app/migrations/*.sql 2>/dev/null | sed 's/^.*\///; s/_.*\.sql$//' | sort)
DUPLICATES=$(echo "$MIGRATION_NUMBERS" | uniq -d)
if [ -n "$DUPLICATES" ]; then
  BENIGN_COUNT=$(echo "$DUPLICATES" | wc -l | tr -d ' ')
  echo "INFO: $BENIGN_COUNT duplicate migration numbers (disjoint-table pairs execute safely by glob order)"
  for NUM in $DUPLICATES; do
    FILE_COUNT=$(ls /app/migrations/${NUM}_*.sql 2>/dev/null | wc -l | tr -d ' ')
    UNIQUE_TABLES=$(cat /app/migrations/${NUM}_*.sql 2>/dev/null \
      | grep -oiE '(ALTER TABLE|CREATE TABLE( IF NOT EXISTS)?)\s+(IF EXISTS\s+)?[a-z_.]+' \
      | grep -oiE '[a-z_.]+$' | sort -u)
    # `|| true` is LOAD-BEARING: `grep -c .` exits 1 on EMPTY input (a duplicate
    # number whose files touch no ALTER/CREATE TABLE — DO-block migrations), and
    # under `set -e` that killed the entrypoint at line 133's assignment on the
    # first boot of this check: the container crash-looped between "PostgreSQL
    # is up" and "Migration validation complete" with no error line. Measured
    # live on pre-prod 2026-08-19 (migration 031, empty UNIQUE_TABLES).
    TABLE_COUNT=$(echo "$UNIQUE_TABLES" | grep -c . || true)
    TABLE_COUNT=${TABLE_COUNT:-0}
    if [ "$FILE_COUNT" -gt 1 ] && [ "$TABLE_COUNT" -gt 0 ] && [ "$TABLE_COUNT" -lt "$FILE_COUNT" ]; then
      echo "WARNING: migration $NUM: multiple files touch the SAME table(s): $(echo $UNIQUE_TABLES | tr '\n' ' ')"
      echo "Note: same-table duplicate migrations can race or conflict — review before relying on them."
    fi
  done
fi
echo "Migration validation complete"

# Run migrations
#
# GATED BY A PER-SCHEMA APPLIED-ONCE LEDGER (2026-08-27).
# This loop used to run EVERY migration on EVERY boot. Measured on a 9-tenant database that
# cost 3,978 row-writes into EXISTING tenant schemas per start (ins=738 upd=3106 del=134) —
# re-applying, on every deploy, migrations that finished months ago. 95 of the files fan out
# into every tenant schema and a dozen write inside that fan-out.
#
# src/provisioning/boot_migrations.py now checks public.schema_migration_status first and runs
# only what is not already applied at the file's CURRENT checksum (so a MODIFIED file is
# detected and re-applied, never silently skipped). It still executes each file with the same
# `psql -f`, in the same order, and still CONTINUES PAST ERRORS and prints the same end-of-run
# ERROR summary — a silently-failed ADD CONSTRAINT is how dBug-074 happened, and that reporting
# is deliberately preserved.
#
# FAIL-SAFE: if the ledger cannot be read, it runs EVERYTHING (this loop's old behaviour) and
# says so loudly. Over-application is bounded and survivable; a silently skipped migration is
# not — see the module docstring for the full argument.
#
# ROLLBACK LEVER: FAULTLINE_MIGRATION_LEDGER=false restores the legacy every-file sweep.
echo "Running migrations..."
if ! FAULTLINE_MIGRATIONS_DIR=/app/migrations python -m src.provisioning.boot_migrations; then
  # The gate never exits non-zero for a migration failure (startup must continue, as before).
  # Reaching here means the gate ITSELF could not run — fall back to the legacy sweep rather
  # than starting with migrations unapplied.
  echo "=================================================================="
  echo "WARNING: the migration gate could not run. Falling back to the legacy"
  echo "         every-file sweep so nothing is left unapplied."
  echo "=================================================================="
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
