# dBug-029-deepseek: Contradictory Facts & Entity Fragmentation — Investigation

**Status:** INVESTIGATION COMPLETE (2026-05-16)
**Investigated by:** DEEPSEEK-30B
**Awaiting:** CLAUDE review + dprompt for fix implementation

## Summary

Four root causes identified for corrupted /query responses and LLM confusion.

## Root Cause 1: Pronoun/Stopword Entities Survived Phase 3 Cleanup

**Finding:** Phase 3 cleanup (DEEPSEEK-27C) only deleted entities with `entity_type='unknown'`. Three entities with `entity_type='Person'` survived because GLiNER2 misclassified them during extraction:

| UUID | Alias | Entity Type | Problem |
|------|-------|-------------|---------|
| `d010884b-77df-5e67-9dc9-09a8b05601b8` | we | Person | Pronoun — creates "we parent_of alice", "we parent_of charlie" |
| `a91f8c22-7deb-5d6a-951f-9be50b1b1e07` | i | Person | Pronoun — creates "i parent_of john", "i spouse emma" |
| `257177dc-8e56-5c77-b811-b3777213adcd` | named | Person | Stopword — creates "named parent_of chris" |

**Evidence (pre-prod DB):**
```sql
SELECT e.id, e.entity_type, ea.alias FROM entities e
JOIN entity_aliases ea ON ea.entity_id = e.id
WHERE e.user_id = '10d7d879-...' AND ea.alias IN ('we','i','named');
-- Returns 3 rows, all entity_type='Person', is_preferred=true
```

/query returns 14 pronoun-containing facts because `_resolve_display_names()` maps UUIDs to these preferred names.

**Impact:**
- 14 pronoun facts visible to LLM
- Multiple identity anchors fragment user representation
- "named parent_of chris" makes the LLM think there's a person called "named"

## Root Cause 2: Staged_Facts Class C Contradictions

**Finding:** `aurora instance_of computer` exists in `staged_facts` (Class C, confidence=0.4). This was our rejected edge from the "my device is a computer" test — the `_commit_rejected_edge_to_qdrant()` function stored it. `/query` UNIONs facts + staged_facts via `_fetch_user_facts()`, so Class C experimental edges appear alongside authoritative Class A facts.

**Evidence:**
```sql
SELECT subject_id, object_id, rel_type, confidence, fact_class
FROM staged_facts WHERE user_id = '10d7d879-...'
AND subject_id LIKE '%aurora%';
-- Returns: aurora instance_of computer (confidence=0.4, fact_class=C)
```

**Impact:** LLM sees both "aurora is a pet" (from Qdrant — see RC4) AND "aurora is a computer" — contradiction.

## Root Cause 3: User Identity Fragmentation

**Finding:** Five separate entities serve as user identity anchors:
- `10d7d879-...` = "chris" (Person, canonical user UUID)
- `d010884b-...` = "we" (Person, pronoun entity)
- `a91f8c22-...` = "i" (Person, pronoun entity)
- `257177dc-...` = "named" (Person, stopword entity)
- `3ec2adc3-...` = "ca" (Location, alias entity)

All have `is_preferred=true` in `entity_aliases`. The /query graph traversal uses ALL of them as identity anchors, producing 23 child/parent facts instead of ~6.

**Evidence:** /query returns `parent_of`/`child_of` facts from "we", "i", "chris", and "named" — all referencing the same children (bob, charlie, alice) but appearing as separate family trees.

## Root Cause 4: Qdrant Stale Points

**Finding:** `aurora instance_of pet` was deleted from PostgreSQL during Phase 3 cleanup (entity `32488b27` = 'computer' was deleted). But the corresponding Qdrant point still exists. Vector search returns it alongside the authoritative PostgreSQL facts.

**Evidence:** The fact appears in /query response but NOT in `facts` table or `staged_facts` table. Must originate from Qdrant vector similarity search.

**Mitigation:** Re-embedder reconciliation runs periodically and cleans stale Qdrant points. May need to wait one cycle or trigger manually.

## Proposed Fixes

| # | Fix | Location | Priority |
|---|-----|----------|----------|
| 1 | Delete pronoun/stopword entities + facts + aliases | Pre-prod DB | P0 |
| 2 | Filter Class C (confidence < 0.6) from /query family responses | main.py /query | P1 |
| 3 | Normalize user identity — one canonical anchor | registry.py or /query | P1 |
| 4 | Trigger Qdrant reconciliation or wait for next cycle | re-embedder | P2 |

## Verification SQL

All queries run against pre-prod (docker-host) and confirmed. See scratch-archive-2026-05-15-30.md for full output.
