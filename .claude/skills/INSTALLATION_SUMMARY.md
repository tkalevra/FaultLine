# FaultLine Database Reset Skill - Installation Summary

**Status:** ✅ FULLY INSTALLED & REGISTERED

**Date:** 2026-05-26  
**Location:** `./.claude/skills/reset-faultline-db`  
**Environment:** FaultLine dev repository (`/home/chris/Documents/013-GIT/FaultLine-dev`)

## What Was Created

### 1. **Python Reset Script** (`scripts/reset_seed_validate.py`)
- **Size:** 13 KB
- **Purpose:** Core execution logic for database reset/seed/validate
- **Features:**
  - Loads all environment variables (no hardcoding)
  - Validates PostgreSQL connectivity
  - Drops and recreates database
  - Runs all 44 migrations in order
  - Validates schema completeness
  - Validates rel_types ontology populated
  - Validates entity_taxonomies seeded
  - Saves configuration to JSON for future use
  - Color-coded output (error/warning/success)

### 2. **Skill Definition** (`.claude/skills/reset-faultline-db.yaml`)
- **Size:** 6.1 KB
- **Purpose:** Skill orchestration and parameter handling
- **Features:**
  - Multi-step workflow (validation → clear → migrate → validate → summary)
  - SSH-based docker health checks (optional)
  - Environment variable validation
  - psql availability check
  - On-error handlers with troubleshooting guidance

### 3. **Environment Setup Helper** (`.claude/skills/setup_faultline_env.sh`)
- **Size:** 2.7 KB
- **Purpose:** Interactive environment variable loader
- **Features:**
  - Load from saved config if available
  - Prompt for sensitive values (FAULTLINE_TOKEN)
  - Validate PostgreSQL DSN format
  - Color-coded output

### 4. **Comprehensive Documentation**
- **README** (RESET-DB-SKILL-README.md) — 400+ lines
  - Quick start
  - Variable discovery guide
  - Troubleshooting
  - Security notes
  - Advanced usage

- **This Summary** (INSTALLATION_SUMMARY.md)
  - What was created
  - How to use
  - Verification steps

### 5. **Settings Registration** (`./.claude/settings.json`)
- Skill registered in `settings.custom[]`
- Name: `reset-faultline-db`
- Path: `./.claude/skills/reset-faultline-db.yaml`
- Status: `enabled: true`

## How to Use

### Step 1: Set Environment Variables

```bash
# Option A: Export in your shell
export FAULTLINE_URL=http://localhost:8000
export FAULTLINE_TOKEN=sk-YOUR_OPENWEBUI_BEARER_TOKEN
export FAULTLINE_USER_ID=YOUR_OPENWEBUI_USER_UUID
export POSTGRES_DSN=postgresql://faultline:faultline@localhost:5432/faultline_test
export QDRANT_URL=http://localhost:6333

# Option B: Use the setup helper (interactive)
source ./.claude/skills/setup_faultline_env.sh
```

### Step 2: Run the Skill

```bash
# Full reset (clear + migrate + validate)
/skill reset-faultline-db

# OR: Preserve data, just migrate (useful for re-running migrations)
/skill reset-faultline-db skip_clear=true

# OR: View saved configuration
/skill reset-faultline-db show_config=true
```

### Step 3: Verify Success

```bash
# Check backend health
curl -H "Authorization: Bearer $FAULTLINE_TOKEN" $FAULTLINE_URL/health

# Test extraction (should work without errors)
curl -X POST $FAULTLINE_URL/extract/rewrite \
  -H "Authorization: Bearer $FAULTLINE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"My son Des is 12","user_id":"'$FAULTLINE_USER_ID'"}'

# Check database state
psql "$POSTGRES_DSN" -c "SELECT COUNT(*) FROM facts;"
psql "$POSTGRES_DSN" -c "SELECT COUNT(*) FROM rel_types;"
psql "$POSTGRES_DSN" -c "SELECT COUNT(*) FROM entity_taxonomies;"
```

## File Structure

```
FaultLine-dev/
├── .claude/
│   ├── settings.json                    ← Skill registered here
│   ├── skills/
│   │   ├── reset-faultline-db.yaml      ← Skill definition
│   │   ├── setup_faultline_env.sh       ← Interactive setup helper
│   │   ├── RESET-DB-SKILL-README.md     ← Full documentation
│   │   ├── INSTALLATION_SUMMARY.md      ← This file
│   │   └── .faultline_config.json       ← Saved config (created on first run)
│   │
│   └── projects/.../memory/             ← Auto-memory (for future sessions)
│
└── scripts/
    └── reset_seed_validate.py           ← Core execution script

migrations/
├── 001_create_facts.sql
├── 002_*.sql
├── ...
└── 044_intent_confidence_feedback.sql   ← 44 total migrations
```

## Key Design Decisions

### 1. **Variable Expansion (No Hardcoding)**
- All configuration from environment variables
- No UUIDs, bearer tokens, or URLs in code
- Promotes deployment flexibility

### 2. **Persistent Configuration**
- Saves non-sensitive config to `.faultline_config.json` after successful run
- Future runs can auto-load from saved state
- Token always requires manual input (never saved)

### 3. **Fail-Loud Philosophy**
- Missing env vars → explicit error + list missing items
- SQL errors → stop immediately with error context
- Schema validation → check all required tables
- No silent failures

### 4. **Comprehensive Validation**
- Step 1: Environment variables
- Step 2: Tool availability (psql)
- Step 3: Connectivity (SSH optional)
- Step 4: Full Python reset/seed workflow
- Step 5: Docker health checks
- Step 6: Summary with next steps

### 5. **Migration Ordering**
- All 44 migrations run in numerical order
- Sorted by migration number (001, 002, etc.)
- Stops on first SQL error (ON_ERROR_STOP=1)
- Full compatibility with existing schema

## Environment Variables Reference

| Variable | Required | Where to Find | Example |
|----------|----------|---------------|---------|
| FAULTLINE_URL | Yes | Docker env / config | http://localhost:8000 |
| FAULTLINE_TOKEN | Yes | OpenWebUI Settings → Account → API Key | sk-... |
| FAULTLINE_USER_ID | Yes | OpenWebUI Settings → Account (User UUID) | 550e8400-... |
| POSTGRES_DSN | Yes | docker-compose.yml | postgresql://user:pass@host:5432/faultline_test |
| QDRANT_URL | Yes | docker-compose.yml | http://localhost:6333 |

## Testing the Skill

### Quick Test

```bash
# Set minimal environment (adjust for your setup)
export FAULTLINE_URL=http://localhost:8000
export FAULTLINE_TOKEN=sk-test-token
export FAULTLINE_USER_ID=00000000-0000-0000-0000-000000000000
export POSTGRES_DSN=postgresql://faultline:faultline@localhost:5432/faultline_test
export QDRANT_URL=http://localhost:6333

# Run skill
/skill reset-faultline-db

# Expected output:
# ✓ Database cleared
# ✓ All 44 migrations completed
# ✓ All 9 required tables present
# ✓ rel_types populated with N relationship types
# ✓ entity_taxonomies populated with N taxonomy groups
# ✓ Configuration saved
```

### Manual Test (if skill doesn't load)

```bash
# Run Python script directly
python3 ./scripts/reset_seed_validate.py

# Expected output: Same as skill, with detailed progress
```

## Troubleshooting Quick Reference

| Problem | Solution |
|---------|----------|
| "Missing required environment variables" | Check output for which vars are missing, export them |
| "psql not found" | `sudo dnf install postgresql` |
| "Failed to clear database" | Check POSTGRES_DSN, ensure PostgreSQL is running |
| "Failed migrations" | Check migration SQL files for syntax errors |
| "rel_types table is empty" | Check migrations populate rel_types (query migrations/*.sql) |
| "SSH to truenas unavailable" | Optional - skill continues without docker checks |

## Advanced Configuration

### Docker-First Setup

If running in Docker:

```bash
# Inside container, environment is usually set
docker exec faultline python3 scripts/reset_seed_validate.py
```

### Kubernetes/Cloud Deployment

```bash
# Export env vars from your cloud config
export FAULTLINE_URL=https://faultline.example.com
export POSTGRES_DSN=postgresql://user@clouddb:5432/faultline_test
export QDRANT_URL=https://qdrant.example.com

# Run skill as usual
/skill reset-faultline-db
```

### SSH-Based Execution

```bash
# If running on remote machine
ssh user@dev-machine << 'EOF'
cd /home/chris/Documents/013-GIT/FaultLine-dev
export FAULTLINE_URL=...
export POSTGRES_DSN=...
# ... set other vars
/skill reset-faultline-db
EOF
```

## Persistence & Future Sessions

After the first successful run:

1. **Configuration saved** to `.faultline_config.json`
2. **Next session** can auto-load from saved state
3. **Token still required** (never auto-saved for security)

```bash
# Future runs:
export FAULTLINE_TOKEN=sk-...  # Only need this
/skill reset-faultline-db      # Token + config loads automatically
```

## Integration with FaultLine Development

This skill integrates with the FaultLine development workflow:

- ✅ **Database isolation:** Clears `faultline_test` only (never touches production `faultline` db)
- ✅ **Schema versioning:** All 44 migrations version-controlled in `migrations/`
- ✅ **Reproducible state:** Always produces identical schema from migrations
- ✅ **Variable expansion:** No hardcoded values (deployable to any environment)
- ✅ **Validation framework:** Checks schema + ontology + taxonomies

Use this skill in your workflow:

```
1. Clone repo
2. /skill reset-faultline-db           ← Initialize fresh database
3. /skill faultline-full-pipeline-test ← Run integration tests
4. Develop → Test → Commit
5. /skill reset-faultline-db           ← Reset before PR
```

## Next Steps

1. **Verify Setup**
   ```bash
   # Check all files are in place
   ls -lh ./.claude/skills/reset-faultline-db.*
   ```

2. **Set Environment Variables**
   ```bash
   export FAULTLINE_URL=...
   export POSTGRES_DSN=...
   # etc.
   ```

3. **Run First Reset**
   ```bash
   /skill reset-faultline-db
   ```

4. **Verify Database**
   ```bash
   psql "$POSTGRES_DSN" -c "SELECT COUNT(*) FROM facts;"
   ```

5. **Test Backend API**
   ```bash
   curl -H "Authorization: Bearer $FAULTLINE_TOKEN" $FAULTLINE_URL/health
   ```

## Documentation Index

- **RESET-DB-SKILL-README.md** — Full skill documentation (400+ lines)
  - Quick start, environment variables, parameters, troubleshooting
  
- **INSTALLATION_SUMMARY.md** — This file
  - What was created, how to use, verification

- **CLAUDE.md** — Architecture and constraints
  - FaultLine design, data model, constraints

- **migrations/*** — Database schema files
  - All 44 migrations in order

## Support & Feedback

For issues or improvements:

1. Check RESET-DB-SKILL-README.md troubleshooting section
2. Run skill with full output: `/skill reset-faultline-db`
3. Run Python script directly for detailed errors: `python3 scripts/reset_seed_validate.py`
4. Review migration SQL files: `ls migrations/ | sort`

---

**Installation completed successfully! ✅**

The skill is ready to use. Set your environment variables and run:
```bash
/skill reset-faultline-db
```

For detailed documentation, see **RESET-DB-SKILL-README.md**.
