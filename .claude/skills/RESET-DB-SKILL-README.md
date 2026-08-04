# FaultLine Database Reset Skill

**Status:** ✅ REGISTERED & READY TO USE

## Overview

The `reset-faultline-db` skill provides a complete database reset, migration, and validation workflow for the FaultLine test environment. It:

1. **Clears** all data from `faultline_test` database
2. **Runs** all migrations (001-044) in order
3. **Validates** schema completeness
4. **Validates** rel_types ontology is populated
5. **Validates** entity_taxonomies are seeded
6. **Saves** configuration for future use

**No hardcoded values, no UUIDs in output, all variable expansion from environment.**

## Quick Start

### 1. Set Environment Variables

```bash
# Add to ~/.bashrc or run before skill execution:
export FAULTLINE_URL=http://localhost:8000
export FAULTLINE_TOKEN=sk-YOUR_OPENWEBUI_BEARER_TOKEN
export FAULTLINE_USER_ID=YOUR_OPENWEBUI_USER_UUID
export POSTGRES_DSN=postgresql://faultline:faultline@localhost:5432/faultline_test
export QDRANT_URL=http://localhost:6333
```

### 2. Run the Skill

```bash
/skill reset-faultline-db
```

### 3. Verify Results

```bash
# Check backend is responding
curl -H "Authorization: Bearer $FAULTLINE_TOKEN" $FAULTLINE_URL/health

# Check extraction works
curl -X POST $FAULTLINE_URL/extract/rewrite \
  -H "Authorization: Bearer $FAULTLINE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"My son Des is 12","user_id":"'$FAULTLINE_USER_ID'"}'
```

## Environment Variables

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `FAULTLINE_URL` | Backend API URL | `http://localhost:8000` |
| `FAULTLINE_TOKEN` | OpenWebUI bearer token | `sk-...` |
| `FAULTLINE_USER_ID` | OpenWebUI user UUID | `550e8400-e29b-41d4-a716-446655440000` |
| `POSTGRES_DSN` | PostgreSQL connection string | `postgresql://user:pass@host:5432/faultline_test` |
| `QDRANT_URL` | Qdrant vector DB URL | `http://localhost:6333` |

### How to Find Them

#### FAULTLINE_TOKEN
```bash
# In OpenWebUI Settings → Account → API Key
# Copy the "sk-..." bearer token
```

#### FAULTLINE_USER_ID
```bash
# In OpenWebUI Settings → Account
# Your user UUID is displayed, or check browser dev tools network tab
# POST /api/auth/profile → response.user.id
```

#### POSTGRES_DSN
```bash
# Check docker-compose.yml or environment setup
# Format: postgresql://user:pass@host:port/database_name
# Database MUST be: faultline_test
```

#### QDRANT_URL
```bash
# Check docker-compose.yml for Qdrant service
# Default: http://qdrant:6333 (in-container) or http://localhost:6333 (localhost)
```

## Skill Parameters

### skip_clear=true
Run migrations and validate without clearing existing data.

```bash
/skill reset-faultline-db skip_clear=true
```

### show_config=true
Display saved configuration and exit (no-op).

```bash
/skill reset-faultline-db show_config=true
```

## Expected Output

```
════════════════════════════════════════════════════════════
FaultLine Database Reset, Seed & Validate
════════════════════════════════════════════════════════════

ℹ Loading environment variables...
✓ FAULTLINE_URL=http://localhost:8000
✓ FAULTLINE_TOKEN=sk-...
✓ FAULTLINE_USER_ID=550e8400-...
✓ POSTGRES_DSN=postgresql://...
✓ QDRANT_URL=http://localhost:6333

ℹ Clearing existing database...
✓ Database cleared

ℹ Running migrations...
  [1/44] 001_create_facts.sql
✓   Completed: 001_create_facts.sql
  [2/44] 002_add_fact_weights.sql
✓   Completed: 002_add_fact_weights.sql
  ...
✓ All 44 migrations completed

ℹ Validating database schema...
✓ All 9 required tables present
ℹ Total tables in database: 15

ℹ Validating rel_types ontology...
✓ rel_types populated with 67 relationship types

ℹ Validating entity_taxonomies...
✓ entity_taxonomies populated with 5 taxonomy groups

✓ Config saved to /home/chris/Documents/013-GIT/FaultLine-dev/.claude/skills/.faultline_config.json

════════════════════════════════════════════════════════════
✓ Database reset, seeded, and validated successfully!
════════════════════════════════════════════════════════════
```

## Implementation Details

### Scripts

| File | Purpose |
|------|---------|
| `.claude/skills/reset-faultline-db.yaml` | Skill definition (steps, parameters, documentation) |
| `scripts/reset_seed_validate.py` | Python script that executes the reset/seed/validate workflow |
| `.claude/skills/setup_faultline_env.sh` | Helper script to load environment variables |
| `.claude/skills/.faultline_config.json` | Saved configuration (created after successful run) |

### Execution Flow

1. **Validate env vars** — All required variables present and non-empty
2. **Check psql** — Verify `psql` command-line tool is available
3. **Check SSH** — Verify SSH to truenas works (optional, for docker checks)
4. **Python script** — Execute reset_seed_validate.py with full progress feedback
5. **Docker health** — Check if containers are running (via SSH)
6. **Summary** — Display configuration and next steps

### Configuration Persistence

After successful execution, configuration is saved to:
```
.claude/skills/.faultline_config.json
```

This file contains:
- FAULTLINE_URL
- FAULTLINE_USER_ID
- QDRANT_URL
- Timestamp of last successful run

**Note:** FAULTLINE_TOKEN is NOT saved (security), must be set via environment variable each time.

## Troubleshooting

### "Missing required environment variables"

```bash
# Check which variables are missing
env | grep FAULTLINE
env | grep POSTGRES
env | grep QDRANT

# Set missing variables
export FAULTLINE_URL=...
# etc.
```

### "psql not found"

```bash
# Install PostgreSQL client
sudo dnf install postgresql

# Verify
psql --version
```

### "Failed to clear database: authentication failed"

```bash
# Check POSTGRES_DSN format
echo $POSTGRES_DSN
# Should be: postgresql://user:pass@host:port/dbname

# Test connection manually
psql "$POSTGRES_DSN" -c "SELECT 1"

# If authentication fails:
# 1. Verify PostgreSQL is running
# 2. Check credentials in POSTGRES_DSN
# 3. Ensure database faultline_test exists
```

### "Failed to run migrations: syntax error"

```bash
# Check a migration file for syntax
cat migrations/001_create_facts.sql | head -20

# If you see issues, check:
# 1. All migration files are properly formatted SQL
# 2. No binary/corrupt files in migrations/ directory
# 3. PostgreSQL version compatibility

# View specific migration error
python3 scripts/reset_seed_validate.py 2>&1 | grep -A5 "Failed"
```

### "rel_types table is empty"

This means migrations ran but didn't populate core ontology data. Check:

```bash
# List all migrations with rel_types seeding
grep -l "rel_type" migrations/*.sql

# Verify at least one migration inserts rel_types
grep -l "INSERT INTO rel_types" migrations/*.sql
```

### "SSH to truenas unavailable"

This is a warning, not an error. The skill will continue without docker health checks. To enable:

```bash
# Check SSH key setup
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519

# Copy public key to truenas
ssh-copy-id -i ~/.ssh/id_ed25519.pub truenas

# Test
ssh truenas "echo OK"
```

## Validation Checklist

After running the skill, verify:

- [ ] All 44 migrations completed successfully
- [ ] 9 required tables present (facts, entities, entity_aliases, etc.)
- [ ] rel_types table has 60+ relationship types
- [ ] entity_taxonomies table has 5 core taxonomies
- [ ] Configuration saved to .faultline_config.json
- [ ] Backend /health endpoint responds
- [ ] /extract/rewrite endpoint accepts requests

## Advanced Usage

### Selective Data Preservation

```bash
# Preserve existing data, just rerun migrations
/skill reset-faultline-db skip_clear=true
```

### Manual Database Reset

For manual control, use the Python script directly:

```bash
python3 scripts/reset_seed_validate.py
```

### Check Current Configuration

```bash
# View last saved configuration
cat .claude/skills/.faultline_config.json

# Pretty-print
cat .claude/skills/.faultline_config.json | jq '.'
```

## Security Notes

- **FAULTLINE_TOKEN** is sensitive and NEVER saved to disk
- **POSTGRES_DSN** contains password; use environment variables, not files
- Configuration JSON saved only contains non-sensitive values
- All database operations use authenticated connections

## Related Documentation

- [CLAUDE.md](../../CLAUDE.md) — Architecture and constraints
- [migrations/](../../migrations/) — All database schemas
- [scripts/](../../scripts/) — Other utility scripts
- [.claude/skills/](.) — Other FaultLine skills

## Support

For issues:

1. Check troubleshooting section above
2. Review skill log output (full output printed to console)
3. Manually run: `python3 scripts/reset_seed_validate.py` for detailed errors
4. Check database connectivity: `psql "$POSTGRES_DSN" -c "SELECT 1"`

---

**Skill Created:** 2026-05-26  
**Last Updated:** 2026-05-26  
**Database:** faultline_test  
**Migrations:** 44 total
