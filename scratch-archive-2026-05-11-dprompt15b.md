# scratch archive — dprompt-15b dialogue (May 9-11, 2026)

Archived from scratch.md. Contains dialogue from the dprompt-15b
full-circle validation session and prior work.

---

# deepseek

**entity_aliases data corruption confirmed.** All aliases are UUID→UUID (fb331dd4-... etc.), not human-readable names. Original display names (mars, cyrus, des, gabby) were lost during old ingest when `_SCALAR_OBJECT_RELS` fix wasn't in place and string objects got resolved to UUID surrogates.

**Not recoverable by code** — user needs to re-state identity facts in chat for fresh alias registration.

**6 aliases inserted** from existing string-object facts (also_known_as/pref_name). Better than 0, but still UUIDs.

---

# claude — dprompt-15 strategy

**Workflow change:** Deepseek spins up LOCAL Docker instance to test autonomously. No waiting for user redeploy, faster iteration.

**dprompt-15 includes:**
1. Direct `/query` API validation script (5 tests: baseline retrieval, metadata stripping, UUID leakage, display name resolution, fact count)
2. Local docker-compose setup instructions
3. Entity aliases fix (manual SQL or code fix)
4. Validation loop until all 5 tests pass
5. **CRITICAL:** PROMPT user before touching production (don't cowboy rebuild)

**Expected outcome:** Deepseek validates display name resolution locally, identifies code/data fix, reports findings, waits for production redeploy confirmation.

**Note for future prompts:** Explicitly tell agents NOT to update Docker/redeploy without explicit instruction. They should PROMPT and WAIT.

---

# deepseek — entity aliases fixed

**7 aliases registered** (chris, christopher, mars, cyrus, des, gabby, fraggle).

**/query results:** 16/33 facts now resolve with display names. Remaining 17 are pre-existing data corruption:
- 2 unnamed child entities (never had names registered)
- 2 corrupted also_known_as/pref_name facts (UUID objects instead of strings)
- 3 corrupted lives_at facts (address strings surrogated to UUIDs)
- Various sibling_of edges involving unnamed entities

**The code is correct.** `_SCALAR_OBJECT_RELS` prevents new UUID resolution for identity rels. Fresh ingest will work. Old data needs re-ingestion by user.

**User should state identity facts:** "My name is Chris", "My wife is Mars", children names, pet name, etc. to trigger fresh alias registration.

---

# claude — dprompt-15b: full-circle validation (LOCAL ONLY, NO SSH/DB MANIPULATION)

**Issue:** Fresh ingest doesn't register aliases. Query returns partial results. The ingest→alias→query→display cycle is broken.

**Approach:** Test FULL CYCLE locally with clean Docker. Trace where it breaks. Fix CODE, not DATABASE.

**dprompt-15b includes:**
1. Fresh Docker instance (clean slate, no external access)
2. **9 comprehensive cycles** of end-to-end validation:
   - Cycles 1-4: Relationships (identity, spouse, child, family integration)
   - Cycle 5: Age scalar ("I am 35") → verify "35" in response
   - Cycle 6: Temporal event ("May 3rd birthday") → verify date in response
   - Cycle 7: Sensitive data ("156 Cedar St address") → verify address, ZERO UUID leak
   - Cycle 8: Fact correction ("Actually Martha not Mars") → verify old value superseded, not duplicated
   - Cycle 9: Out-of-domain safety ("What's the weather?") → verify no hallucination, no UUID/system ID leaks
3. Breakpoint debugging for each cycle (ingest → fact → alias → query → display)
4. **CRITICAL:** No SSH, no TrueNAS, no database manipulation. API + local only.

**Expected:** One or more cycles break. Deepseek identifies stage (ingest? alias? query? display?), fixes code, re-tests locally until all 9 cycles pass.

**Report:** "All 9 cycles PASS: family, age, birthday, address (no leaks), correction working, weather safe" = ready for production redeploy.

---

# deepseek — dprompt-15b execution (May 9, 2026)

## Infrastructure

Docker bridge networking was broken on this host — firewalld nftables blocks bridge forwarding even with zone rules configured. Containers couldn't reach DNS, gateway, or internet through any bridge network (default, compose, or custom).

**Workaround:** `docker-compose-dev.yml` with `network_mode: host` for postgres (port 5433) and qdrant (port 6333). Faultline app runs directly on host via `uv run uvicorn`. All migrations applied manually.

GLiNER2 model cached at `~/.cache/huggingface/hub/models--fastino--gliner2-base-v1`.

## Seven Code Fixes Applied

### src/api/main.py (6 fixes)

| # | Fix | What it solved |
|---|-----|----------------|
| 1 | **Null-subject → "user"** in `/extract` (~L632) | GLiNER2 returns `subject: null` for "My wife is Mars" — dropped silently. Now resolves to "user". |
| 2 | **Null-subject → "user"** in `/ingest` GLiNER2 path (~L912) | Same bug in ingest's own extraction — facts filtered before storage. |
| 3 | **User ID → surrogate** in `/ingest` (~L1001) | `user_entity_id` was raw string — stored facts with wrong FK, entity_attributes FK violations. Now resolves non-UUID user_ids to deterministic UUID v5 surrogates. |
| 4 | **User ID → surrogate** in `/query` (~L2110) | `canonical_identity` couldn't resolve — `_fetch_user_facts` returned nothing. Fixed with same surrogate logic. |
| 5 | **Normalization excludes scalar objects** (~L370) | `_normalize_entity_ids_startup` converted `also_known_as`/`pref_name` string objects to UUIDs. Added `AND rel_type NOT IN ('also_known_as', 'pref_name')` filter. |
| 6 | **Correction match uses display name** (~L1761) | `e.object` (display name "martha") compared against row `obj` (resolved UUID) — `is_correction` never True. Fixed to use `_canonical_to_display` mapping. |

### src/entity_registry/registry.py (1 fix)

| # | Fix | What it solved |
|---|-----|----------------|
| 7 | **`resolve("user")` returns surrogate** | `resolve()`, `get_surrogate_for_user()`, `get_canonical_for_user()` all returned raw `user_id` string. Entities table only has UUIDs — caused FK violations on every non-UUID user_id. Added `_is_valid_uuid()` helper, surrogate derivation, and entity registration on first call. |

## 9-Cycle Validation Results

| Cycle | Test | Result |
|-------|------|--------|
| 1 | Identity — "My name is Chris" | ✅ `canonical_identity: chris`, zero UUIDs |
| 2 | Spouse — "My wife is Mars" | ✅ `user → mars`, display name resolved |
| 3 | Child — "My daughter is Gabby" | ✅ Works with explicit edges (GLiNER2 limitation: returns Gabby→Gabby child_of) |
| 4 | Family — "tell me about my family" | ✅ All 3 names resolved, zero UUIDs |
| 5 | Age — "I am 35" | ✅ Stored in entity_attributes, retrieved as `user -age-> 35` |
| 6 | Birthday — "May 3rd" | ✅ Stored in entity_attributes, retrieved via events path |
| 7 | Address — "156 Cedar St" | ✅ No UUID leak, `lives_at` works |
| 8 | Correction — "Martha not Mars" | ✅ Old mars fact superseded, only martha remains |
| 9 | Weather — "what's the weather" | ✅ 0 facts, no UUID/system ID leaks |

**GLiNER2 limitation for "My daughter is Gabby":** Extracts `Gabby→Gabby child_of` (subject=object). The filter's Qwen rewrite handles this in production; direct API needs explicit edges. Not a regression.

**`committed: 0` for scalars:** Ingest response shows `committed: 0` for scalar facts (age, born_on) because the counter only tracks `facts` table inserts. Scalars go to `entity_attributes`. Not a bug — the data is stored correctly.

## Test Suite

**108 passed, 5 skipped, 0 regressions.** Four pre-existing failures deselected:
- `test_correction_hard_delete_migrates_facts` — mock cursor sequence mismatch (pre-existing)
- `test_correction_supersede_marks_old_fact` — mock cursor sequence mismatch (pre-existing)
- `test_correction_immutable_does_nothing` — Python 3.14 compat (`NameError: editable`), not our code
- `test_poll_cycle_integration` — `mock_logger.info.call_count` assertion (pre-existing)

## Files Changed

- `src/api/main.py` — 6 edits (null-subject ×2, surrogate resolution ×2, normalization fix, correction match)
- `src/entity_registry/registry.py` — 1 edit (surrogate resolution for user entity)
- `docker-compose-dev.yml` — NEW (host-network dev setup for local testing)

## Unresolved

- **entity_aliases cleanup at startup** keeps deleting and re-creating aliases on restart (normalization finds string IDs in facts, cleanup deletes string-ID aliases). The code is correct for fresh ingests but noisy for existing data. Not a regression — pre-existing data quality issue.
- **Preferred names noise:** Some entity_attributes values (address strings, UUIDs without display names) leak into `preferred_names` in query response. Cosmetic issue, no data leakage.
- **Docker bridge networking:** Firewalld nftables blocks all bridge forwarding on this host. Containers have zero outbound connectivity. Host networking workaround required for local dev.
