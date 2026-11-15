# FOSS-004 — correcting a scalar-attribute fact APPLIES the supersede but reports isError/failed "Old hierarchical/relational fact not found" — the tool tells the user the correction failed while it succeeded

- **Branch matrix:** es 761cf914 FAILED (reported failed, work done) · it 9f2724ac succeeded (status corrected, recall correct) · main c0fe9322 succeeded (status corrected, recall correct) — same shared lane, opposite outcomes: the guessed attribute name decides success (see Root cause), and on the "success" runs the reported OLD VALUE was fabricated (FOSS-010)
- **Severity:** High (client models relay the failure to the user on es; on en/it the success is real but its "old value" story is fiction — trust damage either way)
- **Discovered:** 2026-10-17, fresh-install functional gauntlet, es

## Symptom

README's canonical scenario, in Spanish, on a fresh es install:

1. remember: "Mi servidor principal se llama DevBox, su IP es 10.0.1.5."  -> has_ip=10.0.1.5 stored as a SCALAR attribute (ipv4). recall renders "Devbox Has IP Address 10.0.1.5".
2. correct: "En realidad DevBox se mudó a 10.0.1.10 después del cambio de red."
   -> remember_facts returns:

   {"status": "failed", ..., "facts_superseded": 0,
    "message": "Old hierarchical fact not found: located_in", "isError": true}

3. recall afterwards: **"Devbox Has IP Address 10.0.1.10"** — the correction IS in effect.
4. A second, value-identical correction ("La IP de DevBox ahora es 10.0.1.10.") fails again with "Old relational fact not found: ip_address" — reporting not-found for a fact that exists and is already current.

## Root cause (from the DB audit)

The /retract/correct server lane DID the work: entity_attributes.has_ip updated in place and the prior value archived:

    entity_attributes_history:
      has_ip | 10.0.1.5 -> 10.0.1.10 | action=superseded | cause=ingest_restate | 15:51:49

…but the response aggregation only counts RELATIONAL/HIERARCHICAL supersedes (facts / taxonomies). The correction extractor guesses graph rel_types ("located_in", then "ip_address") that were never the storage shape — the IP lives as an entity ATTRIBUTE. When the graph-leg lookup misses, the lane reports failed/not-found even though the scalar leg superseded + archived. Two defects in one:

  (a) response status/message contradicts the persisted effect (misleading isError on success);
  (b) the extractor's old-fact guess is disconnected from where scalars actually live (the
      message names a rel_type the tenant never had).

Three-way confirmation of the lottery (2026-10-17 battery): es guessed located_in -> failed;
en guessed lives_at -> matched the re-minted lives_at row -> succeeded; it guessed
ip_address -> matched -> succeeded. The outcome is decided by whether the lane's GUESS
happens to equal the attribute name the original capture lane used (has_ip vs lives_at vs
ip_address) — a coin flip per install/language. On the "succeeded" runs the reported old
value (10.0.1.9) was never stored anywhere (see FOSS-010) — the DB had no prior row and no
history row, yet the message reported "lives_at=10.0.1.9 -> lives_at=10.0.1.10".

## Reproduce

Fresh install any branch -> remember the DevBox IP sentence -> issue the README's "actually it moved" correction in the install language -> observe failed + facts_superseded:0 -> recall shows the NEW value.

## Fix direction (gauntlet work order)

1. Make the response reflect the scalar leg: when entity_attributes supersede/archive occurred (entity_attributes_history row with cause=retract_lane/ingest_restate in the same transaction), return status=corrected with dimension=SCALAR, the old and new values, and facts_superseded>=0. isError only when NOTHING changed.
2. When the new value equals the current value, return an explicit already-current status (not "not found").
3. Stop guessing graph rel_types for attribute-backed facts: resolve the old fact by (entity, attribute) first; fall back to the graph.
4. Pins: (i) integration pin — remember IP, correct it, assert response status=corrected AND recall returns the new value AND entity_attributes_history has the superseded row; (ii) idempotent re-correct returns already-current; (iii) a true no-match correction still fails honestly (nothing written).
5. **You are NOT your own critic.** When green, request a clean critic agent; critic reproduces the battery on a fresh stack, runs semgrep + codeql over the diff, and audits the response-shape tests for tautology before passing.
