# FOSS-014 — a VERBATIM re-statement after a retraction does not resurrect (dedupe path treats the user's genuine repeat as a replay), while a paraphrased re-statement does

- **Branch matrix:** live-measured on the rebuilt en stack (foss-fix/main @ 557da006) — shared capture-lane behavior, present on es/it by code shape
- **Severity:** Medium (the two re-statement paths disagree; the house doctrine is that a GENUINE user re-statement — fresh call, no replay marker — always resurrects; the verbatim path violates it silently)
- **Discovered:** 2026-10-17, lane C round-3 critic live battery (seat a4444444)

## Symptom

After "Please forget that I have a dog named Rex." retired (user, has_pet, rex):

- Re-stating the IDENTICAL sentence: response committed=2, but NO live has_pet row — the
  turn DEDUPED against the retired row; the store stays retracted.
- Re-stating a PARAPHRASE ("Rex is my dog and he owns me."): the edge RESURRECTED (live row).

Same intent, same authority (user_stated, fresh MCP call, no replay marker), opposite
outcomes. The replay-freeze doctrine (infra replays frozen; genuine user re-statements
resurrect) is implemented such that the verbatim-dedupe path freezes a genuine re-statement.

## Root cause (direction)

The ingest dedupe that keys on the captured edge (or turn content) treats the retired row
as "already known" and skips the re-write instead of un-retiring it. The replay-marker
freeze (X-FL-Deferred-Replay) was designed exactly so that INFRA replays keep retirement
while FRESH ingests resurrect — the verbatim user repeat is falling into the frozen class
without carrying the marker.

## Fix direction (gauntlet work order)

1. The dedupe path must distinguish a fresh user_stated ingest from a marked replay: on
   collision with a retired row (superseded_at set, cause=retract_lane), a fresh
   user_stated edge UN-RETIRES (or re-inserts with fresh provenance) exactly like the
   paraphrase path.
2. Pins: (i) verbatim re-statement after /retract/correct -> live row again;
   (ii) the replay-marker case stays frozen (regression guard on the 2026-09-04 wound);
   (iii) paraphrase resurrection stays (already true).
3. **You are NOT your own critic.** Clean critic re-runs the pair live on both shapes.

## RESOLVED — foss-fix/resurrect @ 4823384f (2026-10-18, FOSS-014 worker)

**Root cause (measured, not just direction).** The ingest Redis idempotency cache is
keyed on the request fingerprint (`text|user|/ingest|memory_facts|is_correction|edges`,
TTL 3600s) — a verbatim re-statement maps to the ORIGINAL turn's key. The cache-hit
validator `main.py::_idempotency_cached_facts_present` checked row existence WITHOUT a
retirement filter "on purpose" (its docstring argued "a correction soft-deletes but keeps
the row → trust the cache, never resurrect a corrected fact"). After `/retract/correct`
soft-retired the row (`superseded_at`, row retained), the re-statement's cache hit saw
the still-existing RETIRED row, trusted the hit, and served the cached `committed=2`
WITHOUT re-writing — the silent freeze. The paraphrase (different text → different key)
missed the cache and took the fresh path, where `store.py::commit`'s CASE already
un-retires (`superseded_at = NULL` on a non-replay upsert) — hence the asymmetry. The
fresh ingest was falling into the frozen class without carrying `X-FL-Deferred-Replay`.

**Fix (two seams, marker discipline preserved).**
1. **Validator (`main.py::_idempotency_cached_facts_present`):** a cache hit is trusted
   ONLY when the rows it vouches for are LIVE — `facts` requires
   `superseded_at/archived_at/deleted_at` NULL, `staged_facts` requires
   `deleted_at/promoted_at` NULL. A retired row and an absent row both BUST the hit and
   the turn re-ingests; the two causes log apart
   (`ingest.idempotency_cache_row_retired` vs `..._row_absent`; call-site reason now
   "absent-or-retired"). The bust reaches the store, where the marker decides.
2. **Staged tombstone (`main.py::_commit_staged`):** the Phase-4 FREEZE TOMBSTONE
   (`WHERE deleted_at IS NULL`) skipped the UPDATE for a deleted row replay-or-not —
   the same silent-freeze class for a `deleted_at`-retired staged row. Now replay-aware:
   `WHERE deleted_at IS NULL OR NOT %s` (marker) + `deleted_at = NULL` in the DO UPDATE —
   a fresh user_stated ingest un-retires (count bump, fresh 30d expiry); a MARKED replay
   still skips the whole UPDATE. The `expires_at` CASE guards keep their replay scope.
3. **Facts side (`store.py::commit`) unchanged** — already fresh-resurrect /
   replay-freeze; the busted cache now actually reaches it.

**Pins** (`tests/test_foss014_verbatim_restatement_resurrect.py`, 11 tests; red-checked
at 68a20854: 8 red / 3 positive controls green at both — facts-side CASE guards, staged
`expires_at` CASE scope, no-schema fail-safe; full-suite failure set otherwise
IDENTICAL to base across 2673 tests):
(i) verbatim re-statement after /retract/correct resurrects (live row, fresh
user_stated provenance, confirmed_count continues);
(ii) marked replay of the same body stays frozen (store-side CASE/WHERE, incl. the
`tests/test_reextract_replay_resurrection.py` and `tests/mcp/test_ingest_retry_dropturn.py`
replay-guard pins — all green);
(iii) paraphrase resurrection stays;
(iv) the episodic reextract path (marked, `X-FL-Replay-Lane: reextract`) stays frozen.

**Live battery** (fl-eval-en dind rebuilt from 4823384f; running-image fingerprint
md5 `ca60def62fc890f7f48f59e2f673ff69` == `git show 4823384f:src/api/main.py`; fresh
seat): remember rex (committed=2) → recall present → retract (superseded=1) → recall
absent → re-state the IDENTICAL sentence → **recall PRESENT (wound healed)**. Api logs
show the exact fix path: same cache key `e9f5704207b2` on both stores,
`ingest.idempotency_cache_row_retired rel_type=has_pet` → `ingest.idempotency_cache_busted_stale`
→ `class_a_committed count=2 replay=False`. Marked-replay control (second pet, same
seat): forget isis → marked replay (`X-FL-Deferred-Replay: 1` +
`X-FL-Replay-Lane: reextract`) → recall **STILL ABSENT**, row count NOT bumped
(`class_a_committed count=1 replay=True`); the UNMARKED replay of the SAME body →
recall **PRESENT** (count bumped). Marker is the only variable. Paraphrase re-check:
forget rex again → paraphrase → recall present.

Verdict: READY FOR CRITIC.
