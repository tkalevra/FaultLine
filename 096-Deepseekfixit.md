# 096 — DeepSeek Fixit: Ingestion Pipeline Audit & Remediation

## Problem

User-submitted facts were failing to persist in PostgreSQL. The ingestion pipeline had multiple silent failure points that dropped facts without surfacing errors.

## Diagnosis

Full pipeline trace from `/ingest` entry to PostgreSQL commit identified **four failure vectors**:

### 1. Novel type rejection (critical)

`src/wgm/gate.py` `validate_edge()` checks if a `rel_type` exists in the registry (or SEED_ONTOLOGY fallback). Types not found require Qwen LLM approval via `_try_approve_novel_type()`. If Qwen was unreachable or timed out, the function returned `False` and the edge got `status = "novel"` — **silently dropped, never stored**.

Types missing from the database but referenced throughout the codebase:
- `has_pet` — `_CLASS_B_REL_TYPES`, `_infer_category()`, migration 013
- `lives_at` — `_SENSITIVE_RELS`, `_BASELINE_RELS`, migrations 007/010/013
- `located_at` — migration 007
- `height` — `_SCALAR_REL_TYPES`, `_CLASS_A_REL_TYPES`, migration 016
- `weight` — `_SCALAR_REL_TYPES`, `_CLASS_A_REL_TYPES`, migration 016

### 2. `staged_facts` UUID column mismatch (critical)

Migration 012 defined `staged_facts` with `user_id UUID`, `subject_id UUID`, `object_id UUID` — but the `facts` table uses `TEXT` for all three. `_commit_staged()` in `main.py` inserted string values. PostgreSQL rejected UUID-incompatible strings (e.g. `"anonymous"`), and the bare `except` in `_commit_staged()` swallowed the error, returning 0. All Class B and C facts were lost.

### 3. EntityRegistry crash on non-UUID user_id

`_make_surrogate()` called `uuid.UUID(user_id)`, which raises `ValueError` for non-UUID strings like `"anonymous"`. The `IngestRequest` model defaults `user_id` to `"anonymous"` — any direct API call without an OpenWebUI context would 500.

### 4. `allowed_head`/`allowed_tail` → `head_types`/`tail_types` rename never migrated

Migration 005 created `allowed_head`/`allowed_tail`. Migration 016 references `head_types`/`tail_types`. `gate.py` queries `head_types`/`tail_types`. No rename migration existed — migration 016 would fail, and the gate fell back to SEED_ONTOLOGY with all constraints disabled. A hidden safety net, but meant type constraints were never enforced.

### Contributing issues

- `RelTypeRegistry` had no `get()` method — `hasattr` check in the ingest endpoint always returned `False`, so `is_engine_generated` was never detected
- `_commit_staged()` logged failures at `warning` level via `structlog`, often invisible
- Hard-coded GLiNER2 constraint strings in `/extract` and `/ingest` were incomplete and duplicated

## Changes

### New file

**`migrations/017_fix_schema_consistency.sql`**
- Renames `allowed_head` → `head_types`, `allowed_tail` → `tail_types` (idempotent, handles both old and new column names via `information_schema` checks)
- Seeds 5 missing rel_types (`lives_at`, `located_at`, `has_pet`, `height`, `weight`) with categories, type constraints, and `correction_behavior`
- Backfills `NULL` categories for any engine-generated types
- Alters `staged_facts.user_id`, `subject_id`, `object_id` from `UUID` → `TEXT`
- Updates `promote_staged_fact()` stored procedure to remove unnecessary `::text` casts

### Modified files

**`src/wgm/gate.py`**
- `RelTypeRegistry.get(rel_type)` — new method returning full ontology entry; fixes `is_engine_generated` detection in the ingest endpoint
- `_auto_approve_novel_type(rel_type)` — new method: inserts novel types into `rel_types` as `engine_generated` with `confidence=0.7` when LLM validation is unavailable
- `_try_approve_novel_type()` — calls `_auto_approve_novel_type()` instead of returning `False` when Qwen URL is unset or the HTTP call fails. Explicit Qwen rejection (valid=false or confidence < 0.7) still returns `False` — only infrastructure failure triggers auto-approval

**`src/entity_registry/registry.py`**
- `_make_surrogate()` — handles non-UUID `user_id` values by falling back to a UUID v5 derived from a stable DNS namespace + the user_id string. No longer crashes on `"anonymous"`

**`src/api/main.py`**
- `_get_constraint()` — new function: returns the GLiNER2 constraint from the startup cache, rebuilds from DB if empty, uses emergency fallback only as last resort
- `_EMERGENCY_CONSTRAINT` — single consolidated string with all known types, replaces 3 different incomplete inline strings
- `/extract` and `/ingest` endpoints — use `_get_constraint()` instead of `_rel_type_constraint or "hardcoded_string"`
- `_commit_staged()` — error log upgraded to `error` level with row count, fact class, and first-row context for debugging

**`tests/wgm/test_gate.py`**
- `test_validate_novel_type` — updated to expect `{"status": "valid"}` (auto-approval)
- `test_novel_type_qwen_timeout` — updated to expect `True` (auto-approve on timeout)

## Design principle

The database `rel_types` table is the authoritative source for valid relationship types. SEED_ONTOLOGY in `gate.py` is strictly an emergency fallback for when the DB is completely unreachable. Novel types are auto-registered in the DB when LLM validation is unavailable. The Class C → expiry-after-30-days pipeline provides a natural safety net: bad auto-approved types produce ephemeral facts that expire without confirmation.

## Test results

```
86 passed, 5 skipped, 3 failed
```

All 3 failures are pre-existing and unrelated to these changes:
- `test_correction_hard_delete_migrates_facts` — mock cursor sequence mismatch
- `test_correction_supersede_marks_old_fact` — mock cursor sequence mismatch
- `test_poll_cycle_integration` — `mock_logger.info.call_count` assertion

Zero regressions.

---

## Phase 2 — Recall Pipeline: Fixes After Live Deployment

After deploying the ingestion fixes, live testing revealed facts were being stored but **not recalled** — the model couldn't answer "What's my name?" after the user stated it.

### Root cause analysis from live logs

Four independent recall failures were identified:

#### Recall Failure 1: Entity ID mismatch — `canonical_identity` never resolves

**Ingest path**: `registry.resolve(req.user_id, "user")` returns `req.user_id` (UUID). Alias stored with `entity_id = <UUID>`.

**Query path**: The `/query` endpoint preferred `id = 'user'` (literal string) over the UUID when resolving the user entity. `registry.get_preferred_name(user_id, "user")` looked for aliases under `entity_id = "user"` — but the alias was stored under `entity_id = <UUID>`. Mismatch.

**Effect**: `canonical_identity` stayed `"user"` forever. The memory block always said `The user is 'user'` instead of `The user is 'Chris'`.

#### Recall Failure 2: Identity facts excluded from PostgreSQL queries

`also_known_as` and `pref_name` were excluded from both `_BASELINE_RELS` and the graph traversal query (`rel_type NOT IN ('also_known_as', 'pref_name')`). These identity-critical facts only surfaced via Qdrant vector search — which has a 10-second sync delay (longer with the broken immediate sync). On a fresh deploy or after cache bust, identity facts were invisible.

#### Recall Failure 3: MAX_MEMORY_SENTENCES truncation buried critical facts

The 10-sentence limit was consumed by "Always call X by 'Y'" directives for UUID-format value strings (`156 cedar st s, kitchener, on`, `systems analyst`, `it generalist`). Family relationships and identity facts landed in the truncated tail.

#### Recall Failure 4: Broken immediate Qdrant sync

The `from src.re_embedder.embedder import ReEmbedder` import referenced a class that never existed (`ReEmbedder`). The immediate sync after Class A commit always failed silently. Facts took 10+ seconds to appear in Qdrant.

### Recall fixes applied

**`src/api/main.py`**
- **Entity ID resolution**: Use user's UUID as the authoritative `user_entity_id` (not the legacy `"user"` literal). Fall back to `"user"` only if the UUID lookup returns no preferred name.
- **Identity facts in baseline**: Added `also_known_as` and `pref_name` to `_BASELINE_RELS` so identity facts are always fetched from PostgreSQL — never dependent on Qdrant sync.
- **Removed identity exclusion from graph traversal**: The `rel_type NOT IN ('also_known_as', 'pref_name')` filter was removed from both the direct-facts and 2-hop queries.
- **Immediate Qdrant sync**: Replaced the broken `ReEmbedder` import with direct calls to `embed_text()`, `upsert_to_qdrant()`, and `mark_synced()` — module-level functions that actually exist.

**`openwebui/faultline_tool.py`**
- **Filtered preferred_names**: UUID-format strings, numeric values, and address-like strings are excluded from "Always call X by 'Y'" directives, preventing them from wasting sentence slots.
- **Deferred name directives**: Preferred name directives are now appended AFTER identity and family facts, ensuring critical information appears before the sentence limit truncates.

### Test results (Phase 1 + Phase 2)

```
87 passed, 5 skipped, 2 failed
```

Pre-existing failures (unchanged from Phase 1):
- `test_correction_hard_delete_migrates_facts` — mock cursor sequence mismatch
- `test_poll_cycle_integration` — `mock_logger.info.call_count` assertion

One previously-failing test now passes: `test_correction_supersede_marks_old_fact`

Zero regressions from either phase.

---

## Phase 3 — Filter Resilience: Fixes From Live Log Analysis

A fresh deploy log revealed two more failure modes that made the system appear to not retain or recall information:

### Failure 1: Self-feedback guard false-positive on user profile names

The `_DEBUG_SIGNALS` tuple contained `"FaultLine WGM"` which matched the user's OpenWebUI profile name `"FaultLine WGM Test 1.0"`. Every message from this user was dropped by the guard as "self-feedback", including explicit corrections.

The log showed hundreds of:
```
[FaultLine Filter] dropping self-feedback message, text snippet: 'My son Cyrus is an Art Major...'
```

**Fix**: Changed the guard from substring matching on broad terms to exact matching on system-message markers only:
- `"⊢ FaultLine Memory"` — exact memory block header
- `"GLiNER2 has pre-classified"` — exact preflight marker
- `"[FaultLine Filter]"` and `"[FaultLine]"` — only at line start (debug print prefix)

Removed `"FaultLine WGM"` entirely — it was matching user profiles, not debug output.

### Failure 2: `rewrite_to_triples` failing silently on every call

Every call to the Qwen LLM for triple extraction was failing, but the error was swallowed with no detail. The `except Exception as e:` branch printed `f"rewrite_to_triples failed: {e}"` but `e` was an empty string (likely `httpx.ConnectError` with no message).

With no extracted triples, no edges were sent to `/ingest`, so no facts were stored. The system appeared to have "amnesia" because every user message produced zero triples.

**Fix**: Changed the error log to include `type(e).__name__` so the exception class is always visible, even when `str(e)` is empty.

### Failure 3: No fallback when LLM is unavailable

The entire structured ingestion pipeline depended on the Qwen LLM being reachable. When it wasn't, even explicit self-identification like `"My name is Christopher, I prefer to be called Chris"` produced zero facts — the identity was silently lost.

**Fix**: Added `_extract_basic_facts(text)` — a lightweight regex-based fallback that runs when `rewrite_to_triples` returns empty. It extracts:
- Identity signals: `"my name is X"`, `"I am X"`, `"I'm X"`, `"I(X) am"`, `"call me X"`
- Preference signals: `"prefer to be called X"`, `"goes by X"`, `"preferred name is X"`, `"please call me X"`
- Correction signals: `"actually"`, `"wrong"`, `"incorrect"`, `"innacurate"`, `"update"`

These basic facts are formatted as edge dicts and sent to `/ingest`, ensuring identity and preference facts are never silently dropped even when the LLM backend is completely unreachable.

### Files changed (Phase 3)

**`openwebui/faultline_tool.py`** — 4 edits
- Self-feedback guard narrowed to exact markers only
- `rewrite_to_triples` error now includes exception type name
- `_extract_basic_facts()` — new function for regex-based fallback extraction
- Inlet fallback: when LLM returns no triples, calls `_extract_basic_facts()` before giving up

### Test results (Phase 1 + 2 + 3)

```
87 passed, 5 skipped, 2 failed
```

Same 2 pre-existing failures. Zero regressions across all three phases.
